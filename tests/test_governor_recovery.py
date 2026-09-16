"""P0-006: uncertain permit escalation and evidence-backed recovery."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.core.concurrency import SqliteQuotaStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.production_hardening.governor_recovery import main as recovery_main  # noqa: E402


def _expire(store: SqliteQuotaStore, permit_id: str, age_seconds: int = 0) -> None:
    with closing(sqlite3.connect(store.path)) as conn:
        conn.execute(
            "UPDATE resource_permits SET expires_at = datetime('now', ?) WHERE permit_id = ?",
            (f"-{int(age_seconds) + 60} seconds", permit_id),
        )
        conn.commit()


def _age_uncertain(store: SqliteQuotaStore, permit_id: str, seconds: int) -> None:
    with closing(sqlite3.connect(store.path)) as conn:
        conn.execute(
            "UPDATE resource_permits SET uncertain_since = datetime('now', ?) WHERE permit_id = ?",
            (f"-{int(seconds)} seconds", permit_id),
        )
        conn.commit()


class UncertainPermitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "resource_quotas.db")
        self.store = SqliteQuotaStore(self.path, lease_seconds=30, deployment_id="test")

    def test_maintain_reaps_and_escalates_after_threshold(self):
        permit = self.store.try_acquire(
            "llm", capacity=2, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertIsNotNone(permit)
        _expire(self.store, permit)
        result = self.store.maintain(escalate_seconds=900)
        self.assertEqual(result["reaped"], 1)
        self.assertEqual(result["escalated"], 0)
        _age_uncertain(self.store, permit, seconds=1200)
        result = self.store.maintain(escalate_seconds=900)
        self.assertEqual(result["escalated"], 1)
        self.assertIn("llm", self.store.escalated_resources())
        # Escalation alerts and pauses admission; it never frees capacity.
        self.assertIsNone(
            self.store.try_acquire(
                "llm", capacity=2, reserved_interactive=0, interactive=True, owner_id="w2", attempt_id="a2"
            )
        )
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["llm"]["uncertain"], 1)
        self.assertEqual(snapshot["llm"]["active"], 0)

    def test_release_requires_evidence_and_is_idempotent(self):
        permit = self.store.try_acquire(
            "rag", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        _expire(self.store, permit)
        self.store.reap_expired(self.store._connect())
        with self.assertRaises(ValueError):
            self.store.release_uncertain(str(permit), evidence="client_timeout")
        released = self.store.release_uncertain(
            str(permit),
            evidence="downstream_completed",
            reason="provider confirmed completion",
            operator="ops-1",
        )
        self.assertTrue(released)
        # Duplicate release is a no-op, not an error and not a second credit.
        self.assertFalse(
            self.store.release_uncertain(str(permit), evidence="downstream_completed")
        )
        audit = self.store.audit_entries(str(permit))
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "release_uncertain")
        self.assertEqual(audit[0]["operator"], "ops-1")

    def test_escalated_resource_rejects_new_admissions_until_released(self):
        from src.core.concurrency import GovernorRejected, ResourceGovernor

        permit = self.store.try_acquire(
            "tool", capacity=2, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        _expire(self.store, permit)
        self.store.reap_expired(self.store._connect())
        _age_uncertain(self.store, permit, seconds=1200)
        self.store.maintain(escalate_seconds=900)
        governor = ResourceGovernor(scope="deployment", store=self.store, limits={"tool": (2, 0)})
        with self.assertRaises(GovernorRejected) as ctx:
            governor.acquire("tool", interactive=True, timeout=0.2)
        self.assertEqual(ctx.exception.reason, "uncertain_escalated")
        self.store.release_uncertain(str(permit), evidence="operator_verified_restart", operator="ops")
        permit2 = governor.acquire("tool", interactive=True, timeout=0.5)
        permit2.release()

    def test_finish_requires_evidence_and_is_idempotent(self):
        permit = self.store.try_acquire(
            "tool", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        with self.assertRaises(ValueError):
            self.store.finish(str(permit), outcome="completed", evidence="client_exception")
        self.assertTrue(
            self.store.finish(
                str(permit), outcome="completed", evidence="downstream_completed"
            )
        )
        # Duplicate confirmation is handled, not an error.
        self.assertTrue(
            self.store.finish(
                str(permit), outcome="completed", evidence="downstream_completed"
            )
        )
        audit = self.store.audit_entries(str(permit))
        self.assertEqual([row["action"] for row in audit], ["finish"])
        self.assertEqual(self.store.active_counts().get("tool", 0), 0)

    def test_finish_uncertain_keeps_the_permit_isolated(self):
        permit = self.store.try_acquire(
            "tool", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertTrue(
            self.store.finish(
                str(permit), outcome="uncertain", reason="transport interrupted"
            )
        )
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["tool"]["uncertain"], 1)
        self.assertEqual(snapshot["tool"]["active"], 0)
        # Escalation and evidence-backed recovery remain the only exits.
        self.assertTrue(
            self.store.release_uncertain(
                str(permit), evidence="operator_verified_cancel", operator="ops"
            )
        )

    def test_governor_reconcile_releases_uncertain_permit(self):
        from src.core.concurrency import ResourceGovernor

        permit = self.store.try_acquire(
            "rag", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        _expire(self.store, permit)
        self.store.reap_expired(self.store._connect())
        governor = ResourceGovernor(scope="deployment", store=self.store, limits={"rag": (1, 0)})
        self.assertTrue(
            governor.reconcile(
                str(permit), evidence="downstream_completed", reason="confirmed", operator="ops"
            )
        )
        self.assertEqual(self.store.snapshot()["rag"]["uncertain"], 0)

    def test_recovery_tool_cli_paths(self):
        output = os.path.join(self.tmp.name, "reconcile.json")
        self.assertEqual(recovery_main(["--db", self.path, "reconcile", "--output", output]), 0)
        self.assertTrue(os.path.isfile(output))
        permit = self.store.try_acquire(
            "memory", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        _expire(self.store, permit)
        self.store.reap_expired(self.store._connect())
        # Unknown evidence is rejected by argparse choices.
        with self.assertRaises(SystemExit):
            recovery_main(
                ["--db", self.path, "release", "--permit-id", str(permit), "--evidence", "client_timeout"]
            )
        self.assertEqual(
            recovery_main(
                [
                    "--db",
                    self.path,
                    "release",
                    "--permit-id",
                    str(permit),
                    "--evidence",
                    "server_deadline_passed",
                    "--operator",
                    "ops",
                ]
            ),
            0,
        )
        # Releasing an already released permit reports failure but stays safe.
        self.assertEqual(
            recovery_main(
                [
                    "--db",
                    self.path,
                    "release",
                    "--permit-id",
                    str(permit),
                    "--evidence",
                    "server_deadline_passed",
                ]
            ),
            1,
        )

    def test_deployment_identity_mismatch_fails_fast(self):
        with self.assertRaises(RuntimeError):
            SqliteQuotaStore(self.path, lease_seconds=30, deployment_id="other")

    def test_require_preconfigured_refuses_missing_database(self):
        missing = os.path.join(self.tmp.name, "missing", "quotas.db")
        with self.assertRaises(RuntimeError):
            SqliteQuotaStore(missing, lease_seconds=30, require_preconfigured=True)


if __name__ == "__main__":
    unittest.main()
