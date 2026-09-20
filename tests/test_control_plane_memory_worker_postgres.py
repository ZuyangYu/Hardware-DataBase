"""PostgreSQL Memory Worker lease ownership (v3 §6.4 / §5.3)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.core.conversation import ConversationService
from src.storage.control_plane import apply_schema, load_sqlite_snapshot
from src.storage.control_plane.memory_worker import PostgresMemoryJobRepository


DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = name
    return make_conninfo(**values)


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class PostgresMemoryWorkerLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_memory_worker_{uuid.uuid4().hex[:8]}"
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sqlite_path = os.path.join(self.tmp.name, "auth.db")
        with closing(sqlite3.connect(self.sqlite_path)) as conn:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, department_id INTEGER, is_active INTEGER)"
            )
            conn.execute("INSERT INTO users VALUES (1, 'worker-test', NULL, 1)")
            conn.commit()
        service = ConversationService(self.sqlite_path)
        self.session = service.create_session(1, "__general__")
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn)
            report = load_sqlite_snapshot(self.sqlite_path, conn, batch_size=100)
            self.assertTrue(report.ok, report.failures)

    def _insert_pending_job(self, job_id: str) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO memory_jobs (
                    job_id, session_id, job_kind, scope_fingerprint,
                    target_turn_id, target_message_id, generation, status,
                    requested_at, available_at
                ) VALUES (%s, %s, 'project_reflection', 'project:worker',
                          'turn-worker', 1, 1, 'pending', now(), now())
                """,
                (job_id, self.session.id),
            )

    def test_ensure_memory_schema_never_runs_sqlite_ddl_on_postgres(self) -> None:
        # v3 §5.2: PG schema is versioned DDL only; the SQLite schema helper
        # must be a no-op on PG connections instead of issuing PRAGMA/CREATE.
        from src.memory.catalog import ensure_memory_schema

        repo = PostgresMemoryJobRepository(self.dsn)
        conn = repo._connect()
        try:
            ensure_memory_schema(conn)
            row = conn.execute(
                "SELECT to_regclass('public.memory_jobs') AS name"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["name"], "memory_jobs")

    def test_two_workers_cannot_claim_the_same_memory_job(self) -> None:
        job_id = f"memory-job-{uuid.uuid4().hex}"
        self._insert_pending_job(job_id)
        barrier = threading.Barrier(2)
        claims = []
        errors = []

        def claim(worker_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                claims.append(PostgresMemoryJobRepository(self.dsn).claim_next(worker_id))
            except Exception as exc:  # noqa: BLE001 - asserted after joining
                errors.append(exc)

        threads = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        claimed = [item for item in claims if item is not None]
        self.assertEqual(len(claimed), 1)
        owner = claimed[0]
        repo = PostgresMemoryJobRepository(self.dsn)
        self.assertTrue(repo.heartbeat(owner.job_id, owner.lease_token, owner.generation))
        self.assertTrue(repo.mark_completed(owner.job_id, owner.lease_token, owner.generation))


if __name__ == "__main__":
    unittest.main()
