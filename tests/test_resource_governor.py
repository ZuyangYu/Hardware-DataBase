"""P0-006: resource governor, quota store leases and rate limiting."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest

import src.settings
from src.core.concurrency import (
    GovernorRejected,
    RateLimiter,
    ResourceGovernor,
    SqliteQuotaStore,
    TokenBucket,
    governor_acquire,
    reset_governor,
)


class ProcessGovernorTests(unittest.TestCase):
    def _governor(self, **kwargs) -> ResourceGovernor:
        limits = kwargs.pop("limits", {"llm": (2, 1)})
        return ResourceGovernor(scope="process", limits=limits, **kwargs)

    def test_capacity_limits_and_release(self):
        governor = self._governor(limits={"llm": (2, 0)})
        first = governor.acquire("llm", timeout=0.5)
        second = governor.acquire("llm", timeout=0.5)
        with self.assertRaises(GovernorRejected) as ctx:
            governor.acquire("llm", timeout=0.1)
        self.assertEqual(ctx.exception.reason, "deadline")
        first.release()
        third = governor.acquire("llm", timeout=0.5)
        second.release()
        third.release()

    def test_interactive_reserve_is_not_consumed_by_background(self):
        governor = self._governor(limits={"rag": (2, 1)})
        background = governor.acquire("rag", interactive=False, timeout=0.2)
        with self.assertRaises(GovernorRejected):
            governor.acquire("rag", interactive=False, timeout=0.1)
        interactive = governor.acquire("rag", interactive=True, timeout=0.2)
        background.release()
        interactive.release()

    def test_waiting_caller_is_admitted_after_release(self):
        governor = self._governor(limits={"tool": (1, 0)})
        held = governor.acquire("tool", timeout=0.5)
        admitted: list[bool] = []

        def waiter() -> None:
            try:
                permit = governor.acquire("tool", timeout=2.0)
                admitted.append(True)
                permit.release()
            except GovernorRejected:
                admitted.append(False)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        held.release()
        thread.join(timeout=3)
        self.assertEqual(admitted, [True])

    def test_queue_depth_rejects_immediately(self):
        governor = self._governor(limits={"tool": (1, 0)}, queue_max_depth=1)
        held = governor.acquire("tool", timeout=0.5)
        blocking = threading.Event()

        def waiter() -> None:
            try:
                permit = governor.acquire("tool", timeout=0.2)
                permit.release()
            except GovernorRejected:
                pass
            finally:
                blocking.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        with self.assertRaises(GovernorRejected) as ctx:
            governor.acquire("tool", timeout=0.2)
        self.assertEqual(ctx.exception.reason, "queue_full")
        held.release()
        thread.join(timeout=3)
        self.assertTrue(blocking.is_set())


    def test_cancel_wait_stops_a_queued_acquisition(self):
        governor = self._governor(limits={"tool": (1, 0)})
        held = governor.acquire("tool", timeout=0.5)
        outcomes: list[str] = []

        def waiter() -> None:
            try:
                permit = governor.acquire("tool", timeout=5.0, request_id="req-1")
                permit.release()
                outcomes.append("granted")
            except GovernorRejected as exc:
                outcomes.append(exc.reason)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        self.assertTrue(governor.cancel_wait("req-1"))
        thread.join(timeout=3)
        held.release()
        self.assertEqual(outcomes, ["cancelled"])
        self.assertFalse(governor.cancel_wait("req-unknown"))

    def test_permit_finish_releases_process_slot(self):
        governor = self._governor(limits={"tool": (1, 0)})
        permit = governor.acquire("tool", timeout=0.5)
        self.assertTrue(
            permit.finish(outcome="completed", evidence="downstream_completed")
        )
        self.assertEqual(governor.snapshot()["tool"]["active"], 0)
        replacement = governor.acquire("tool", timeout=0.5)
        replacement.release()


class QuotaStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteQuotaStore(os.path.join(self.tmp.name, "quotas.db"), lease_seconds=30)

    def test_capacity_and_reserved_interactive(self):
        permit_a = self.store.try_acquire(
            "rag", capacity=2, reserved_interactive=1, interactive=False, owner_id="w1", attempt_id="a1"
        )
        self.assertIsNotNone(permit_a)
        permit_b = self.store.try_acquire(
            "rag", capacity=2, reserved_interactive=1, interactive=False, owner_id="w1", attempt_id="a1"
        )
        self.assertIsNone(permit_b)
        interactive = self.store.try_acquire(
            "rag", capacity=2, reserved_interactive=1, interactive=True, owner_id="api", attempt_id=""
        )
        self.assertIsNotNone(interactive)
        self.store.release(permit_a)
        permit_c = self.store.try_acquire(
            "rag", capacity=2, reserved_interactive=1, interactive=False, owner_id="w2", attempt_id="a2"
        )
        self.assertIsNotNone(permit_c)
        self.store.release(permit_c)
        self.store.release(interactive)
        self.assertEqual(self.store.active_counts().get("rag", 0), 0)

    def test_expired_lease_becomes_uncertain_and_frees_capacity(self):
        store = SqliteQuotaStore(self.store.path, lease_seconds=30)
        permit = store.try_acquire(
            "llm", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertIsNotNone(permit)
        self.assertEqual(store.active_counts().get("llm"), 1)
        # Force the lease into the past without touching the row state.
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute(
                "UPDATE resource_permits SET expires_at = '2000-01-01T00:00:00+00:00' WHERE permit_id = ?",
                (permit,),
            )
            conn.commit()
        replacement = store.try_acquire(
            "llm", capacity=1, reserved_interactive=0, interactive=True, owner_id="w2", attempt_id="a2"
        )
        self.assertIsNotNone(replacement)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["llm"]["active"], 1)
        self.assertEqual(snapshot["llm"]["uncertain"], 1)

    def test_maintenance_lease_single_owner_and_takeover(self):
        first = self.store.try_acquire_maintenance("maintainer-a", lease_seconds=30)
        self.assertEqual(first, 1)
        self.assertIsNone(self.store.try_acquire_maintenance("maintainer-b", lease_seconds=30))
        self.assertTrue(self.store.renew_maintenance("maintainer-a", first, lease_seconds=30))
        self.assertFalse(self.store.renew_maintenance("maintainer-a", int(first) + 1, lease_seconds=30))
        import sqlite3 as _sqlite3
        from contextlib import closing as _closing

        with _closing(_sqlite3.connect(self.store.path)) as conn:
            conn.execute(
                "UPDATE resource_maintenance_lease SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
            )
            conn.commit()
        second = self.store.try_acquire_maintenance("maintainer-b", lease_seconds=30)
        self.assertEqual(second, 2, "takeover must bump the fencing token")
        self.assertFalse(self.store.renew_maintenance("maintainer-a", first))
        self.assertTrue(self.store.release_maintenance("maintainer-b", second))
        self.assertEqual(self.store.maintenance_snapshot()["owner_id"], "")

    def test_renew_extends_only_active_permits(self):
        permit = self.store.try_acquire(
            "tool", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertTrue(self.store.renew(str(permit)))
        self.store.release(str(permit))
        self.assertFalse(self.store.renew(str(permit)))


class GovernorUtilityTests(unittest.TestCase):
    def test_governor_acquire_is_noop_when_disabled(self):
        original = src.settings.RESOURCE_GOVERNOR_ENABLED
        src.settings.RESOURCE_GOVERNOR_ENABLED = False
        reset_governor()
        try:
            with governor_acquire("llm") as permit:
                self.assertIsNone(permit)
        finally:
            src.settings.RESOURCE_GOVERNOR_ENABLED = original
            reset_governor()

    def test_token_bucket_and_rate_limiter(self):
        bucket = TokenBucket(rate_per_second=0.5, burst=2)
        self.assertTrue(bucket.take())
        self.assertTrue(bucket.take())
        self.assertFalse(bucket.take())
        self.assertTrue(bucket.wait(tokens=1, timeout=3.0))
        limiter = RateLimiter({"llm": (0.5, 1)})
        self.assertTrue(limiter.allow("llm"))
        self.assertFalse(limiter.allow("llm"))
        self.assertTrue(limiter.allow("unconfigured"))
        self.assertTrue(limiter.wait("llm", timeout=3.0))


class LlmPriorityPropagationTests(unittest.TestCase):
    """P0b: ``llm_governor.slot`` is the single LLM admission authority."""

    def test_slot_acquires_deployment_permit_with_priority(self):

        import src.core.concurrency as concurrency
        from src.core import llm_governor

        captured: list[tuple[str, bool]] = []
        exits: list[int] = []
        original = concurrency.governor_acquire

        class _Permit:
            def __enter__(self):
                return None

            def __exit__(self, *_args):
                exits.append(1)
                return False

        def spy(resource, *, interactive=False, **kwargs):
            captured.append((resource, interactive))
            return _Permit()

        concurrency.governor_acquire = spy
        try:
            governor = llm_governor.LLMGovernor()
            with governor.slot(llm_governor.PRIORITY_BATCH):
                pass
            with governor.slot(llm_governor.PRIORITY_INTERACTIVE):
                pass
        finally:
            concurrency.governor_acquire = original
        self.assertEqual(captured, [("llm", False), ("llm", True)])
        self.assertEqual(len(exits), 2, "deployment permits must be released")

    def test_slot_releases_process_and_deployment_on_rejection(self):
        import src.core.concurrency as concurrency
        from src.core import llm_governor

        original = concurrency.governor_acquire

        def rejecting(resource, *, interactive=False, **kwargs):
            raise concurrency.GovernorRejected(resource, "uncertain_escalated")

        concurrency.governor_acquire = rejecting
        try:
            governor = llm_governor.LLMGovernor()
            with self.assertRaises(concurrency.GovernorRejected):
                with governor.slot(llm_governor.PRIORITY_INTERACTIVE):
                    pass
            # The process slot must be released even though the deployment
            # permit was rejected.
            self.assertEqual(governor.snapshot().get("llm", {}).get("active", 0), 0)
        finally:
            concurrency.governor_acquire = original

    def test_batch_slot_sets_context_priority_and_resets_after_timeout(self):
        from src.core import llm_governor

        original_timeout = src.settings.LLM_QUEUE_TIMEOUT_SECONDS
        original_total = src.settings.LLM_MAX_CONCURRENCY
        original_batch = src.settings.LLM_BATCH_MAX_CONCURRENCY
        src.settings.LLM_QUEUE_TIMEOUT_SECONDS = 1
        src.settings.LLM_MAX_CONCURRENCY = 1
        src.settings.LLM_BATCH_MAX_CONCURRENCY = 1
        try:
            governor = llm_governor.LLMGovernor()
            with governor.slot(llm_governor.PRIORITY_BATCH):
                self.assertEqual(
                    llm_governor.current_priority(), llm_governor.PRIORITY_BATCH
                )
            holder = governor.slot(llm_governor.PRIORITY_BATCH)
            holder.__enter__()
            try:
                with self.assertRaises(llm_governor.LLMQueueTimeoutError):
                    with governor.slot(llm_governor.PRIORITY_BATCH):
                        pass
            finally:
                holder.__exit__(None, None, None)
            self.assertEqual(
                llm_governor.current_priority(), llm_governor.PRIORITY_INTERACTIVE
            )
        finally:
            src.settings.LLM_QUEUE_TIMEOUT_SECONDS = original_timeout
            src.settings.LLM_MAX_CONCURRENCY = original_total
            src.settings.LLM_BATCH_MAX_CONCURRENCY = original_batch


class JudgeGovernanceStatusTests(unittest.TestCase):
    """P0-006: judge-LLM governance assembly state is explicit, not implicit."""

    def setUp(self):
        from src.core import model_gateway

        model_gateway.reset_governance_status()
        self.addCleanup(model_gateway.reset_governance_status)
        self.gateway = model_gateway

    def test_assembly_is_recorded_and_method_wrapped(self):
        class FakeLLM:
            def generate(self, *_a, **_k):
                return None

        llm = self.gateway.govern_ragas_llm(FakeLLM())
        self.assertTrue(getattr(llm.generate, "_hdb_governed", False))
        status = self.gateway.governance_status()
        self.assertTrue(status["assembled"])
        self.assertFalse(status["bypassed"])
        self.assertIn("generate", status["wrapped_methods"])

    def test_assembly_failure_is_marked_bypassed(self):
        class Exploding:
            @property
            def generate(self):
                raise RuntimeError("boom")

        original = Exploding()
        returned = self.gateway.govern_ragas_llm(original)
        self.assertIs(returned, original, "governance must fail open")
        status = self.gateway.governance_status()
        self.assertFalse(status["assembled"])
        self.assertTrue(status["bypassed"])
        self.assertIn("boom", status["reason"])

    def test_missing_generate_is_marked_bypassed(self):
        class Empty:
            pass

        self.gateway.govern_ragas_llm(Empty())
        status = self.gateway.governance_status()
        self.assertTrue(status["bypassed"])
        self.assertEqual(status["reason"], "no_generate_method")


class GovernorMaintenanceStartupTests(unittest.TestCase):
    """P0-006 regression: maintenance startup must not self-deadlock."""

    def setUp(self):
        import src.settings as settings
        from src.core.concurrency import reset_governor, stop_governor_maintenance

        self.settings = settings
        self._old = {
            key: getattr(settings, key)
            for key in (
                "RESOURCE_GOVERNOR_ENABLED",
                "RESOURCE_GOVERNOR_SCOPE",
                "RESOURCE_GOVERNOR_DB_PATH",
                "RESOURCE_GOVERNOR_MAINTENANCE_SECONDS",
            )
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(stop_governor_maintenance)
        self.addCleanup(reset_governor)
        self.addCleanup(self._restore)
        settings.RESOURCE_GOVERNOR_ENABLED = True
        settings.RESOURCE_GOVERNOR_SCOPE = "deployment"
        settings.RESOURCE_GOVERNOR_DB_PATH = os.path.join(self.tmp.name, "quotas.db")
        settings.RESOURCE_GOVERNOR_MAINTENANCE_SECONDS = 0.05
        reset_governor()

    def _restore(self):
        for key, value in self._old.items():
            setattr(self.settings, key, value)

    def test_start_maintenance_does_not_deadlock(self):
        from src.core.concurrency import start_governor_maintenance

        result: dict[str, object] = {}

        def run() -> None:
            result["thread"] = start_governor_maintenance()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "start_governor_maintenance deadlocked")
        self.assertIsNotNone(result.get("thread"), "maintenance thread was not started")

    def test_start_is_idempotent(self):
        from src.core.concurrency import start_governor_maintenance

        first = start_governor_maintenance()
        second = start_governor_maintenance()
        self.assertIsNotNone(first)
        self.assertIs(first, second)

    def test_maintenance_leader_elected_and_released(self):
        import os

        from src.core.concurrency import (
            get_governor,
            start_governor_maintenance,
            stop_governor_maintenance,
        )

        start_governor_maintenance()
        store = get_governor().store
        self.assertIsNotNone(store)
        expected = f"governor-maint-{os.getpid()}"
        owner = ""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            owner = str(store.maintenance_snapshot()["owner_id"])
            if owner == expected:
                break
            time.sleep(0.05)
        snapshot = store.maintenance_snapshot()
        self.assertEqual(owner, expected)
        self.assertGreaterEqual(snapshot["fencing_token"], 1)
        stop_governor_maintenance()
        self.assertEqual(store.maintenance_snapshot()["owner_id"], "")


if __name__ == "__main__":
    unittest.main()
