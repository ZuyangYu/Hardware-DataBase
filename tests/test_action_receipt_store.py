"""P2-001: persistent action receipts and crash-recovery semantics."""

from __future__ import annotations

import os
import tempfile
import unittest

import src.settings
from src.agents.orchestration.capabilities import capability_for_action
from src.agents.orchestration.contracts import ActionReceipt, ActionSpec, ConversationActionPlan
from src.agents.orchestration.executor import ConversationActionExecutor
from src.agents.orchestration.receipts import (
    ActionExecutionScope,
    ActionReceiptStore,
)


def _plan(*actions: ActionSpec, plan_id: str = "plan-1") -> ConversationActionPlan:
    return ConversationActionPlan(plan_id=plan_id, goal="test", actions=list(actions))


class ActionReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ActionReceiptStore(os.path.join(self.tmp.name, "auth.db"))
        self.scope = ActionExecutionScope(
            tenant_id="tenant-a", session_generation=3, turn_id="turn-1", plan_version="v1"
        )

    def test_upsert_get_and_monotonic_revision(self):
        receipt = ActionReceipt(action_id="a1", status="completed", result_refs=["job:1"])
        revision = self.store.upsert(self.scope, "plan-1", receipt)
        self.assertEqual(revision, 1)
        stored = self.store.get(self.scope, "plan-1", "a1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.result_refs, ["job:1"])
        # A stale writer cannot move the receipt backwards.
        revision = self.store.upsert(
            self.scope, "plan-1", ActionReceipt(action_id="a1", status="failed"),
            expected_revision=99,
        )
        self.assertEqual(revision, 1)
        self.assertEqual(self.store.get(self.scope, "plan-1", "a1").status, "completed")

    def test_scopes_are_isolated(self):
        self.store.upsert(self.scope, "plan-1", ActionReceipt(action_id="a1", status="completed"))
        other = ActionExecutionScope(
            tenant_id="tenant-b", session_generation=3, turn_id="turn-1", plan_version="v1"
        )
        self.assertIsNone(self.store.get(other, "plan-1", "a1"))
        later_generation = ActionExecutionScope(
            tenant_id="tenant-a", session_generation=4, turn_id="turn-1", plan_version="v1"
        )
        self.assertIsNone(self.store.get(later_generation, "plan-1", "a1"))

    def test_reconciliation_marker_blocks_replay(self):
        self.store.upsert(
            self.scope,
            "plan-1",
            ActionReceipt(
                action_id="a1",
                status="failed",
                error_code="handler_error",
                data={"needs_reconciliation": True},
            ),
        )
        stored = self.store.get(self.scope, "plan-1", "a1")
        self.assertFalse(stored.replayable)
        self.store.upsert(
            self.scope, "plan-1", ActionReceipt(action_id="a2", status="failed")
        )
        self.assertTrue(self.store.get(self.scope, "plan-1", "a2").replayable)


class ExecutorReceiptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ActionReceiptStore(os.path.join(self.tmp.name, "auth.db"))
        self.scope = ActionExecutionScope(
            tenant_id="t", session_generation=1, turn_id="turn-1", plan_version="v1"
        )
        self.calls: list[str] = []
        self.keys: list[str] = []

        def handler(action, _prior, idempotency_key=""):
            self.calls.append(action.action_id)
            self.keys.append(idempotency_key)
            return {"status": "completed", "data": {"job_ref": f"job:{action.action_id}"}}

        self.handler = handler
        self._original_parallel = src.settings.ACTION_DAG_PARALLEL_ENABLED
        self.addCleanup(setattr, src.settings, "ACTION_DAG_PARALLEL_ENABLED", self._original_parallel)

    def _executor(self) -> ConversationActionExecutor:
        return ConversationActionExecutor(
            handlers={"search": self.handler},
            available_capabilities={capability_for_action("search")},
            receipt_store=self.store,
            execution_scope=self.scope,
        )

    def test_completed_receipt_is_reused_without_redispatch(self):
        plan = _plan(ActionSpec(action_id="a1", kind="search"))
        first = self._executor().execute(plan)
        self.assertEqual([r.status for r in first.receipts], ["completed"])
        self.assertEqual(self.calls, ["a1"])
        second = self._executor().execute(plan)
        self.assertEqual([r.status for r in second.receipts], ["completed"])
        self.assertEqual(self.calls, ["a1"], "completed action must not re-dispatch")
        self.assertEqual(second.receipts[0].data.get("job_ref"), "job:a1")
        stored = self.store.get(self.scope, "plan-1", "a1")
        self.assertEqual(stored.job_ref, "job:a1")

    def test_idempotency_key_is_passed_to_capable_handlers(self):
        plan = _plan(ActionSpec(action_id="a1", kind="search"))
        self._executor().execute(plan)
        self.assertEqual(len(self.keys), 1)
        key = self.keys[0]
        self.assertIn("t", key)
        self.assertIn("turn-1", key)
        self.assertIn("a1", key)

    def test_failed_action_with_idempotency_support_is_replayed(self):
        attempts = {"count": 0}

        def flaky(action, _prior, idempotency_key=""):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("transient")
            return {"status": "completed"}

        executor = ConversationActionExecutor(
            handlers={"search": flaky},
            available_capabilities={capability_for_action("search")},
            receipt_store=self.store,
            execution_scope=self.scope,
        )
        plan = _plan(ActionSpec(action_id="a1", kind="search"))
        first = executor.execute(plan)
        self.assertEqual([r.status for r in first.receipts], ["failed"])
        stored = self.store.get(self.scope, "plan-1", "a1")
        self.assertTrue(stored.replayable)
        self.assertNotIn("needs_reconciliation", stored.data)
        second = executor.execute(plan)
        self.assertEqual([r.status for r in second.receipts], ["completed"])
        self.assertEqual(attempts["count"], 2)

    def test_non_idempotent_failure_requires_reconciliation(self):
        def broken(action, _prior):
            raise RuntimeError("boom")

        executor = ConversationActionExecutor(
            handlers={"search": broken},
            available_capabilities={capability_for_action("search")},
            receipt_store=self.store,
            execution_scope=self.scope,
        )
        plan = _plan(ActionSpec(action_id="a1", kind="search"))
        first = executor.execute(plan)
        self.assertEqual([r.status for r in first.receipts], ["failed"])
        stored = self.store.get(self.scope, "plan-1", "a1")
        self.assertFalse(stored.replayable)
        self.assertTrue(stored.data.get("needs_reconciliation"))
        # Recovery must not auto-retry an unreconcilable write.
        second = executor.execute(plan)
        self.assertEqual([r.status for r in second.receipts], ["failed"])
        self.assertEqual(second.receipts[0].error_code, "handler_error")

    def test_parallel_layer_uses_receipts_for_partial_recovery(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = True
        plan = _plan(
            ActionSpec(action_id="a1", kind="search"),
            ActionSpec(action_id="a2", kind="search"),
        )
        first = self._executor().execute(plan)
        self.assertEqual(sorted(self.calls), ["a1", "a2"])
        self.assertEqual(len(first.receipts), 2)
        second = self._executor().execute(plan)
        self.assertEqual(len(second.receipts), 2)
        self.assertEqual(sorted(self.calls), ["a1", "a2"], "no re-dispatch for completed receipts")


    def test_plan_persisted_and_domain_settlement_resumes_dependent_action(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = True
        calls: list[str] = []

        def create(action, _prior):
            calls.append("gen")
            return {"status": "accepted", "result_refs": ["task:task-1"]}

        def revise(action, _prior, idempotency_key=""):
            calls.append("rev")
            return {"status": "completed"}

        executor = ConversationActionExecutor(
            handlers={"create_document": create, "revise_document": revise},
            available_capabilities={
                capability_for_action("create_document"),
                capability_for_action("revise_document"),
            },
            receipt_store=self.store,
            execution_scope=self.scope,
        )
        plan = _plan(
            ActionSpec(action_id="gen", kind="create_document"),
            ActionSpec(action_id="rev", kind="revise_document", depends_on=["gen"]),
        )
        first = executor.execute(plan)
        statuses = {receipt.action_id: receipt.status for receipt in first.receipts}
        self.assertEqual(statuses["gen"], "accepted")
        self.assertEqual(statuses["rev"], "skipped")
        self.assertEqual(self.store.get_plan(self.scope, "plan-1")["status"], "awaiting_domain")

        # A domain completion event settles the accepted receipt (idempotently).
        self.assertEqual(self.store.settle_domain_job("task:task-1", status="completed"), 1)
        self.assertEqual(self.store.settle_domain_job("task:task-1", status="completed"), 0)
        self.assertEqual(self.store.get(self.scope, "plan-1", "gen").status, "completed")
        self.assertTrue(
            any(row["plan_id"] == "plan-1" for row in self.store.list_ready_plans())
        )

        # Re-entering the plan reuses the settled receipt and runs only the
        # missing dependent action.
        second = executor.execute(plan)
        statuses = {receipt.action_id: receipt.status for receipt in second.receipts}
        self.assertEqual(statuses["rev"], "completed")
        self.assertEqual(calls.count("gen"), 1)
        self.assertEqual(calls.count("rev"), 1)
        self.assertEqual(self.store.get_plan(self.scope, "plan-1")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
