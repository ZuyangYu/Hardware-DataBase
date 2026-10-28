"""Read-path parity: PostgreSQL store vs ConversationService on a loaded snapshot.

Requires ``HDB_TEST_POSTGRES_DSN``.  The fixture is built with the real
ConversationService, copied with the migration loader, and then both sides
answer the same read calls; fields must match so the API/worker can switch
backends without changing projections.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

import src.settings
from src.core.conversation import ConversationService
from src.memory.jobs import enqueue_project_reflection_in_connection
from src.storage.control_plane import apply_schema, load_sqlite_snapshot
from src.storage.control_plane.postgres import PostgresExecutionStore

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _canon(value):
    """Bool→int and JSON text/dict→canonical JSON for cross-engine comparison."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _normalize(rows: list[dict]) -> list[dict]:
    return [
        {key: _canon(value) for key, value in row.items()} for row in rows
    ]


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
class ReadParityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_parity_{uuid.uuid4().hex[:8]}"
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

    def _fixture(self) -> tuple[ConversationService, str, int, str, str]:
        """Returns (sqlite service, db path, session id, completed turn, pending turn)."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = os.path.join(self.tmp.name, "auth.db")
        _seed_users(db_path)
        original_db = src.settings.AUTH_DB_PATH
        original_serialize = src.settings.CONVERSATION_SERIALIZE_SAME_THREAD
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", original_db)
        self.addCleanup(
            setattr, src.settings, "CONVERSATION_SERIALIZE_SAME_THREAD", original_serialize
        )
        src.settings.AUTH_DB_PATH = db_path
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        service = ConversationService(db_path)
        session = service.create_session(1, "__general__")

        completed = service.create_turn(
            user_id=1,
            session_id=session.id,
            query="已完成的问题",
        )
        claimed = service.claim_turn(1, completed.id, "worker-a")
        service.complete_turn_result(
            1,
            completed.id,
            "完整回答",
            {
                "memory_context": [{"scope": "project", "count": 1}],
                "evidence": [
                    {
                        "id": "e-1",
                        "source_name": "manual.pdf",
                        "content_kind": "text",
                        "locator": {"page": 3},
                        "content": "证据正文预览",
                        "metadata": {"citation_number": 1},
                    }
                ],
            },
            footer="footer",
            worker_id="worker-a",
            fencing_token=claimed.fencing_token,
            expected_generation=claimed.session_generation,
            terminal_events_builder=lambda turn: [("done", {"answer": turn.answer})],
        )
        service.append_turn_event(
            completed.id,
            "document_card",
            {"card": {"kind": "attachment_revision", "status": "review_candidate"}},
        )
        pending = service.create_turn(user_id=1, session_id=session.id, query="排队中的问题")
        return service, db_path, session.id, completed.id, pending.id

    def _load_into_postgres(self, db_path: str) -> PostgresExecutionStore:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn)
            report = load_sqlite_snapshot(db_path, conn, batch_size=50)
            self.assertTrue(report.ok, report.failures)
        store = PostgresExecutionStore(self.dsn)
        self.addCleanup(store.close)
        return store

    def test_session_message_and_history_projections_match(self):
        service, db_path, session_id, completed_id, pending_id = self._fixture()
        store = self._load_into_postgres(db_path)

        sqlite_session = service.get_session(1, session_id)
        pg_session = store.get_session(1, session_id)
        assert pg_session is not None
        self.assertEqual(pg_session.id, sqlite_session.id)
        self.assertEqual(pg_session.user_id, sqlite_session.user_id)
        self.assertEqual(pg_session.kb_name, sqlite_session.kb_name)
        self.assertEqual(pg_session.title, sqlite_session.title)
        self.assertIsNone(store.get_session(2, session_id))

        sqlite_messages = service.list_messages(1, session_id)
        pg_messages = store.list_messages(1, session_id)
        self.assertEqual(len(pg_messages), len(sqlite_messages))
        for expected, actual in zip(sqlite_messages, pg_messages):
            self.assertEqual(actual.id, expected.id)
            self.assertEqual(actual.role, expected.role)
            self.assertEqual(actual.content, expected.content)
            self.assertEqual(actual.footer, expected.footer)
            self.assertEqual(actual.memory_context, expected.memory_context)
            self.assertEqual(actual.citations, expected.citations)
        self.assertEqual(store.list_messages(2, session_id), [])

        expected_history = service.history_before_turn(1, pending_id)
        self.assertEqual(store.history_before_turn(1, pending_id), expected_history)

        expected_active = [turn.id for turn in service.list_active_turns(1, session_id)]
        self.assertEqual(
            [turn.id for turn in store.list_active_turns(1, session_id)], expected_active
        )
        expected_recent = [turn.id for turn in service.list_recent_turns(1, session_id, limit=2)]
        self.assertEqual(
            [turn.id for turn in store.list_recent_turns(1, session_id, limit=2)],
            expected_recent,
        )
        expected_cards = service.list_session_document_cards(1, session_id)
        self.assertEqual(store.list_session_document_cards(1, session_id), expected_cards)

    def test_create_turn_full_semantics_and_attachment_wait_match(self):
        from types import SimpleNamespace
        from unittest import mock

        service, db_path, session_id, _completed_id, _pending_id = self._fixture()
        store = self._load_into_postgres(db_path)

        sqlite_turn = service.create_turn(
            1,
            session_id,
            "带上下文的问题",
            query_mode="deep",
            trace_context={"traceparent": "00-abc-def-01"},
            wiki_assist=True,
        )
        pg_turn = store.create_turn(
            1,
            session_id,
            "带上下文的问题",
            query_mode="deep",
            trace_context={"traceparent": "00-abc-def-01"},
            wiki_assist=True,
        )
        self.assertEqual(pg_turn.status, sqlite_turn.status)
        self.assertEqual(pg_turn.query_mode, sqlite_turn.query_mode)
        self.assertEqual(pg_turn.trace_context, sqlite_turn.trace_context)
        self.assertEqual(pg_turn.wiki_assist, sqlite_turn.wiki_assist)
        self.assertEqual(pg_turn.source_scope, sqlite_turn.source_scope)
        self.assertEqual(
            store.get_session(1, session_id).title,
            service.get_session(1, session_id).title,
        )
        with self.assertRaises(ValueError):
            store.create_turn(1, session_id, "   ")

        replay = store.create_turn(
            1,
            session_id,
            "带上下文的问题",
            client_request_id="ctx-replay",
        )
        replay_again = store.create_turn(
            1,
            session_id,
            "带上下文的问题",
            client_request_id="ctx-replay",
        )
        self.assertEqual(replay.id, replay_again.id)

        fake_ref = SimpleNamespace(
            attachment_id="att-9",
            asset_id="asset-9",
            filename="spec.xlsx",
            media_type="application/vnd.ms-excel",
            extension=".xlsx",
            size_bytes=10,
            sha256="sha-9",
            usage_hint="reference",
            parse_status="running",
            parser_version="v1",
            degraded_reason="",
        )

        class _FakeAttachmentService:
            def resolve_refs_for_turn(self, **_kwargs):
                return [fake_ref], []

        with mock.patch(
            "src.attachments.service.AttachmentService", _FakeAttachmentService
        ):
            sqlite_attached = service.create_turn(
                1, session_id, "带附件的问题", attachment_ids=["att-9"]
            )
            pg_attached = store.create_turn(
                1, session_id, "带附件的问题", attachment_ids=["att-9"]
            )
        self.assertEqual(pg_attached.status, "waiting_for_attachments")
        self.assertEqual(pg_attached.status, sqlite_attached.status)
        self.assertEqual(
            pg_attached.required_attachment_ids,
            sqlite_attached.required_attachment_ids,
        )
        self.assertEqual(
            [(ref["attachment_id"], ref["parse_status"]) for ref in pg_attached.attachments],
            [(ref["attachment_id"], ref["parse_status"]) for ref in sqlite_attached.attachments],
        )

        def _shape(snapshots: list[dict]) -> list[tuple]:
            return [
                (
                    snapshot["attachment_id"],
                    snapshot["filename_snapshot"],
                    snapshot["parse_status_snapshot"],
                )
                for snapshot in snapshots
            ]

        self.assertEqual(
            _shape(store.list_turn_attachments(pg_attached.id)),
            _shape(service.list_turn_attachments(sqlite_attached.id)),
        )

    def test_session_writes_messages_and_admission_counts_match(self):
        service, db_path, _session_id, _completed_id, pending_id = self._fixture()
        store = self._load_into_postgres(db_path)

        sqlite_new = service.create_session(1, "kb2")
        pg_new = store.create_session(1, "kb2")
        self.assertEqual(
            (pg_new.user_id, pg_new.kb_name, pg_new.title, pg_new.id),
            (sqlite_new.user_id, sqlite_new.kb_name, sqlite_new.title, sqlite_new.id),
        )
        self.assertEqual(
            [session.id for session in store.list_sessions(1)],
            [session.id for session in service.list_sessions(1)],
        )
        self.assertEqual(
            [session.id for session in store.list_sessions(1, "kb2")],
            [session.id for session in service.list_sessions(1, "kb2")],
        )

        sqlite_message = service.add_message(1, sqlite_new.id, "user", "标题来自消息")
        pg_message = store.add_message(1, pg_new.id, "user", "标题来自消息")
        self.assertEqual(pg_message.role, sqlite_message.role)
        self.assertEqual(pg_message.content, sqlite_message.content)
        self.assertEqual(
            store.get_session(1, pg_new.id).title,
            service.get_session(1, sqlite_new.id).title,
        )
        with self.assertRaises(PermissionError):
            store.add_message(2, pg_new.id, "user", "x")
        with self.assertRaises(ValueError):
            store.add_message(1, pg_new.id, "robot", "x")

        sqlite_turn = service.create_turn(
            1, sqlite_new.id, "幂等请求", client_request_id="req-9"
        )
        pg_turn = store.create_turn(1, pg_new.id, "幂等请求", client_request_id="req-9")
        self.assertEqual(
            (pg_turn.client_request_id, pg_turn.turn_sequence),
            (sqlite_turn.client_request_id, sqlite_turn.turn_sequence),
        )
        self.assertEqual(
            store.find_turn_by_client_request(pg_new.id, "req-9").id,
            pg_turn.id,
        )
        self.assertIsNone(store.find_turn_by_client_request(pg_new.id, "missing"))

        self.assertEqual(
            store.count_unterminal_turns(1), service.count_unterminal_turns(1)
        )
        self.assertEqual(
            store.count_unterminal_turns(1, session_id=pg_new.id),
            service.count_unterminal_turns(1, session_id=sqlite_new.id),
        )
        service.request_turn_cancel(1, pending_id)
        store.request_turn_cancel(1, pending_id)
        self.assertEqual(
            store.count_unterminal_turns(1), service.count_unterminal_turns(1)
        )

    def _sqlite_exec(self, db_path: str, sql: str, params: tuple = ()) -> None:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def _pg_exec(self, sql: str, params: tuple = ()) -> None:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute(sql, params)

    def test_waiting_lifecycle_attachments_and_metrics_match(self):
        service, db_path, session_id, completed_id, pending_id = self._fixture()
        store = self._load_into_postgres(db_path)

        self._sqlite_exec(
            db_path,
            "UPDATE chat_turns SET status='waiting_for_attachments' WHERE id=?",
            (pending_id,),
        )
        self._pg_exec(
            "UPDATE chat_turns SET status='waiting_for_attachments' WHERE id=%s",
            (pending_id,),
        )
        self.assertEqual(service.list_waiting_turn_ids(), [pending_id])
        self.assertEqual(store.list_waiting_turn_ids(), [pending_id])
        self.assertEqual(store.list_waiting_turn_ids([]), [])
        self.assertEqual(service.list_waiting_turn_ids(["missing"]), [])
        self.assertEqual(store.list_waiting_turn_ids(["missing"]), [])
        self.assertTrue(service.transition_waiting_turn(pending_id, to_status="pending"))
        self.assertTrue(store.transition_waiting_turn(pending_id, to_status="pending"))
        self.assertEqual(
            store.get_turn(pending_id).status,
            service.get_turn_unscoped(pending_id).status,
        )
        self.assertFalse(store.transition_waiting_turn(pending_id, to_status="pending"))
        with self.assertRaises(ValueError):
            store.transition_waiting_turn(pending_id, to_status="streaming")

        self._sqlite_exec(
            db_path,
            "UPDATE chat_turns SET status='waiting_for_attachments',"
            " created_at = datetime('now', '-2 hours') WHERE id=?",
            (pending_id,),
        )
        self._pg_exec(
            "UPDATE chat_turns SET status='waiting_for_attachments',"
            " created_at = now() - interval '2 hours' WHERE id=%s",
            (pending_id,),
        )
        self.assertEqual(service.expire_waiting_turns(max_wait_seconds=30), 1)
        self.assertEqual(store.expire_waiting_turns(max_wait_seconds=30), 1)
        sqlite_turn = service.get_turn_unscoped(pending_id)
        pg_turn = store.get_turn(pending_id)
        self.assertEqual(pg_turn.status, sqlite_turn.status)
        self.assertEqual(pg_turn.error_message, sqlite_turn.error_message)
        self.assertIn(
            "attachment_wait_timeout",
            [event.event_type for event in store.list_turn_events(pending_id)],
        )

        self._sqlite_exec(
            db_path,
            "INSERT OR REPLACE INTO turn_attachments (turn_id, attachment_id, ordinal,"
            " filename_snapshot, media_type_snapshot, usage_hint_snapshot,"
            " content_hash_snapshot, parser_version_snapshot, asset_id, parse_status_snapshot)"
            " VALUES (?, 'att-1', 0, 'a.xlsx', 'application/vnd.ms-excel', 'reference',"
            " 'hash-1', 'v1', '', 'ready')",
            (completed_id,),
        )
        self._pg_exec(
            "INSERT INTO turn_attachments (turn_id, attachment_id, ordinal,"
            " filename_snapshot, media_type_snapshot, usage_hint_snapshot,"
            " content_hash_snapshot, parser_version_snapshot, asset_id, parse_status_snapshot)"
            " VALUES (%s, 'att-1', 0, 'a.xlsx', 'application/vnd.ms-excel', 'reference',"
            " 'hash-1', 'v1', '', 'ready') ON CONFLICT DO NOTHING",
            (completed_id,),
        )

        def _shape(snapshots: list[dict]) -> list[tuple]:
            return [
                (
                    snapshot["attachment_id"],
                    snapshot["filename_snapshot"],
                    int(snapshot["ordinal"]),
                    bool(snapshot.get("deleted")),
                )
                for snapshot in snapshots
            ]

        self.assertEqual(
            _shape(store.list_turn_attachments(completed_id)),
            _shape(service.list_turn_attachments(completed_id)),
        )
        self.assertEqual(
            {
                key: _shape(value)
                for key, value in store.list_turn_attachments_for_session(session_id).items()
            },
            {
                key: _shape(value)
                for key, value in service.list_turn_attachments_for_session(session_id).items()
            },
        )

        self.assertEqual(
            store.task_metrics_summary(), service.task_metrics_summary()
        )
        self.assertEqual(
            store.task_metrics_summary(hours=48)["total"],
            service.task_metrics_summary(hours=48)["total"],
        )

    def test_session_resolution_and_turn_state_predicates_match(self):
        service, db_path, session_id, completed_id, _pending_id = self._fixture()
        store = self._load_into_postgres(db_path)

        sqlite_resolved = service.get_or_create_session(1, "__general__", session_id)
        pg_resolved = store.get_or_create_session(1, "__general__", session_id)
        self.assertEqual(pg_resolved.id, sqlite_resolved.id)

        sqlite_created = service.get_or_create_session(1, "kb-resolve")
        pg_created = store.get_or_create_session(1, "kb-resolve")
        self.assertEqual(pg_created.id, sqlite_created.id)
        self.assertEqual(
            store.get_or_create_session(1, "kb-resolve").id,
            service.get_or_create_session(1, "kb-resolve").id,
        )

        sqlite_turn = service.get_turn_unscoped(completed_id)
        pg_turn = store.get_turn_by_message(1, sqlite_turn.user_message_id)
        self.assertIsNotNone(pg_turn)
        self.assertEqual(pg_turn.id, completed_id)
        self.assertEqual(
            store.get_turn_by_message(1, sqlite_turn.assistant_message_id).id,
            service.get_turn_by_message(1, sqlite_turn.assistant_message_id).id,
        )
        self.assertIsNone(store.get_turn_by_message(2, sqlite_turn.user_message_id))

        self._sqlite_exec(
            db_path,
            "UPDATE chat_turns SET status='streaming',"
            " worker_heartbeat_at = datetime('now', '-2 hours') WHERE id=?",
            (completed_id,),
        )
        self._pg_exec(
            "UPDATE chat_turns SET status='streaming',"
            " worker_heartbeat_at = now() - interval '2 hours' WHERE id=%s",
            (completed_id,),
        )
        self.assertTrue(
            service.is_turn_worker_stale(1, completed_id, stale_after_seconds=60)
        )
        self.assertTrue(
            store.is_turn_worker_stale(1, completed_id, stale_after_seconds=60)
        )
        self._sqlite_exec(
            db_path,
            "UPDATE chat_turns SET worker_heartbeat_at = datetime('now') WHERE id=?",
            (completed_id,),
        )
        self._pg_exec(
            "UPDATE chat_turns SET worker_heartbeat_at = now() WHERE id=%s",
            (completed_id,),
        )
        self.assertFalse(
            service.is_turn_worker_stale(1, completed_id, stale_after_seconds=60)
        )
        self.assertFalse(
            store.is_turn_worker_stale(1, completed_id, stale_after_seconds=60)
        )

        self.assertFalse(service.is_turn_cancel_requested(completed_id))
        self.assertFalse(store.is_turn_cancel_requested(completed_id))
        service.request_turn_cancel(1, completed_id)
        store.request_turn_cancel(1, completed_id)
        self.assertTrue(store.is_turn_cancel_requested(completed_id))
        self.assertTrue(service.is_turn_cancel_requested(completed_id))

    def test_queue_state_and_pending_work_match(self):
        service, db_path, session_id, _completed_id, pending_id = self._fixture()
        store = self._load_into_postgres(db_path)

        sqlite_depth, _ = service.pending_turn_queue_state()
        pg_depth, pg_age = store.pending_turn_queue_state()
        self.assertEqual(pg_depth, sqlite_depth)
        self.assertGreaterEqual(pg_age, 0.0)

        sqlite_work = [(turn.id, user_id) for turn, user_id in service.list_pending_turn_work(8)]
        pg_work = [(turn.id, user_id) for turn, user_id in store.list_pending_turn_work(8)]
        self.assertEqual(pg_work, sqlite_work)
        self.assertEqual(pg_work, [(pending_id, 1)])

        expected_events = [
            (event.seq, event.event_type) for event in service.list_turn_events(1, _completed_id)
        ]
        actual_events = [
            (event.seq, event.event_type) for event in store.list_turn_events(_completed_id)
        ]
        self.assertEqual(actual_events, expected_events)

    def _isolate_attachments(self) -> None:
        self.addCleanup(
            setattr,
            src.settings,
            "CHAT_ATTACHMENT_INDEX_DB_PATH",
            getattr(src.settings, "CHAT_ATTACHMENT_INDEX_DB_PATH", None),
        )
        self.addCleanup(
            setattr,
            src.settings,
            "CHAT_ATTACHMENT_STORAGE_DIR",
            getattr(src.settings, "CHAT_ATTACHMENT_STORAGE_DIR", None),
        )
        src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH = os.path.join(
            self.tmp.name, "attachments.db"
        )
        src.settings.CHAT_ATTACHMENT_STORAGE_DIR = os.path.join(
            self.tmp.name, "attachments"
        )

    def test_queue_timeout_and_session_lifecycle_match(self):
        service, db_path, session_id, _completed_id, pending_id = self._fixture()
        store = self._load_into_postgres(db_path)
        self._isolate_attachments()

        self._sqlite_exec(
            db_path,
            "UPDATE chat_turns SET created_at = datetime('now', '-2 hours') WHERE id=?",
            (pending_id,),
        )
        self._pg_exec(
            "UPDATE chat_turns SET created_at = now() - interval '2 hours' WHERE id=%s",
            (pending_id,),
        )
        self.assertEqual(service.expire_queued_turns(max_queue_seconds=30), 1)
        self.assertEqual(store.expire_queued_turns(max_queue_seconds=30), 1)
        self.assertEqual(
            store.get_turn(pending_id).status,
            service.get_turn_unscoped(pending_id).status,
        )
        self.assertEqual(
            store.get_turn(pending_id).error_message,
            service.get_turn_unscoped(pending_id).error_message,
        )
        self.assertIn(
            "chat_queue_timeout",
            [event.event_type for event in store.list_turn_events(pending_id)],
        )

        service.clear_session(1, session_id)
        store.clear_session(1, session_id)
        self.assertEqual(store.list_messages(1, session_id), [])
        self.assertEqual(service.list_messages(1, session_id), [])
        self.assertEqual(
            store.get_session(1, session_id).title,
            service.get_session(1, session_id).title,
        )
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            pg_turns = conn.execute(
                "SELECT COUNT(*) FROM chat_turns WHERE session_id = %s", (session_id,)
            ).fetchone()[0]
            pg_generation = conn.execute(
                "SELECT generation FROM conversation_execution_state WHERE session_id = %s",
                (session_id,),
            ).fetchone()[0]
        with closing(sqlite3.connect(db_path)) as conn:
            sqlite_turns = conn.execute(
                "SELECT COUNT(*) FROM chat_turns WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            sqlite_generation = conn.execute(
                "SELECT generation FROM conversation_execution_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        self.assertEqual(pg_turns, 0)
        self.assertEqual(pg_turns, sqlite_turns)
        self.assertEqual(pg_generation, sqlite_generation)

        sqlite_extra = service.create_session(1, "kb-del")
        pg_extra = store.create_session(1, "kb-del")
        self.assertTrue(store.delete_session(1, pg_extra.id))
        self.assertTrue(service.delete_session(1, sqlite_extra.id))
        self.assertIsNone(store.get_session(1, pg_extra.id))
        self.assertIsNone(service.get_session(1, sqlite_extra.id))
        self.assertFalse(store.delete_session(1, pg_extra.id))
        self.assertFalse(service.delete_session(1, sqlite_extra.id))

    def _sqlite_rows(self, db_path: str, sql: str) -> list[dict]:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return _normalize([dict(row) for row in conn.execute(sql).fetchall()])

    def _pg_rows(self, sql: str) -> list[dict]:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC", row_factory=dict_row
        ) as conn:
            return _normalize([dict(row) for row in conn.execute(sql).fetchall()])

    def test_message_edit_and_reflection_enqueue_match(self):
        service, db_path, _session_id, _completed_id, _pending_id = self._fixture()
        scoped = service.create_session(1, "kb-edit", department_id=1, kb_id=1)
        turn = service.create_turn(1, scoped.id, "编辑问题")
        claimed = service.claim_turn(1, turn.id, "worker-a")
        service.complete_turn_result(
            1,
            turn.id,
            "原回答",
            {},
            worker_id="worker-a",
            fencing_token=claimed.fencing_token,
            expected_generation=claimed.session_generation,
        )
        assistant_id = service.get_turn_unscoped(turn.id).assistant_message_id
        store = self._load_into_postgres(db_path)

        edits_sql = (
            "SELECT message_id, editor_user_id, action, previous_content,"
            " previous_content_hash, reason, request_id FROM chat_message_edits"
            f" WHERE message_id = {assistant_id} ORDER BY request_id, action"
        )
        jobs_sql = (
            "SELECT session_id, job_kind, scope_fingerprint, target_turn_id,"
            " target_message_id, generation, status, last_error,"
            " (lease_owner IS NULL) AS lease_clear, (completed_at IS NULL) AS open"
            f" FROM memory_jobs WHERE session_id = {scoped.id} ORDER BY job_kind"
        )

        pg_msg = store.edit_message(
            1, scoped.id, assistant_id, content="新回答", reason="fix", request_id="req-1"
        )
        sqlite_msg = service.edit_message(
            1, scoped.id, assistant_id, content="新回答", reason="fix", request_id="req-1"
        )
        self.assertEqual(pg_msg.content, sqlite_msg.content)
        self.assertEqual(pg_msg.redacted, sqlite_msg.redacted)
        self.assertEqual(bool(pg_msg.edited_at), bool(sqlite_msg.edited_at))
        self.assertEqual(self._pg_rows(edits_sql), self._sqlite_rows(db_path, edits_sql))
        self.assertEqual(self._pg_rows(jobs_sql), self._sqlite_rows(db_path, jobs_sql))

        store.edit_message(1, scoped.id, assistant_id, redact=True, reason="privacy")
        service.edit_message(1, scoped.id, assistant_id, redact=True, reason="privacy")
        self.assertEqual(
            self._pg_rows(edits_sql), self._sqlite_rows(db_path, edits_sql)
        )
        self.assertEqual(self._pg_rows(jobs_sql), self._sqlite_rows(db_path, jobs_sql))
        pg_message = _normalize(
            [dict(row) for row in self._pg_rows(
                "SELECT content, redacted FROM chat_messages"
                f" WHERE id = {assistant_id}"
            )]
        )
        self.assertEqual(pg_message[0]["content"], "[已脱敏]")
        self.assertEqual(pg_message[0]["redacted"], 1)

    def test_queue_context_and_memory_settings_match(self):
        service, db_path, session_id, completed_id, pending_id = self._fixture()
        seeded_at = "2026-01-01T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO memory_records (memory_id, scope, user_id, status,"
                " current_revision, content_hash, memory_type, title, content_json,"
                " schema_version, created_at, updated_at)"
                " VALUES ('mem-q', 'project', 'u1', 'candidate', 1, 'h', 'fact',"
                " 't', '{}', '1', ?, ?)",
                (seeded_at, seeded_at),
            )
            conn.execute(
                "INSERT INTO memory_sources (source_id, memory_id, session_id,"
                " message_id, source_hash, source_valid, created_at)"
                " VALUES ('src-q', 'mem-q', ?, 1, 'sh', 1, ?)",
                (session_id, seeded_at),
            )
            enqueue_project_reflection_in_connection(
                conn,
                session_id=session_id,
                scope_fingerprint="project:q",
                target_turn_id="turn-q",
                target_message_id=1,
                available_at="2026-01-01T00:00:00+00:00",
            )
            conn.commit()
        store = self._load_into_postgres(db_path)

        # No earlier non-terminal turn: the queued turn has no blocker.
        sqlite_ctx = service.turn_queue_context(pending_id)
        pg_ctx = store.turn_queue_context(pending_id)
        self.assertEqual(pg_ctx, sqlite_ctx)

        self._sqlite_exec(
            db_path, "UPDATE chat_turns SET status='streaming' WHERE id=?", (completed_id,)
        )
        self._pg_exec(
            "UPDATE chat_turns SET status='streaming' WHERE id=%s", (completed_id,)
        )
        sqlite_ctx = service.turn_queue_context(pending_id)
        pg_ctx = store.turn_queue_context(pending_id)
        self.assertEqual(pg_ctx["blocked_by_turn_id"], completed_id)
        self.assertEqual(pg_ctx, sqlite_ctx)

        self._sqlite_exec(
            db_path,
            "UPDATE chat_turns SET status='waiting_for_attachments' WHERE id=?",
            (pending_id,),
        )
        self._pg_exec(
            "UPDATE chat_turns SET status='waiting_for_attachments' WHERE id=%s",
            (pending_id,),
        )
        sqlite_ctx = service.turn_queue_context(pending_id)
        pg_ctx = store.turn_queue_context(pending_id)
        self.assertEqual(pg_ctx["queue_reason"], "waiting_for_attachments")
        self.assertEqual(pg_ctx["queue_reason"], sqlite_ctx["queue_reason"])
        self.assertIsNotNone(pg_ctx["wait_deadline"])
        self.assertIsNotNone(sqlite_ctx["wait_deadline"])

        sqlite_settings = service.get_session_memory_settings(1, session_id)
        pg_settings = store.get_session_memory_settings(1, session_id)
        self.assertEqual(pg_settings, sqlite_settings)
        self.assertEqual(pg_settings["extracted_memories"], 1)
        with self.assertRaises(PermissionError):
            store.get_session_memory_settings(2, session_id)

        disabled_pg = store.set_session_auto_extract(1, session_id, False)
        disabled_sqlite = service.set_session_auto_extract(1, session_id, False)
        self.assertEqual(disabled_pg, disabled_sqlite)
        self.assertFalse(disabled_pg["auto_extract_enabled"])
        jobs_sql = (
            "SELECT session_id, status, generation, last_error FROM memory_jobs"
            f" WHERE session_id = {session_id} ORDER BY job_kind"
        )
        self.assertEqual(self._pg_rows(jobs_sql), self._sqlite_rows(db_path, jobs_sql))
        audit_sql = (
            "SELECT event_type, metadata_json FROM memory_audit_events"
            " ORDER BY event_type, metadata_json"
        )
        self.assertEqual(self._pg_rows(audit_sql), self._sqlite_rows(db_path, audit_sql))

        enabled_pg = store.set_session_auto_extract(1, session_id, True)
        enabled_sqlite = service.set_session_auto_extract(1, session_id, True)
        self.assertEqual(enabled_pg, enabled_sqlite)
        self.assertTrue(enabled_pg["auto_extract_enabled"])


if __name__ == "__main__":
    unittest.main()
