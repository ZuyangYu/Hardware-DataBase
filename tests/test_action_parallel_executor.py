"""P2-001: action DAG contract resolution and bounded parallel execution."""

from __future__ import annotations

import threading
import time
import unittest

import src.settings
from src.agents.orchestration.capabilities import capability_for_action
from src.agents.orchestration.contracts import (
    ActionSpec,
    ConversationActionPlan,
    resolve_action,
)
from src.agents.orchestration.executor import ConversationActionExecutor


def _plan(*actions: ActionSpec) -> ConversationActionPlan:
    return ConversationActionPlan(plan_id="plan-1", goal="test", actions=list(actions))


class _Recorder:
    def __init__(self, barrier: threading.Barrier | None = None, fail_on: str = ""):
        self.active = 0
        self.high_water = 0
        self.order: list[str] = []
        self.lock = threading.Lock()
        self.barrier = barrier
        self.fail_on = fail_on

    def handler(self, action: ActionSpec, _prior):
        with self.lock:
            self.active += 1
            self.high_water = max(self.high_water, self.active)
            self.order.append(action.action_id)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=2.0)
            if action.action_id == self.fail_on:
                raise RuntimeError("boom")
            time.sleep(0.05)
            status = "accepted" if action.kind == "create_document" else "completed"
            return {"status": status}
        finally:
            with self.lock:
                self.active -= 1


class ActionParallelExecutorTests(unittest.TestCase):
    def setUp(self):
        self._original_parallel = src.settings.ACTION_DAG_PARALLEL_ENABLED
        self._original_max = src.settings.ACTION_EXECUTOR_MAX_PARALLEL
        self.addCleanup(setattr, src.settings, "ACTION_DAG_PARALLEL_ENABLED", self._original_parallel)
        self.addCleanup(setattr, src.settings, "ACTION_EXECUTOR_MAX_PARALLEL", self._original_max)
        src.settings.ACTION_EXECUTOR_MAX_PARALLEL = 4

    def _executor(self, recorder: _Recorder, kinds: set[str]) -> ConversationActionExecutor:
        return ConversationActionExecutor(
            handlers={kind: recorder.handler for kind in kinds},
            available_capabilities={capability_for_action(kind) for kind in kinds},
        )

    def test_flag_off_is_sequential(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = False
        recorder = _Recorder()
        executor = self._executor(recorder, {"search"})
        result = executor.execute(
            _plan(
                ActionSpec(action_id="a1", kind="search"),
                ActionSpec(action_id="a2", kind="search"),
            )
        )
        self.assertEqual(recorder.high_water, 1)
        self.assertEqual([receipt.status for receipt in result.receipts], ["completed", "completed"])

    def test_read_only_actions_run_parallel_when_enabled(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = True
        recorder = _Recorder(barrier=threading.Barrier(2))
        executor = self._executor(recorder, {"search"})
        result = executor.execute(
            _plan(
                ActionSpec(action_id="a1", kind="search"),
                ActionSpec(action_id="a2", kind="search"),
            )
        )
        self.assertEqual(recorder.high_water, 2)
        self.assertEqual([receipt.action_id for receipt in result.receipts], ["a1", "a2"])

    def test_write_actions_stay_serial(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = True
        recorder = _Recorder()
        executor = self._executor(recorder, {"cancel"})
        executor.execute(
            _plan(
                ActionSpec(action_id="a1", kind="cancel"),
                ActionSpec(action_id="a2", kind="cancel"),
            )
        )
        self.assertEqual(recorder.high_water, 1)

    def test_unknown_kind_defaults_to_write_heavy(self):
        # ``model_construct`` bypasses the Literal so the server-side fallback
        # for an unrecognized kind can be verified.
        unknown = ActionSpec.model_construct(action_id="x", kind="mystery")
        resolved = resolve_action(unknown)
        self.assertEqual(resolved.access_mode, "write")
        self.assertEqual(resolved.cost_class, "heavy")

    def test_dependency_on_accepted_requires_completed_when_enabled(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = True
        recorder = _Recorder()
        executor = self._executor(recorder, {"create_document", "revise_document"})
        result = executor.execute(
            _plan(
                ActionSpec(action_id="gen", kind="create_document"),
                ActionSpec(action_id="revise", kind="revise_document", depends_on=["gen"]),
            )
        )
        statuses = {receipt.action_id: receipt.status for receipt in result.receipts}
        self.assertEqual(statuses["revise"], "skipped")
        self.assertNotIn("revise", recorder.order)

    def test_dependency_on_accepted_runs_in_legacy_mode(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = False
        recorder = _Recorder()
        executor = self._executor(recorder, {"create_document", "revise_document"})
        result = executor.execute(
            _plan(
                ActionSpec(action_id="gen", kind="create_document"),
                ActionSpec(action_id="revise", kind="revise_document", depends_on=["gen"]),
            )
        )
        statuses = {receipt.action_id: receipt.status for receipt in result.receipts}
        self.assertEqual(statuses["revise"], "completed")

    def test_partial_failure_keeps_stable_order(self):
        src.settings.ACTION_DAG_PARALLEL_ENABLED = True
        recorder = _Recorder(fail_on="a2")
        executor = self._executor(recorder, {"search"})
        result = executor.execute(
            _plan(
                ActionSpec(action_id="a1", kind="search"),
                ActionSpec(action_id="a2", kind="search"),
            )
        )
        statuses = [receipt.status for receipt in result.receipts]
        self.assertEqual(statuses, ["completed", "failed"])


if __name__ == "__main__":
    unittest.main()
