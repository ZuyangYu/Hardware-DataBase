"""G3: PostgreSQL deployment quota store (cross-host governor state).

Requires ``HDB_TEST_POSTGRES_DSN`` (a disposable PostgreSQL instance). The
class provisions its own database so tests never touch shared state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.core.concurrency.resource_governor import (
    GovernorRejected,
    PostgresQuotaStore,
    ResourceGovernor,
)

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    return make_conninfo(**params)




REPO_ROOT = Path(__file__).resolve().parents[1]


def _spawn(script: str, env_extra: dict[str, str]) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_ready(
    stop_file: str, procs: list[subprocess.Popen] | None = None, timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(stop_file):
        for proc in procs or []:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(
                    f"child exited rc={proc.returncode} before ready\nstdout={out}\nstderr={err}"
                )
        if time.monotonic() > deadline:
            raise TimeoutError("children never signalled ready")
        time.sleep(0.02)


def _finish(proc: subprocess.Popen, timeout: float = 60.0) -> dict:
    out, err = proc.communicate(timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"child failed rc={proc.returncode}\nstdout={out}\nstderr={err}")
    lines = [line for line in out.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise AssertionError(f"child produced no JSON payload\nstdout={out}\nstderr={err}")
    return json.loads(lines[-1])


_QUOTA_WORKER = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["PYTHONPATH"])
from src.core.concurrency.resource_governor import PostgresQuotaStore
store = PostgresQuotaStore(
    os.environ["HDB_TEST_PG_DSN"],
    deployment_id=os.environ["HDB_TEST_PG_DEPLOYMENT"],
    lease_seconds=30,
)
resource = os.environ["HDB_TEST_RESOURCE"]
capacity = int(os.environ["HDB_TEST_QUOTA_CAPACITY"])
worker = os.environ["HDB_TEST_WORKER"]
log_path = os.environ["HDB_TEST_LOG"]

def log(message):
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(message + "\n")

open(os.environ["HDB_TEST_READY"], "w").close()
while not os.path.exists(os.environ["HDB_TEST_GO"]):
    time.sleep(0.005)
deadline = time.monotonic() + 30
permit = None
while time.monotonic() < deadline:
    permit = store.try_acquire(
        resource,
        capacity=capacity,
        reserved_interactive=0,
        interactive=True,
        owner_id=worker,
        attempt_id="a",
    )
    if permit is not None:
        break
    time.sleep(0.01)
if permit is None:
    print(json.dumps({"worker": worker, "ok": False}))
    sys.exit(1)
log(f"start:{worker}")
time.sleep(0.15)
log(f"end:{worker}")
store.release(permit)
print(json.dumps({"worker": worker, "ok": True}))
"""


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class PostgresQuotaStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_quota_test_{uuid.uuid4().hex[:8]}"
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{cls.database}"')
        finally:
            admin.close()
        cls.dsn = _dsn_with_database(DSN, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{cls.database}" WITH (FORCE)')
        finally:
            admin.close()

    def setUp(self) -> None:
        for table in (
            "resource_permits",
            "resource_permit_audit",
            "resource_maintenance_lease",
            "resource_quotas",
            "resource_governor_meta",
        ):
            try:
                with psycopg.connect(self.dsn, autocommit=True) as conn:
                    conn.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
            except psycopg.errors.UndefinedTable:
                pass
        self.deployment = "hdb-test"
        self.store = PostgresQuotaStore(
            self.dsn, deployment_id=self.deployment, lease_seconds=30
        )

    def test_identity_mismatch_is_rejected(self):
        with self.assertRaises(RuntimeError):
            PostgresQuotaStore(self.dsn, deployment_id="other-deployment")

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
        permit = self.store.try_acquire(
            "llm", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertIsNotNone(permit)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE resource_permits SET expires_at = now() - interval '1 hour'"
                " WHERE permit_id = %s",
                (permit,),
            )
        replacement = self.store.try_acquire(
            "llm", capacity=1, reserved_interactive=0, interactive=True, owner_id="w2", attempt_id="a2"
        )
        self.assertIsNotNone(replacement)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["llm"]["active"], 1)
        self.assertEqual(snapshot["llm"]["uncertain"], 1)

    def test_expiry_uses_database_clock(self):
        permit = self.store.try_acquire(
            "llm", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            remaining = conn.execute(
                "SELECT expires_at - now() FROM resource_permits WHERE permit_id = %s",
                (permit,),
            ).fetchone()[0].total_seconds()
        # lease_seconds=30 in setUp; the remaining time must come from now().
        self.assertGreater(remaining, 25)
        self.assertLess(remaining, 35)

    def test_renew_extends_only_active_permits(self):
        permit = self.store.try_acquire(
            "tool", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertTrue(self.store.renew(str(permit)))
        self.store.release(str(permit))
        self.assertFalse(self.store.renew(str(permit)))

    def test_finish_requires_evidence_and_is_idempotent(self):
        permit = self.store.try_acquire(
            "export", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        with self.assertRaises(ValueError):
            self.store.finish(str(permit), outcome="completed", evidence="")
        self.assertTrue(
            self.store.finish(str(permit), outcome="completed", evidence="downstream_completed")
        )
        self.assertTrue(
            self.store.finish(str(permit), outcome="completed", evidence="downstream_completed")
        )
        audit = self.store.audit_entries(str(permit))
        self.assertEqual([entry["action"] for entry in audit], ["finish"])

    def test_release_uncertain_requires_recognized_evidence(self):
        permit = self.store.try_acquire(
            "ocr", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertTrue(self.store.finish(str(permit), outcome="uncertain", reason="lost worker"))
        with self.assertRaises(ValueError):
            self.store.release_uncertain(str(permit), evidence="restarted")
        self.assertTrue(
            self.store.release_uncertain(
                str(permit),
                evidence="operator_verified_restart",
                reason="worker restarted",
                operator="ops",
            )
        )
        self.assertFalse(
            self.store.release_uncertain(str(permit), evidence="operator_verified_restart")
        )
        self.assertEqual(self.store.active_counts().get("ocr", 0), 0)

    def test_maintenance_lease_single_owner_and_takeover(self):
        first = self.store.try_acquire_maintenance("maintainer-a", lease_seconds=30)
        self.assertEqual(first, 1)
        self.assertIsNone(self.store.try_acquire_maintenance("maintainer-b", lease_seconds=30))
        self.assertTrue(self.store.renew_maintenance("maintainer-a", first, lease_seconds=30))
        self.assertFalse(self.store.renew_maintenance("maintainer-a", int(first) + 1, lease_seconds=30))
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("UPDATE resource_maintenance_lease SET lease_expires_at = now() - interval '1 hour'")
        second = self.store.try_acquire_maintenance("maintainer-b", lease_seconds=30)
        self.assertEqual(second, 2, "takeover must bump the fencing token")
        self.assertFalse(self.store.renew_maintenance("maintainer-a", first))
        self.assertTrue(self.store.release_maintenance("maintainer-b", second))
        self.assertEqual(self.store.maintenance_snapshot()["owner_id"], "")

    def test_two_replicas_share_one_budget(self):
        store_a = PostgresQuotaStore(self.dsn, deployment_id=self.deployment, lease_seconds=30)
        store_b = PostgresQuotaStore(self.dsn, deployment_id=self.deployment, lease_seconds=30)
        permit = store_a.try_acquire(
            "heavy_job", capacity=1, reserved_interactive=0, interactive=True, owner_id="api-a", attempt_id="a1"
        )
        self.assertIsNotNone(permit)
        self.assertEqual(store_b.active_counts().get("heavy_job"), 1)
        self.assertIsNone(
            store_b.try_acquire(
                "heavy_job", capacity=1, reserved_interactive=0, interactive=True, owner_id="api-b", attempt_id="a2"
            )
        )
        store_b.release(str(permit))
        self.assertIsNotNone(
            store_b.try_acquire(
                "heavy_job", capacity=1, reserved_interactive=0, interactive=True, owner_id="api-b", attempt_id="a3"
            )
        )

    def test_deployment_budget_is_not_multiplied_by_process_limits(self):
        first = ResourceGovernor(
            scope="deployment", store=self.store, limits={"llm": (1, 0)}, queue_max_wait_seconds=0.2
        )
        permit = first.acquire("llm", interactive=True, timeout=0.2)
        second = ResourceGovernor(
            scope="deployment", store=self.store, limits={"llm": (5, 0)}, queue_max_wait_seconds=0.2
        )
        self.assertEqual(second.limits_for("llm"), (1, 0))
        with self.assertRaises(GovernorRejected):
            second.acquire("llm", interactive=True, timeout=0.2)
        permit.release()

    def test_escalated_uncertain_permit_blocks_admission_until_reconciled(self):
        permit = self.store.try_acquire(
            "memory", capacity=1, reserved_interactive=0, interactive=True, owner_id="w1", attempt_id="a1"
        )
        self.assertTrue(self.store.finish(str(permit), outcome="uncertain", reason="crash"))
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE resource_permits SET uncertain_since = now() - interval '1 hour'"
                " WHERE permit_id = %s",
                (permit,),
            )
        self.assertEqual(self.store.maintain(60)["escalated"], 1)
        self.assertIn("memory", self.store.escalated_resources())
        self.assertIsNone(
            self.store.try_acquire(
                "memory", capacity=1, reserved_interactive=0, interactive=True, owner_id="w2", attempt_id="a2"
            )
        )
        self.assertTrue(
            self.store.release_uncertain(
                str(permit), evidence="operator_verified_restart", operator="ops"
            )
        )
        self.assertNotIn("memory", self.store.escalated_resources())


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class PostgresQuotaMultiprocessTests(unittest.TestCase):
    """G3: cross-host budget must not be multiplied by process count."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_quota_mp_{uuid.uuid4().hex[:8]}"
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{cls.database}"')
        finally:
            admin.close()
        cls.dsn = _dsn_with_database(DSN, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{cls.database}" WITH (FORCE)')
        finally:
            admin.close()

    def test_capacity_two_permits_never_overlap_across_processes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = os.path.join(tmp.name, "holders.log")
        ready_dir = os.path.join(tmp.name, "ready")
        os.makedirs(ready_dir, exist_ok=True)
        go_file = os.path.join(tmp.name, "go")
        specs = []
        for index in range(4):
            ready_file = os.path.join(ready_dir, f"ready-{index}")
            proc = _spawn(
                _QUOTA_WORKER,
                {
                    "HDB_TEST_PG_DSN": self.dsn,
                    "HDB_TEST_PG_DEPLOYMENT": "hdb-mp-test",
                    "HDB_TEST_RESOURCE": "llm",
                    "HDB_TEST_QUOTA_CAPACITY": "2",
                    "HDB_TEST_WORKER": f"worker-{index}",
                    "HDB_TEST_LOG": log_path,
                    "HDB_TEST_READY": ready_file,
                    "HDB_TEST_GO": go_file,
                },
            )
            specs.append((proc, ready_file))
        self.addCleanup(lambda: [proc.kill() for proc, _ in specs if proc.poll() is None])
        for _, ready_file in specs:
            _wait_ready(ready_file, [proc for proc, _ in specs])
        open(go_file, "w").close()
        results = [_finish(proc) for proc, _ in specs]
        self.assertTrue(all(r["ok"] for r in results), results)
        events = [
            line.strip().split(":", 1)
            for line in Path(log_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(events), 8, events)
        active: set[str] = set()
        high_water = 0
        for action, worker in events:
            if action == "start":
                active.add(worker)
                high_water = max(high_water, len(active))
            else:
                active.discard(worker)
        self.assertLessEqual(high_water, 2, f"cross-host LLM total exceeded: {high_water}")
        self.assertEqual(active, set())


if __name__ == "__main__":
    unittest.main()
