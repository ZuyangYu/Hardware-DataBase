"""Memory-ledger enqueue parity: PostgreSQL twin vs SQLite original (v3 §5).

Requires ``HDB_TEST_POSTGRES_DSN``.  Both sides run the same watermark/force
scenario and must end with the same job state (ids differ only by uuid).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timezone

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

import src.settings
from src.core.conversation import ConversationService
from src.memory.jobs import enqueue_project_reflection_in_connection
from src.storage.control_plane import apply_schema, load_sqlite_snapshot
from src.storage.control_plane.memory_jobs import enqueue_project_reflection

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    return make_conninfo(**params)


def _seed_users(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                department_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, department_id, is_active)"
            " VALUES (1, 'u1', NULL, 1)"
        )
        conn.commit()


def _normalize(row: dict) -> dict:
    normalized: dict = {}
    for key, value in row.items():
        if key in {"job_id", "requested_at"}:
            continue
        if isinstance(value, datetime):
            moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            normalized[key] = moment.astimezone(timezone.utc).isoformat()
        elif isinstance(value, bool):
            normalized[key] = int(value)
        else:
            normalized[key] = value
    return normalized


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class MemoryEnqueueParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_mem_{uuid.uuid4().hex[:8]}"
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

    def _fixture(self) -> tuple[ConversationService, str, int]:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = os.path.join(self.tmp.name, "auth.db")
        _seed_users(db_path)
        original_db = src.settings.AUTH_DB_PATH
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", original_db)
        src.settings.AUTH_DB_PATH = db_path
        service = ConversationService(db_path)
        session = service.create_session(1, "__general__")
        return service, db_path, session.id

    def _load(self, db_path: str) -> None:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn)
            report = load_sqlite_snapshot(db_path, conn, batch_size=50)
            self.assertTrue(report.ok, report.failures)

    def test_watermark_and_force_semantics_match(self):
        service, db_path, session_id = self._fixture()
        self._load(db_path)

        first_available = "2026-01-01T00:00:05+00:00"
        newer_available = "2026-01-01T00:00:09+00:00"
        older_available = "2026-01-01T00:00:07+00:00"
        forced_available = "2026-01-01T00:00:11+00:00"

        with closing(service._connect()) as sqlite_conn:
            created = enqueue_project_reflection_in_connection(
                sqlite_conn,
                session_id=session_id,
                scope_fingerprint="project:fp",
                target_turn_id="turn-1",
                target_message_id=5,
                available_at=first_available,
            )
            sqlite_conn.commit()
            advanced = enqueue_project_reflection_in_connection(
                sqlite_conn,
                session_id=session_id,
                scope_fingerprint="project:fp",
                target_turn_id="turn-2",
                target_message_id=9,
                available_at=newer_available,
                source_start_turn_id="turn-1",
                source_start_message_id=5,
            )
            sqlite_conn.commit()
            stale = enqueue_project_reflection_in_connection(
                sqlite_conn,
                session_id=session_id,
                scope_fingerprint="project:fp",
                target_turn_id="turn-1b",
                target_message_id=7,
                available_at=older_available,
            )
            sqlite_conn.commit()
            forced = enqueue_project_reflection_in_connection(
                sqlite_conn,
                session_id=session_id,
                scope_fingerprint="project:fp",
                target_turn_id="turn-1",
                target_message_id=5,
                available_at=forced_available,
                force=True,
            )
            sqlite_conn.commit()
            sqlite_row = sqlite_conn.execute(
                "SELECT * FROM memory_jobs WHERE session_id = ? AND job_kind = 'project_reflection'",
                (session_id,),
            ).fetchone()
            sqlite_job = dict(sqlite_row) if sqlite_row is not None else {}

        with psycopg.connect(
            self.dsn, autocommit=False, options="-c TimeZone=UTC", row_factory=dict_row
        ) as pg_conn:
            with pg_conn.transaction():
                pg_created = enqueue_project_reflection(
                    pg_conn,
                    session_id=session_id,
                    scope_fingerprint="project:fp",
                    target_turn_id="turn-1",
                    target_message_id=5,
                    available_at=first_available,
                )
                pg_advanced = enqueue_project_reflection(
                    pg_conn,
                    session_id=session_id,
                    scope_fingerprint="project:fp",
                    target_turn_id="turn-2",
                    target_message_id=9,
                    available_at=newer_available,
                    source_start_turn_id="turn-1",
                    source_start_message_id=5,
                )
                pg_stale = enqueue_project_reflection(
                    pg_conn,
                    session_id=session_id,
                    scope_fingerprint="project:fp",
                    target_turn_id="turn-1b",
                    target_message_id=7,
                    available_at=older_available,
                )
                pg_forced = enqueue_project_reflection(
                    pg_conn,
                    session_id=session_id,
                    scope_fingerprint="project:fp",
                    target_turn_id="turn-1",
                    target_message_id=5,
                    available_at=forced_available,
                    force=True,
                )
                pg_row = pg_conn.execute(
                    "SELECT * FROM memory_jobs WHERE session_id = %s AND job_kind = 'project_reflection'",
                    (session_id,),
                ).fetchone()

        # One project job per session: every call returns the same job id and
        # only the watermark/generation move.
        self.assertEqual(created, advanced)
        self.assertEqual(created, stale)
        self.assertEqual(created, forced)
        self.assertEqual(pg_created, pg_advanced)
        self.assertEqual(pg_created, pg_stale)
        self.assertEqual(pg_created, pg_forced)

        normalized_pg = _normalize(dict(pg_row))
        normalized_sqlite = _normalize(dict(sqlite_job))
        self.assertEqual(normalized_pg, normalized_sqlite)
        self.assertEqual(normalized_pg["generation"], 3)
        self.assertEqual(normalized_pg["target_turn_id"], "turn-1")
        self.assertEqual(normalized_pg["target_message_id"], 5)
        self.assertEqual(normalized_pg["source_start_turn_id"], "turn-1")
        self.assertEqual(normalized_pg["source_start_message_id"], 5)
        self.assertEqual(normalized_pg["status"], "pending")
        self.assertEqual(
            normalized_pg["available_at"], "2026-01-01T00:00:11+00:00"
        )
        self.assertIsNone(normalized_pg["lease_owner"])
        self.assertIsNone(normalized_pg["lease_expires_at"])


if __name__ == "__main__":
    unittest.main()
