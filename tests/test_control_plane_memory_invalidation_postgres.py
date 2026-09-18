"""Memory invalidation parity: PostgreSQL twin vs SQLite original (v3 §5).

Requires ``HDB_TEST_POSTGRES_DSN``.  Both sides seed the same three memory
records (candidate with a surviving source, verified, user-scope), one active
projection each and a pending project job; the invalidation results must match
field by field, and a second run must be a no-op on both.
"""

from __future__ import annotations

import json
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
from src.memory.jobs import (
    enqueue_project_reflection_in_connection,
    invalidate_message_memory_in_connection,
    invalidate_session_memory_in_connection,
)
from src.storage.control_plane import apply_schema, load_sqlite_snapshot
from src.storage.control_plane.memory_invalidation import (
    invalidate_message_memory,
    invalidate_session_memory,
)

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")
NOW = "2026-01-01T00:00:00+00:00"

MEMORIES = (
    ("mem-candidate", "project", "candidate", "candidate"),
    ("mem-verified", "project", "verified", "verified"),
    ("mem-user", "user", "candidate", "candidate"),
)

RECORDS_SQL = (
    "SELECT memory_id, scope, status, title, memory_type, subject, content_hash,"
    " (deleted_at IS NOT NULL) AS deleted FROM memory_records ORDER BY memory_id"
)
SOURCES_SQL = (
    "SELECT source_id, memory_id, message_id, source_valid,"
    " (invalidated_at IS NOT NULL) AS invalidated FROM memory_sources ORDER BY source_id"
)
PROJECTIONS_SQL = (
    "SELECT projection_id, memory_id, active, manager_writable, fence_version,"
    " (retired_at IS NOT NULL) AS retired FROM memory_projections ORDER BY projection_id"
)
OUTBOX_SQL = (
    "SELECT memory_id, operation, expected_revision, expected_fence, idempotency_key"
    " FROM memory_deletion_outbox ORDER BY memory_id, idempotency_key"
)
AUDIT_SQL = (
    # NULLS-first differs between engines; order portable via the predicate.
    "SELECT memory_id, event_type, metadata_json FROM memory_audit_events"
    " ORDER BY memory_id IS NULL DESC, memory_id, event_type"
)
JOBS_SQL = (
    "SELECT session_id, status, generation, target_message_id, last_error,"
    " (lease_owner IS NULL) AS lease_clear FROM memory_jobs"
)
RUNS_SQL = (
    "SELECT run_id, job_id, status, (output_payload_json IS NULL) AS scrubbed,"
    " (encrypted_source_snapshot_ref IS NULL) AS ref_cleared"
    " FROM memory_reflection_runs ORDER BY run_id"
)
CONSENT_SQL = (
    "SELECT consent_event_id, session_id, (revoked_at IS NOT NULL) AS revoked"
    " FROM memory_consent_events ORDER BY consent_event_id"
)


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


def _canon(value):
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    return value


def _normalize(rows: list[dict]) -> list[dict]:
    return [
        {
            key: _canon(value)
            for key, value in row.items()
            if key not in {"job_id", "created_at"}
        }
        for row in rows
    ]


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class MemoryInvalidationParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_meminv_{uuid.uuid4().hex[:8]}"
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

    def _fixture(self) -> tuple[str, int]:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = os.path.join(self.tmp.name, "auth.db")
        _seed_users(db_path)
        original_db = src.settings.AUTH_DB_PATH
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", original_db)
        src.settings.AUTH_DB_PATH = db_path
        service = ConversationService(db_path)
        session = service.create_session(1, "__general__")
        return db_path, session.id

    def _seed_sqlite(self, db_path: str, session_id: int) -> None:
        with closing(sqlite3.connect(db_path)) as conn:
            for memory_id, scope, status, kind in MEMORIES:
                conn.execute(
                    """
                    INSERT INTO memory_records
                        (memory_id, scope, user_id, status, current_revision,
                         content_hash, memory_type, title, subject, content_json,
                         schema_version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, 'fact', ?, NULL, '{"v": 1}', '1', ?, ?)
                    """,
                    (
                        memory_id,
                        scope,
                        "u1",
                        status,
                        f"hash-{memory_id}",
                        f"title-{memory_id}",
                        NOW,
                        NOW,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO memory_sources
                        (source_id, memory_id, session_id, message_id, source_hash,
                         source_valid, created_at)
                    VALUES (?, ?, ?, 7, ?, 1, ?)
                    """,
                    (f"src-{memory_id}-7", memory_id, session_id, f"sh-{memory_id}", NOW),
                )
                conn.execute(
                    """
                    INSERT INTO memory_projections
                        (projection_id, memory_id, projection_kind, store_backend,
                         namespace_json, store_key, current_revision,
                         current_content_hash, active, manager_writable,
                         fence_version, created_at)
                    VALUES (?, ?, ?, 'langmem', '{}', ?, 1, ?, 1, 1, 1, ?)
                    """,
                    (
                        f"proj-{memory_id}",
                        memory_id,
                        kind,
                        f"key-{memory_id}",
                        f"hash-{memory_id}",
                        NOW,
                    ),
                )
            conn.execute(
                """
                INSERT INTO memory_sources
                    (source_id, memory_id, session_id, message_id, source_hash,
                     source_valid, created_at)
                VALUES ('src-mem-candidate-8', 'mem-candidate', ?, 8, 'sh-extra', 1, ?)
                """,
                (session_id, NOW),
            )
            enqueue_project_reflection_in_connection(
                conn,
                session_id=session_id,
                scope_fingerprint="project:fp",
                target_turn_id="turn-1",
                target_message_id=7,
                available_at="2026-01-01T00:00:10+00:00",
            )
            job_id = conn.execute(
                "SELECT job_id FROM memory_jobs WHERE session_id = ?" " AND job_kind = 'project_reflection'",
                (session_id,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO memory_consent_events
                    (consent_event_id, user_id, session_id, turn_id, message_id,
                     consent_kind, policy_version, consent_revoke_generation,
                     granted_at, authorized_start_turn_id,
                     authorized_start_message_id, authorized_end_turn_id,
                     authorized_end_message_id, authorized_source_hash, created_at)
                VALUES ('consent-1', 'u1', ?, 'turn-1', 7, 'user_memory_extract',
                        '1', 0, ?, 'turn-1', 7, 'turn-1', 7, 'ash', ?)
                """,
                (session_id, NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO memory_reflection_runs
                    (run_id, job_id, generation, job_kind, run_key,
                     source_snapshot_hash, encrypted_source_snapshot_ref,
                     output_payload_json, status, created_at)
                VALUES ('run-1', ?, 1, 'project_reflection', 'run-key-1', 'snap',
                        'enc', '{"x": 1}', 'prepared', ?)
                """,
                (job_id, NOW),
            )
            conn.execute(
                """
                INSERT INTO memory_reflection_runs
                    (run_id, job_id, generation, job_kind, consent_event_id, run_key,
                     source_snapshot_hash, encrypted_source_snapshot_ref,
                     output_payload_json, status, created_at)
                VALUES ('run-2', ?, 1, 'user_reflection', 'consent-1',
                        'run-key-2', 'snap', 'enc', '{"y": 2}', 'prepared', ?)
                """,
                (job_id, NOW),
            )
            conn.commit()

    def _load(self, db_path: str) -> None:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn)
            report = load_sqlite_snapshot(db_path, conn, batch_size=50)
            self.assertTrue(report.ok, report.failures)

    def _snapshot(self, execute) -> dict[str, list[dict]]:
        return {
            "records": _normalize(execute(RECORDS_SQL)),
            "sources": _normalize(execute(SOURCES_SQL)),
            "projections": _normalize(execute(PROJECTIONS_SQL)),
            "outbox": _normalize(execute(OUTBOX_SQL)),
            "audit": _normalize(execute(AUDIT_SQL)),
            "jobs": _normalize(execute(JOBS_SQL)),
            "runs": _normalize(execute(RUNS_SQL)),
            "consent": _normalize(execute(CONSENT_SQL)),
        }

    def test_invalidation_classification_and_outbox_match(self):
        db_path, session_id = self._fixture()
        self._seed_sqlite(db_path, session_id)
        self._load(db_path)
        # The load copied the seeded pre-invalidation rows verbatim, so both
        # sides start from the same state before invalidating message 7.

        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            sqlite_first = invalidate_message_memory_in_connection(
                conn, session_id, 7, reason="message_edit"
            )
            conn.commit()
            sqlite_second = invalidate_message_memory_in_connection(
                conn, session_id, 7, reason="message_edit"
            )
            conn.commit()

            def _sqlite_execute(sql):
                return [dict(row) for row in conn.execute(sql).fetchall()]

            sqlite_state = self._snapshot(_sqlite_execute)

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC", row_factory=dict_row
        ) as conn:
            pg_first = invalidate_message_memory(
                conn, session_id, 7, reason="message_edit"
            )
            pg_second = invalidate_message_memory(
                conn, session_id, 7, reason="message_edit"
            )

            def _pg_execute(sql):
                return [dict(row) for row in conn.execute(sql).fetchall()]

            pg_state = self._snapshot(_pg_execute)

        self.assertEqual(pg_first, sqlite_first)
        self.assertEqual(pg_first, 3)
        self.assertEqual(pg_second, 0)
        self.assertEqual(sqlite_second, 0)

        for section, expected in sqlite_state.items():
            self.assertEqual(
                pg_state[section], expected, f"section={section}"
            )

        statuses = {row["memory_id"]: row["status"] for row in pg_state["records"]}
        self.assertEqual(statuses["mem-candidate"], "needs_rebuild")
        self.assertEqual(statuses["mem-verified"], "provenance_missing")
        self.assertEqual(statuses["mem-user"], "deleted")
        self.assertEqual(len(pg_state["outbox"]), 3)
        self.assertEqual(len(pg_state["audit"]), 3)
        self.assertEqual(pg_state["jobs"][0]["status"], "cancelled")
        self.assertEqual(pg_state["jobs"][0]["generation"], 2)

    def test_session_invalidation_revokes_consent_and_scrubs_runs(self):
        db_path, session_id = self._fixture()
        self._seed_sqlite(db_path, session_id)
        self._load(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            sqlite_first = invalidate_session_memory_in_connection(
                conn, session_id, reason="session_deleted"
            )
            conn.commit()
            sqlite_second = invalidate_session_memory_in_connection(
                conn, session_id, reason="session_deleted"
            )
            conn.commit()
            sqlite_state = self._snapshot(
                lambda sql: [dict(row) for row in conn.execute(sql).fetchall()]
            )

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC", row_factory=dict_row
        ) as conn:
            pg_first = invalidate_session_memory(
                conn, session_id, reason="session_deleted"
            )
            pg_second = invalidate_session_memory(
                conn, session_id, reason="session_deleted"
            )
            pg_state = self._snapshot(
                lambda sql: [dict(row) for row in conn.execute(sql).fetchall()]
            )

        self.assertEqual(pg_first, sqlite_first)
        self.assertEqual(pg_first, 3)
        self.assertEqual(pg_second, 0)
        self.assertEqual(sqlite_second, 0)
        for section, expected in sqlite_state.items():
            self.assertEqual(
                pg_state[section], expected, f"section={section}"
            )

        self.assertTrue(all(row["revoked"] for row in pg_state["consent"]))
        self.assertTrue(all(row["scrubbed"] for row in pg_state["runs"]))
        self.assertTrue(all(row["ref_cleared"] for row in pg_state["runs"]))
        self.assertEqual(
            sorted(row["memory_id"] for row in pg_state["records"]), ["mem-candidate", "mem-user", "mem-verified"]
        )
        statuses = {row["memory_id"]: row["status"] for row in pg_state["records"]}
        self.assertEqual(statuses["mem-candidate"], "deleted")
        self.assertEqual(statuses["mem-verified"], "provenance_missing")
        self.assertEqual(statuses["mem-user"], "deleted")
        audit_types = sorted(row["event_type"] for row in pg_state["audit"])
        self.assertEqual(audit_types.count("consent_revoked"), 1)
        self.assertEqual(audit_types.count("source_invalidated"), 3)


if __name__ == "__main__":
    unittest.main()
