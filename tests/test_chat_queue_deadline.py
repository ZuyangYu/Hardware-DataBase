"""P0b: chat queue deadline expiry for never-started turns."""

from __future__ import annotations

import os
import sqlite3
import tempfile
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


class ChatQueueDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._original_db = src.settings.AUTH_DB_PATH
        self._original_deadline = src.settings.CHAT_QUEUE_DEADLINE_SECONDS
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", self._original_db)
        self.addCleanup(setattr, src.settings, "CHAT_QUEUE_DEADLINE_SECONDS", self._original_deadline)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        src.settings.AUTH_DB_PATH = self.db_path
        src.settings.CHAT_QUEUE_DEADLINE_SECONDS = 600
        _seed_users(self.db_path)
        self.conv = ConversationService(self.db_path)
        self.session = self.conv.create_session(1, "__general__")

    def _turn(self, query: str):
        return self.conv.create_turn(user_id=1, session_id=self.session.id, query=query)

    def _age(self, turn_id: str, seconds: int) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE chat_turns SET created_at = datetime('now', ?) WHERE id = ?",
                (f"-{int(seconds)} seconds", turn_id),
            )
            conn.commit()

    def test_expired_pending_turn_fails_with_event(self):
        turn = self._turn("q1")
        self._age(turn.id, 1200)
        self.assertEqual(self.conv.expire_queued_turns(), 1)
        updated = self.conv.get_turn(1, turn.id)
        self.assertEqual(updated.status, "failed")
        self.assertIn("排队", updated.error_message)
        events = [event.event_type for event in self.conv.list_turn_events(1, turn.id)]
        self.assertIn("chat_queue_timeout", events)

    def test_fresh_pending_turn_is_not_expired(self):
        turn = self._turn("q1")
        self._age(turn.id, 5)
        self.assertEqual(self.conv.expire_queued_turns(), 0)
        self.assertEqual(self.conv.get_turn(1, turn.id).status, "pending")

    def test_running_turn_keeps_its_execution_deadline(self):
        turn = self._turn("q1")
        claimed = self.conv.claim_turn(1, turn.id, "worker-a")
        self.assertIsNotNone(claimed)
        self._age(turn.id, 1200)
        self.assertEqual(self.conv.expire_queued_turns(), 0)
        self.assertEqual(self.conv.get_turn(1, turn.id).status, "streaming")


if __name__ == "__main__":
    unittest.main()
