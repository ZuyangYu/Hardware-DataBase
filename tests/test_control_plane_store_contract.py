"""Execution-store contract tests: SQLite adapter vs PostgreSQL store (v3 PR-2).

The same scenarios run against both implementations; the PostgreSQL half also
proves the shared-transaction guarantees (terminal events, domain writer) on a
real database.  Requires ``HDB_TEST_POSTGRES_DSN`` for the PostgreSQL store.
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
from psycopg.rows import dict_row

import src.settings
from src.core.conversation import StaleTurnOwner
from src.storage.control_plane import apply_schema
from src.storage.control_plane.postgres import PostgresExecutionStore
from src.storage.control_plane.sqlite import SqliteExecutionStore

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
                department_id INTEGER
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, department_id) VALUES (1, 'u1', NULL)"
        )
        conn.commit()


class _StoreContractScenarios:
    """Behavioral contract shared by both store implementations."""

    user_id = 1

    # -- harness hooks (implemented per backend) -----------------------------

    def store(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def create_session(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def expire_lease(self, turn_id: str) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def set_retry_count(self, turn_id: str, value: int) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- scenarios -----------------------------------------------------------

    def test_claim_orders_turns_and_allocates_tokens(self):
        store = self.store()
        session = self.create_session()
        first = store.create_turn(self.user_id, session, "q1")
        second = store.create_turn(self.user_id, session, "q2")
        self.assertEqual((first.turn_sequence, second.turn_sequence), (1, 2))

        blocked = store.claim_turn(second.id, "worker-b")
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.blocker_turn_id, first.id)

        claimed = store.claim_turn(first.id, "worker-a")
        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(claimed.turn.turn_sequence, 1)
        self.assertEqual(claimed.turn.fencing_token, 1)
        self.assertTrue(claimed.turn.attempt_id)
        self.assertNotEqual(store.claim_turn(first.id, "worker-b").status, "claimed")

    def test_takeover_bumps_token_and_rejects_stale_writes(self):
        store = self.store()
        session = self.create_session()
        turn = store.create_turn(self.user_id, session, "q1")
        first = store.claim_turn(turn.id, "worker-a")
        self.expire_lease(turn.id)
        second = store.claim_turn(turn.id, "worker-b")
        self.assertEqual(second.status, "claimed")
        self.assertEqual(second.turn.fencing_token, 2)
        with self.assertRaises(StaleTurnOwner):
            store.append_turn_event(
                turn.id,
                "delta",
                {"text": "late"},
                worker_id="worker-a",
                attempt_id=first.turn.attempt_id,
                fencing_token=first.turn.fencing_token,
            )
        accepted = store.append_turn_event(
            turn.id,
            "delta",
            {"text": "current"},
            worker_id="worker-b",
            attempt_id=second.turn.attempt_id,
            fencing_token=second.turn.fencing_token,
        )
        self.assertEqual(accepted.seq, 1)

    def test_complete_persists_terminal_events_atomically(self):
        store = self.store()
        session = self.create_session()
        turn = store.create_turn(self.user_id, session, "q1")
        claimed = store.claim_turn(turn.id, "worker-a")
        completion = store.complete_turn(
            self.user_id,
            turn.id,
            "answer",
            {},
            worker_id="worker-a",
            attempt_id=claimed.turn.attempt_id,
            fencing_token=claimed.turn.fencing_token,
            expected_generation=claimed.turn.session_generation,
            terminal_events_builder=lambda completed: [
                ("done", {"answer": completed.answer}),
                ("turn_end", {"status": "completed"}),
            ],
        )
        self.assertEqual(completion.turn.status, "completed")
        self.assertEqual([event.seq for event in completion.terminal_events], [1, 2])
        events = store.list_turn_events(turn.id)
        self.assertEqual([event.event_type for event in events], ["done", "turn_end"])
        self.assertEqual(store.get_turn(turn.id).status, "completed")
        self.assertEqual(store.get_turn(turn.id).answer, "answer")

    def test_domain_writer_failure_rolls_back_terminal_commit(self):
        store = self.store()
        session = self.create_session()
        turn = store.create_turn(self.user_id, session, "q1")
        claimed = store.claim_turn(turn.id, "worker-a")

        def broken_domain(_conn, _turn):
            raise RuntimeError("domain commit failed")

        with self.assertRaises(RuntimeError):
            store.complete_turn(
                self.user_id,
                turn.id,
                "answer",
                {},
                worker_id="worker-a",
                attempt_id=claimed.turn.attempt_id,
                fencing_token=claimed.turn.fencing_token,
                expected_generation=claimed.turn.session_generation,
                terminal_events_builder=lambda completed: [("done", {"ok": True})],
                domain_writer=broken_domain,
            )
        current = store.get_turn(turn.id)
        self.assertEqual(current.status, "streaming")
        self.assertEqual(current.answer, "")
        self.assertEqual(current.last_event_seq, 0)
        self.assertEqual(store.list_turn_events(turn.id), [])

    def test_cancel_revokes_completion_and_allows_cancel_finalize(self):
        store = self.store()
        session = self.create_session()
        turn = store.create_turn(self.user_id, session, "q1")
        claimed = store.claim_turn(turn.id, "worker-a")
        cancelled = store.request_turn_cancel(self.user_id, turn.id)
        self.assertEqual(cancelled.status, "cancelled")
        with self.assertRaises(StaleTurnOwner):
            store.complete_turn(
                self.user_id,
                turn.id,
                "answer",
                {},
                worker_id="worker-a",
                attempt_id=claimed.turn.attempt_id,
                fencing_token=claimed.turn.fencing_token,
                expected_generation=claimed.turn.session_generation,
            )
        with self.assertRaises(StaleTurnOwner):
            store.fail_turn(
                self.user_id,
                turn.id,
                "已停止生成",
                cancelled=True,
                worker_id="worker-a",
                attempt_id=claimed.turn.attempt_id,
                fencing_token=claimed.turn.fencing_token,
            )
        self.assertEqual(store.get_turn(turn.id).status, "cancelled")

    def test_cancel_pending_turn_converges_immediately(self):
        store = self.store()
        session = self.create_session()
        turn = store.create_turn(self.user_id, session, "q1")
        cancelled = store.request_turn_cancel(self.user_id, turn.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(store.get_turn(turn.id).status, "cancelled")

    def test_requeue_converges_cancelled_exhausted_and_live(self):
        store = self.store()
        live = store.create_turn(self.user_id, self.create_session(), "q1")
        store.claim_turn(live.id, "worker-a")
        self.expire_lease(live.id)
        summary = store.requeue_stale_turns(stale_after_seconds=30)
        self.assertEqual(summary.requeued, 1)
        self.assertEqual(store.get_turn(live.id).status, "pending")
        self.assertEqual(store.list_turn_events(live.id), [])

        cancelled = store.create_turn(self.user_id, self.create_session(), "q2")
        claimed = store.claim_turn(cancelled.id, "worker-b")
        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(
            store.request_turn_cancel(self.user_id, cancelled.id).status, "cancelled"
        )
        self.expire_lease(cancelled.id)
        summary = store.requeue_stale_turns(stale_after_seconds=30)
        self.assertEqual(summary.requeued, 0)
        self.assertEqual(store.get_turn(cancelled.id).status, "cancelled")
        self.assertEqual(
            [event.event_type for event in store.list_turn_events(cancelled.id)],
            ["error", "turn_end"],
        )

        exhausted = store.create_turn(self.user_id, self.create_session(), "q3")
        store.claim_turn(exhausted.id, "worker-c")
        self.set_retry_count(exhausted.id, 3)
        self.expire_lease(exhausted.id)
        summary = store.requeue_stale_turns(stale_after_seconds=30)
        self.assertEqual(summary.requeued, 0)
        current = store.get_turn(exhausted.id)
        self.assertEqual(current.status, "failed")
        self.assertIn("重试", current.error_message)

    def test_idempotent_create_turn_replay_returns_original(self):
        store = self.store()
        session = self.create_session()
        first = store.create_turn(
            self.user_id, session, "q1", client_request_id="request-1"
        )
        replay = store.create_turn(
            self.user_id, session, "q1", client_request_id="request-1"
        )
        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.turn_sequence, first.turn_sequence)


class SqliteStoreContractTests(_StoreContractScenarios, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._original_db = src.settings.AUTH_DB_PATH
        self._original_serialize = src.settings.CONVERSATION_SERIALIZE_SAME_THREAD
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", self._original_db)
        self.addCleanup(
            setattr,
            src.settings,
            "CONVERSATION_SERIALIZE_SAME_THREAD",
            self._original_serialize,
        )
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        _seed_users(self.db_path)
        src.settings.AUTH_DB_PATH = self.db_path
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        self._store = SqliteExecutionStore(self.db_path)

    def store(self):
        return self._store

    def create_session(self) -> int:
        return self._store.conversations.create_session(self.user_id, "__general__").id

    def expire_lease(self, turn_id: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE conversation_execution_state"
                " SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
                " WHERE active_turn_id = ?",
                (turn_id,),
            )
            conn.execute(
                "UPDATE chat_turns SET worker_heartbeat_at = '2000-01-01T00:00:00+00:00'"
                " WHERE id = ?",
                (turn_id,),
            )
            conn.commit()

    def set_retry_count(self, turn_id: str, value: int) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE chat_turns SET retry_count = ? WHERE id = ?", (value, turn_id)
            )
            conn.commit()


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class PostgresStoreContractTests(_StoreContractScenarios, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_store_test_{uuid.uuid4().hex[:8]}"
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
        self._original_serialize = src.settings.CONVERSATION_SERIALIZE_SAME_THREAD
        self.addCleanup(
            setattr,
            src.settings,
            "CONVERSATION_SERIALIZE_SAME_THREAD",
            self._original_serialize,
        )
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC", row_factory=dict_row
        ) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn, include_indexes=True)
            from src.quality.store import initialize_schema
            from src.storage.control_plane.sql_adapter import PgSqlAdapterConnection

            initialize_schema(PgSqlAdapterConnection(conn))
        self._store = PostgresExecutionStore(self.dsn)
        self.addCleanup(self._store.close)

    def store(self):
        return self._store

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute(sql, params)

    def create_session(self) -> int:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            row = conn.execute(
                "INSERT INTO chat_sessions (user_id, kb_name, title, created_at, updated_at)"
                " VALUES (%s, '__general__', '新对话', now(), now()) RETURNING id",
                (self.user_id,),
            ).fetchone()
        return int(row[0])

    def expire_lease(self, turn_id: str) -> None:
        self._execute(
            "UPDATE conversation_execution_state"
            " SET lease_expires_at = now() - interval '1 hour'"
            " WHERE active_turn_id = %s",
            (turn_id,),
        )
        self._execute(
            "UPDATE chat_turns SET worker_heartbeat_at = now() - interval '1 hour'"
            " WHERE id = %s",
            (turn_id,),
        )

    def set_retry_count(self, turn_id: str, value: int) -> None:
        self._execute(
            "UPDATE chat_turns SET retry_count = %s WHERE id = %s", (int(value), turn_id)
        )

    def test_recovery_uses_configured_heartbeat_ttl_and_explicit_override(self):
        original = src.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS
        self.addCleanup(setattr, src.settings, "CHAT_TURN_HEARTBEAT_TTL_SECONDS", original)
        src.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS = 30
        store = self.store()
        session = self.create_session()
        turn = store.create_turn(self.user_id, session, "recover configured timeout")
        first = store.claim_turn(turn.id, "old-worker")
        self._execute("UPDATE chat_turns SET worker_heartbeat_at = now() - interval '45 seconds' WHERE id = %s", (turn.id,))
        self.assertEqual(store.requeue_stale_turns(60).requeued, 0)
        self.assertEqual(store.requeue_stale_turns().requeued, 1)
        second = store.claim_turn(turn.id, "new-worker")
        self.assertEqual(second.status, "claimed")
        self.assertGreater(second.turn.fencing_token, first.turn.fencing_token)
        with self.assertRaises(StaleTurnOwner):
            store.append_turn_event(turn.id, "delta", {"text": "late"}, worker_id="old-worker",
                                    attempt_id=first.turn.attempt_id, fencing_token=first.turn.fencing_token)


if __name__ == "__main__":
    unittest.main()
