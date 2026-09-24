"""Real-PostgreSQL tests for the control-plane schema and loader (v3 PR-2).

Requires ``HDB_TEST_POSTGRES_DSN`` (a disposable PostgreSQL instance).  Each
test resets the ``public`` schema first, which also rehearses the rollback
path: a failed migration is recovered by dropping the target and re-running
from the frozen snapshot.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row

import src.settings
from src.core.conversation import ConversationService
from src.storage.control_plane import (
    SCHEMA_VERSION,
    apply_schema,
    assert_schema_compatible,
    load_sqlite_snapshot,
    record_schema_version,
)

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    return make_conninfo(**params)


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")


def _scalar(conn, sql: str, params: tuple = ()):
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def _seed_users(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                department_id INTEGER
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, department_id) VALUES (1, 'u1', NULL)"
        )
        conn.commit()


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class ControlPlanePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_cp_test_{uuid.uuid4().hex[:8]}"
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
        with _connect(self.dsn) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _snapshot(self, *, bad_event_json: bool = False) -> str:
        """Build a realistic frozen snapshot (no service writes afterwards)."""
        db_path = os.path.join(self.tmp.name, f"auth_{uuid.uuid4().hex[:6]}.db")
        _seed_users(db_path)
        original = src.settings.AUTH_DB_PATH
        original_serialize = src.settings.CONVERSATION_SERIALIZE_SAME_THREAD
        src.settings.AUTH_DB_PATH = db_path
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        try:
            conv = ConversationService(db_path)
            session = conv.create_session(1, "__general__")
            conv.create_session(1, "__general__")  # no execution_state row on purpose
            turn = conv.create_turn(user_id=1, session_id=session.id, query="q1")
            claimed = conv.claim_turn(1, turn.id, "worker-a")
            conv.complete_turn_result(
                1,
                turn.id,
                "answer",
                {},
                worker_id="worker-a",
                fencing_token=claimed.fencing_token,
                expected_generation=claimed.session_generation,
                terminal_events_builder=lambda completed: [
                    ("done", {"answer": completed.answer}),
                    ("turn_end", {"status": "completed"}),
                ],
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS app_settings ("
                    " key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings VALUES"
                    " ('theme', 'dark', '2026-09-17T00:00:00+00:00')"
                )
                # memory_jobs is created by ConversationService._init_db; these
                # partial unique indexes must survive the schema translation.
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_jobs_user_consent"
                    " ON memory_jobs(consent_event_id) WHERE consent_event_id IS NOT NULL"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_jobs_project_session"
                    " ON memory_jobs(session_id) WHERE job_kind = 'project_reflection'"
                )
                if bad_event_json:
                    conn.execute("UPDATE chat_turn_events SET payload_json = '{bad'")
                conn.commit()
        finally:
            src.settings.AUTH_DB_PATH = original
            src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = original_serialize
        return db_path

    # -- tests --------------------------------------------------------------

    def test_schema_apply_is_idempotent_and_versioned(self):
        with _connect(self.dsn) as conn:
            apply_schema(conn, include_indexes=True)
            apply_schema(conn, include_indexes=True)
            record_schema_version(conn)
            self.assertEqual(assert_schema_compatible(conn), SCHEMA_VERSION)
            conn.execute(
                "UPDATE control_plane_schema SET value = '99' WHERE key = 'schema_version'"
            )
            with self.assertRaises(RuntimeError):
                assert_schema_compatible(conn)

    def test_core_constraints_reject_duplicates_and_bad_status(self):
        with _connect(self.dsn) as conn:
            apply_schema(conn, include_indexes=True)
            conn.execute(
                "INSERT INTO chat_sessions (user_id, kb_name, title, created_at, updated_at)"
                " VALUES (1, 'kb', 't', now(), now())"
            )
            session_id = _scalar(conn, "SELECT MAX(id) FROM chat_sessions")
            conn.execute(
                "INSERT INTO chat_turns (id, session_id, user_message_id, assistant_message_id,"
                " kb_name, query, status, client_request_id, turn_sequence, created_at)"
                " VALUES ('t1', %s, 0, 0, 'kb', 'q', 'pending', 'req-1', 1, now())",
                (session_id,),
            )
            conn.execute(
                "INSERT INTO chat_turn_events (turn_id, seq, event_type, payload_json, created_at)"
                " VALUES ('t1', 1, 'delta', '{}'::jsonb, now())"
            )
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO chat_turn_events (turn_id, seq, event_type, payload_json, created_at)"
                    " VALUES ('t1', 1, 'delta', '{}'::jsonb, now())"
                )
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO chat_turns (id, session_id, user_message_id, assistant_message_id,"
                    " kb_name, query, status, turn_sequence, created_at)"
                    " VALUES ('t2', %s, 0, 0, 'kb', 'q', 'bogus', 2, now())",
                    (session_id,),
                )
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO chat_turns (id, session_id, user_message_id, assistant_message_id,"
                    " kb_name, query, status, client_request_id, turn_sequence, created_at)"
                    " VALUES ('t3', %s, 0, 0, 'kb', 'q', 'pending', 'req-1', 3, now())",
                    (session_id,),
                )

    def test_loader_round_trip_backfills_state_and_sets_sequences(self):
        snapshot = self._snapshot()
        with _connect(self.dsn) as conn:
            report = load_sqlite_snapshot(snapshot, conn, batch_size=50)
            self.assertTrue(report.ok, report.failures)
            # The loader records the schema version so readiness can assert it.
            self.assertEqual(assert_schema_compatible(conn), SCHEMA_VERSION)
            self.assertEqual(_scalar(conn, "SELECT COUNT(*) FROM chat_sessions"), 2)
            self.assertEqual(_scalar(conn, "SELECT COUNT(*) FROM chat_turns"), 1)
            self.assertEqual(_scalar(conn, "SELECT COUNT(*) FROM chat_turn_events"), 2)
            self.assertEqual(_scalar(conn, "SELECT COUNT(*) FROM conversation_execution_state"), 2)
            self.assertEqual(report.checks["execution_state_backfilled"], 1)
            conn.execute(
                "INSERT INTO chat_sessions (user_id, kb_name, title, created_at, updated_at)"
                " VALUES (1, 'kb', 'next', now(), now())"
            )
            self.assertGreater(_scalar(conn, "SELECT MAX(id) FROM chat_sessions"), 2)
            self.assertEqual(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM pg_indexes"
                    " WHERE indexname = 'ux_chat_turns_session_sequence'",
                ),
                1,
            )
            self.assertEqual(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM pg_constraint WHERE conname = 'fk_chat_turns_session'",
                ),
                1,
            )
            self.assertEqual(_scalar(conn, "SELECT COUNT(*) FROM app_settings"), 1)
            self.assertEqual(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'ux_users_username'",
                ),
                1,
            )
            # Registered partial indexes are recreated with their predicate, so
            # uniqueness keeps applying only to the matching rows.
            predicate = _scalar(
                conn,
                "SELECT indpred IS NOT NULL FROM pg_index"
                " WHERE indexrelid = 'uq_memory_jobs_user_consent'::regclass",
            )
            self.assertTrue(predicate)
            predicate = _scalar(
                conn,
                "SELECT indpred IS NOT NULL FROM pg_index"
                " WHERE indexrelid = 'uq_memory_jobs_project_session'::regclass",
            )
            self.assertTrue(predicate)

    def test_loader_fails_closed_on_invalid_json(self):
        snapshot = self._snapshot(bad_event_json=True)
        with _connect(self.dsn) as conn:
            report = load_sqlite_snapshot(snapshot, conn, batch_size=50)
            self.assertFalse(report.ok)
            failed = [entry for entry in report.tables if entry.table == "chat_turn_events"]
            self.assertEqual(failed[0].status, "error")
            self.assertIn("chat_turn_events", " ".join(report.failures))

    def test_dry_run_reports_without_writing(self):
        snapshot = self._snapshot()
        with _connect(self.dsn) as conn:
            report = load_sqlite_snapshot(snapshot, conn, dry_run=True)
            self.assertTrue(report.ok)
            self.assertTrue(all(entry.status == "dry_run" for entry in report.tables))
            self.assertEqual(
                _scalar(conn, "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public'"), 0
            )

    def test_rehearsal_cycle_is_repeatable_after_schema_drop(self):
        snapshot = self._snapshot()
        for _ in range(2):
            with _connect(self.dsn) as conn:
                report = load_sqlite_snapshot(snapshot, conn, batch_size=50)
                self.assertTrue(report.ok, report.failures)
                self.assertEqual(_scalar(conn, "SELECT COUNT(*) FROM chat_turns"), 1)
            with _connect(self.dsn) as conn:
                conn.execute("DROP SCHEMA public CASCADE")
                conn.execute("CREATE SCHEMA public")
                with self.assertRaises(RuntimeError):
                    assert_schema_compatible(conn)


if __name__ == "__main__":
    unittest.main()
