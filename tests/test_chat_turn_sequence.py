"""turn_sequence schema, allocation, idempotency and legacy backfill locks."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing

import src.settings
from src.core.conversation import ConversationService


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
        conn.execute("INSERT OR IGNORE INTO users (id, username, department_id) VALUES (1, 'u1', NULL)")
        conn.commit()


def _legacy_schema(db_path: str) -> None:
    """Pre-migration schema: no turn_sequence / next_turn_sequence columns."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kb_name TEXT NOT NULL,
                department_id INTEGER,
                kb_id INTEGER,
                title TEXT NOT NULL,
                document_context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chat_turns (
                id TEXT PRIMARY KEY,
                session_id INTEGER NOT NULL,
                user_message_id INTEGER NOT NULL,
                assistant_message_id INTEGER NOT NULL,
                kb_name TEXT NOT NULL,
                department_id INTEGER,
                kb_id INTEGER,
                query TEXT NOT NULL,
                query_mode TEXT NOT NULL DEFAULT 'fast',
                status TEXT NOT NULL,
                client_request_id TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                last_event_seq INTEGER NOT NULL DEFAULT 0,
                answer TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL DEFAULT '{}',
                footer TEXT NOT NULL DEFAULT '',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                trace_context_json TEXT NOT NULL DEFAULT '{}',
                document_context_json TEXT NOT NULL DEFAULT '{}',
                export_plan_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                worker_heartbeat_at TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )
        conn.commit()


class ChatTurnSequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._original_auth = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", self._original_auth)
        _seed_users(self.db_path)
        self.conv = ConversationService(self.db_path)
        self.session = self.conv.create_session(1, "__general__")

    def _create(self, query: str, session_id: int | None = None, request_id: str | None = None):
        return self.conv.create_turn(
            user_id=1,
            session_id=session_id or self.session.id,
            query=query,
            client_request_id=request_id,
        )

    def test_sequence_starts_at_one_and_increments(self):
        first = self._create("q1")
        second = self._create("q2")
        third = self._create("q3")
        self.assertEqual([first.turn_sequence, second.turn_sequence, third.turn_sequence], [1, 2, 3])
        with closing(sqlite3.connect(self.db_path)) as conn:
            counter = conn.execute(
                "SELECT next_turn_sequence FROM chat_sessions WHERE id = ?", (self.session.id,)
            ).fetchone()[0]
        # The counter stores the next available value (plan §5.1).
        self.assertEqual(counter, 4)

    def test_sequences_are_scoped_per_session(self):
        other = self.conv.create_session(1, "__general__")
        first = self._create("a")
        second = self._create("b")
        third = self._create("c", session_id=other.id)
        self.assertEqual(first.turn_sequence, 1)
        self.assertEqual(second.turn_sequence, 2)
        self.assertEqual(third.turn_sequence, 1)

    def test_idempotent_request_does_not_allocate_new_sequence(self):
        first = self._create("q1", request_id="req-1")
        replay = self._create("q1", request_id="req-1")
        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.turn_sequence, first.turn_sequence)
        with closing(sqlite3.connect(self.db_path)) as conn:
            counter = conn.execute(
                "SELECT next_turn_sequence FROM chat_sessions WHERE id = ?", (self.session.id,)
            ).fetchone()[0]
        # Idempotent replay allocates nothing: next stays 2 (first turn was 1).
        self.assertEqual(counter, 2)

    def test_concurrent_creation_allocates_unique_sequences(self):
        errors: list[Exception] = []
        results: list[int] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                turn = self.conv.create_turn(user_id=1, session_id=self.session.id, query=f"c-{index}")
                with lock:
                    results.append(turn.turn_sequence)
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        self.assertEqual(sorted(results), list(range(1, 21)))
        with closing(sqlite3.connect(self.db_path)) as conn:
            duplicates = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT turn_sequence FROM chat_turns WHERE session_id = ?
                    GROUP BY turn_sequence HAVING COUNT(*) > 1
                )
                """,
                (self.session.id,),
            ).fetchone()[0]
        self.assertEqual(duplicates, 0)

    def test_legacy_rows_are_backfilled_in_message_order(self):
        legacy_path = os.path.join(self.tmp.name, "legacy.db")
        _seed_users(legacy_path)
        _legacy_schema(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.execute(
                "INSERT INTO chat_sessions (id, user_id, kb_name, title, created_at, updated_at)"
                " VALUES (1, 1, '__general__', 'legacy', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
            message_ids = []
            for index in range(4):
                cursor = conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, created_at)"
                    " VALUES (1, 'user', ?, '2026-01-01T00:00:00Z')",
                    (f"legacy-{index}",),
                )
                message_ids.append(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, created_at)"
                    " VALUES (1, 'assistant', '', '2026-01-01T00:00:00Z')"
                )
            for note, message_id in zip(("t-shuffled-2", "t-shuffled-0", "t-shuffled-3", "t-shuffled-1"), (message_ids[2], message_ids[0], message_ids[3], message_ids[1])):
                conn.execute(
                    """
                    INSERT INTO chat_turns (
                        id, session_id, user_message_id, assistant_message_id, kb_name, query, query_mode, status, created_at
                    ) VALUES (?, 1, ?, ?, '__general__', 'legacy', 'fast', 'completed', '2026-01-01T00:00:00Z')
                    """,
                    (note, message_id, message_id + 1),
                )
            conn.commit()
        # Instantiating the service runs the expand+backfill migration.
        ConversationService(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, turn_sequence FROM chat_turns WHERE session_id = 1 ORDER BY user_message_id"
            ).fetchall()
            counter = conn.execute(
                "SELECT next_turn_sequence FROM chat_sessions WHERE id = 1"
            ).fetchone()[0]
        self.assertEqual([int(row["turn_sequence"]) for row in rows], [1, 2, 3, 4])
        self.assertEqual(int(counter), 5)
        ordered_ids = [str(row["id"]) for row in rows]
        self.assertEqual(ordered_ids, ["t-shuffled-0", "t-shuffled-1", "t-shuffled-2", "t-shuffled-3"])

    def test_schema_indexes_and_not_null_on_new_databases(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            indexes = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chat_turns'"
                ).fetchall()
            }
            table_sql = str(
                conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_turns'").fetchone()[0]
            )
        self.assertIn("ux_chat_turns_session_sequence", indexes)
        self.assertIn("ix_chat_turns_session_status_sequence", indexes)
        self.assertRegex(table_sql, r"turn_sequence\s+INTEGER\s+NOT NULL")


if __name__ == "__main__":
    unittest.main()
