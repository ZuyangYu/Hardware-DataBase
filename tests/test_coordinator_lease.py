"""P0-004/P0-005: single-owner coordinator lease and control loop."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from src.attachments.coordinator_lease import (
    AttachmentCoordinatorLoop,
    CoordinatorLeaseStore,
)


class _FakeCoordinator:
    def __init__(self, results: dict | None = None):
        self.results = results or {}
        self.reconcile_calls = 0
        self.expire_calls = 0

    def reconcile_waiting_turns(self, limit: int = 100) -> int:
        self.reconcile_calls += 1
        return int(self.results.get("reconcile", 0))

    def expire_waiting_turns(self, max_wait_seconds: int | None = None) -> int:
        self.expire_calls += 1
        return int(self.results.get("expire", 0))


class CoordinatorLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "chat_attachments.db")
        self.store = CoordinatorLeaseStore(db_path=self.db_path)

    def test_only_one_owner_and_takeover_bumps_token(self):
        first = self.store.try_acquire("worker-a", lease_seconds=15)
        self.assertEqual(first, 1)
        self.assertIsNone(self.store.try_acquire("worker-b", lease_seconds=15))
        self.assertTrue(self.store.renew("worker-a", first, lease_seconds=15))
        # Force expiry: another candidate may take over, token increases.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE coordinator_leases SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
                " WHERE fencing_token = 1"
            )
            conn.commit()
        second = self.store.try_acquire("worker-b", lease_seconds=15)
        self.assertEqual(second, 2)
        self.assertFalse(self.store.renew("worker-a", first, lease_seconds=15))
        self.assertFalse(self.store.release("worker-a", first))
        self.assertTrue(self.store.renew("worker-b", second, lease_seconds=15))

    def test_loop_scans_only_while_holding_lease(self):
        coord_a = _FakeCoordinator()
        coord_b = _FakeCoordinator()
        loop_a = AttachmentCoordinatorLoop(
            coordinator=coord_a, lease_store=self.store, worker_id="loop-a", interval_seconds=0.01
        )
        loop_b = AttachmentCoordinatorLoop(
            coordinator=coord_b, lease_store=self.store, worker_id="loop-b", interval_seconds=0.01
        )
        self.assertFalse(loop_a.run_once())  # nothing to resolve => no work
        self.assertEqual((coord_a.reconcile_calls, coord_a.expire_calls), (1, 1))
        # The second candidate refuses to scan while the lease is live.
        self.assertFalse(loop_b.run_once())
        self.assertEqual((coord_b.reconcile_calls, coord_b.expire_calls), (0, 0))
        # Simulate a dead holder: expire the row, then a standby takes over.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE coordinator_leases SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
            )
            conn.commit()
        self.assertFalse(loop_b.run_once())
        self.assertEqual((coord_b.reconcile_calls, coord_b.expire_calls), (1, 1))
        # The superseded holder stops scanning on its next cycle.
        self.assertFalse(loop_a.run_once())
        self.assertEqual((coord_a.reconcile_calls, coord_a.expire_calls), (1, 1))

    def test_background_thread_scans_and_stops(self):
        coord = _FakeCoordinator({"expire": 1})
        loop = AttachmentCoordinatorLoop(
            coordinator=coord,
            lease_store=self.store,
            worker_id="thread-loop",
            interval_seconds=0.02,
        )
        loop.start()
        try:
            deadline = 100
            while coord.expire_calls == 0 and deadline > 0:
                import time

                time.sleep(0.01)
                deadline -= 1
            self.assertGreater(coord.expire_calls, 0)
        finally:
            loop.stop()
        self.assertFalse(loop.holding_lease)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["owner_id"], "")


if __name__ == "__main__":
    unittest.main()
