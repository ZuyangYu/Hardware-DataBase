"""End-to-end lifecycle of the PostgreSQL conversation facade (v3 PR-3).

Requires ``HDB_TEST_POSTGRES_DSN``.  Exercises the same public API the API and
worker call (create/claim/events/cancel/complete/history/queue/checkpoint
pointers) against a real database, with no SQLite involved.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings
from src.core.conversation import ConversationService, StaleTurnOwner
from src.storage.control_plane import apply_schema, load_sqlite_snapshot
from src.storage.control_plane.conversation_service import PostgresConversationService

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


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class ConversationFacadeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_facade_{uuid.uuid4().hex[:8]}"
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
        # Provision the same schema a real cutover produces: a SQLite snapshot
        # migrated with the loader (this also brings the memory tables the
        # invalidation paths require).
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = os.path.join(self.tmp.name, "auth.db")
        _seed_users(db_path)
        original_db = src.settings.AUTH_DB_PATH
        original_serialize = src.settings.CONVERSATION_SERIALIZE_SAME_THREAD
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", original_db)
        self.addCleanup(
            setattr,
            src.settings,
            "CONVERSATION_SERIALIZE_SAME_THREAD",
            original_serialize,
        )
        src.settings.AUTH_DB_PATH = db_path
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        ConversationService(db_path)
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn)
            report = load_sqlite_snapshot(db_path, conn, batch_size=50)
            self.assertTrue(report.ok, report.failures)
        self.service = PostgresConversationService(self.dsn, pool_size=2)
        self.addCleanup(self.service.close)

    def test_only_single_kb_completion_enters_project_memory_outbox(self):
        from tests.test_multi_kb_store_contract import snapshot

        with patch.multiple(src.settings, MEMORY_ENABLED=True, MEMORY_EXTRACTION_ENABLED=True,
                            RESULT_EXPORT_ENABLED=False):
            for multi_kb in (False, True):
                with self.subTest(multi_kb=multi_kb):
                    scope = snapshot(1) if multi_kb else None
                    session = self.service.create_session(
                        1, "KB101", department_id=7, kb_id=101,
                        knowledge_selection=scope.selection() if scope else None,
                    )
                    turn = self.service.create_turn(1, session.id, "compare supplies", knowledge_scope=scope)
                    claimed = self.service.claim_turn_result(1, turn.id, "memory-isolation-worker")
                    self.assertEqual(claimed.status, "claimed")
                    completed = self.service.complete_turn(
                        1, turn.id, "answer", {"status": "success"},
                        worker_id="memory-isolation-worker", fencing_token=claimed.turn.fencing_token,
                        expected_generation=claimed.turn.session_generation,
                    )
                    self.assertEqual(completed.answer, "answer")
                    with psycopg.connect(self.dsn) as conn:
                        count = conn.execute(
                            "SELECT COUNT(*) FROM memory_jobs WHERE session_id = %s AND job_kind = 'project_reflection'",
                            (session.id,),
                        ).fetchone()[0]
                    self.assertEqual(count, 0 if multi_kb else 1)

    def test_full_turn_lifecycle_on_postgres(self):
        service = self.service
        session = service.create_session(1, "__general__")
        self.assertIsNotNone(service.get_session(1, session.id))
        self.assertIsNone(service.get_session(2, session.id))

        first = service.create_turn(1, session.id, "第一问")
        claimed = service.claim_turn_result(1, first.id, "worker-a")
        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(claimed.turn.fencing_token, 1)
        self.assertIsNotNone(service.get_turn(1, first.id))

        event = service.append_turn_event(
            first.id,
            "delta",
            {"text": "x"},
            worker_id="worker-a",
            fencing_token=claimed.turn.fencing_token,
        )
        self.assertEqual(event.seq, 1)
        self.assertTrue(
            service.touch_turn_worker(
                first.id, "worker-a", fencing_token=claimed.turn.fencing_token
            )
        )
        self.assertFalse(
            service.touch_turn_worker(first.id, "worker-a", fencing_token=999)
        )

        second = service.create_turn(1, session.id, "第二问")
        blocked = service.claim_turn_result(1, second.id, "worker-b")
        self.assertEqual(blocked.status, "blocked")
        depth, _age = service.pending_turn_queue_state()
        self.assertEqual(depth, 1)
        self.assertEqual(service.list_pending_turn_work(8), [])

        cancelled = service.request_turn_cancel(1, first.id)
        self.assertEqual(cancelled.status, "cancelled")
        with self.assertRaises(StaleTurnOwner):
            service.complete_turn_result(
                1,
                first.id,
                "不该提交",
                {},
                worker_id="worker-a",
                fencing_token=claimed.turn.fencing_token,
                expected_generation=claimed.turn.session_generation,
            )
        with self.assertRaises(StaleTurnOwner):
            service.fail_turn_result(
                1,
                first.id,
                "已停止生成",
                cancelled=True,
                worker_id="worker-a",
                fencing_token=claimed.turn.fencing_token,
                terminal_events_builder=lambda _turn: [
                    ("error", {"message": "已停止生成", "cancelled": True}),
                    ("turn_end", {"status": "cancelled"}),
                ],
            )
        self.assertEqual(
            [event.event_type for event in service.list_turn_events(1, first.id)],
            ["delta", "error", "turn_end"],
        )

        claimed_second = service.claim_turn_result(1, second.id, "worker-b")
        self.assertEqual(claimed_second.status, "claimed")

        ref = {"backend": "sqlite", "thread_id": f"{session.id}", "checkpoint_id": "cp-1"}
        self.assertTrue(
            service.update_recoverable_checkpoint_ref(
                second.id,
                "worker-b",
                claimed_second.turn.fencing_token,
                ref,
                expected_generation=claimed_second.turn.session_generation,
            )
        )
        self.assertEqual(
            service.get_checkpoint_refs(session.id)["recoverable"]["checkpoint_id"],
            "cp-1",
        )
        with self.assertRaises(StaleTurnOwner):
            service.update_recoverable_checkpoint_ref(second.id, "worker-b", 999, ref)
        completion = service.complete_turn_result(
            1,
            second.id,
            "第二答",
            {},
            worker_id="worker-b",
            fencing_token=claimed_second.turn.fencing_token,
            expected_generation=claimed_second.turn.session_generation,
            terminal_events_builder=lambda turn: [
                ("done", {"answer": turn.answer}),
                ("turn_end", {"status": "completed"}),
            ],
        )
        self.assertEqual(completion.turn.status, "completed")
        self.assertTrue(service.release_turn_ownership(
            second.id, "worker-b", fencing_token=claimed_second.turn.fencing_token
        ))

        third = service.create_turn(1, session.id, "第三问")
        self.assertEqual(
            service.history_before_turn(1, third.id), [("第二问", "第二答")]
        )
        messages = service.list_messages(1, session.id)
        self.assertEqual(
            [message.content for message in messages],
            ["第一问", "已停止生成", "第二问", "第二答", "第三问", ""],
        )


        service.clear_session(1, session.id)
        self.assertEqual(service.list_messages(1, session.id), [])
        self.assertEqual(service.list_turn_events(1, second.id), [])
        self.assertIsNone(service.get_checkpoint_refs(session.id)["recoverable"])

    def test_queue_timeout_and_session_delete(self):
        service = self.service
        session = service.create_session(1, "kb-x")
        turn = service.create_turn(1, session.id, "排队超时")
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute(
                "UPDATE chat_turns SET created_at = now() - interval '2 hours'"
                " WHERE id = %s",
                (turn.id,),
            )
        self.assertEqual(service.expire_queued_turns(max_queue_seconds=30), 1)
        self.assertEqual(service.get_turn(1, turn.id).status, "failed")

        self.assertTrue(service.delete_session(1, session.id))
        self.assertIsNone(service.get_session(1, session.id))
        self.assertFalse(service.delete_session(1, session.id))


if __name__ == "__main__":
    unittest.main()
