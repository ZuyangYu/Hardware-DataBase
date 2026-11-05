"""Task turns use an isolated queue status so stale workers cannot claim them."""

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
                department_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, department_id, is_active) "
            "VALUES (1, 'u1', NULL, 1)"
        )
        conn.commit()


class TaskTurnQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._original = {
            "AUTH_DB_PATH": src.settings.AUTH_DB_PATH,
            "CONVERSATION_SERIALIZE_SAME_THREAD": getattr(
                src.settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True
            ),
        }
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        src.settings.AUTH_DB_PATH = self.db_path
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        self.addCleanup(self._restore_settings)
        _seed_users(self.db_path)
        self.conv = ConversationService(self.db_path)
        self.session = self.conv.create_session(1, "__general__")

    def _restore_settings(self):
        for key, value in self._original.items():
            setattr(src.settings, key, value)

    def test_task_turn_uses_isolated_status_and_is_claimable(self):
        turn = self.conv.create_turn(
            1,
            self.session.id,
            "整理清单",
            query_mode="deep",
            trace_context={"task_id": "task-1"},
        )
        self.assertEqual(turn.status, "task_pending")
        # Old workers only poll pending/multi_kb_pending; the task turn is
        # invisible to them by construction.
        self.assertNotIn(turn.status, {"pending", "multi_kb_pending"})

        pending = self.conv.list_pending_turn_work()
        self.assertIn(turn.id, {item.id for item, _uid in pending})

        claim = self.conv.claim_turn_result(1, turn.id, worker_id="worker-a")
        self.assertIsNotNone(claim.turn)
        self.assertIn(claim.turn.status, {"streaming", "pending", "multi_kb_streaming"})

    def test_plain_turn_keeps_pending_status(self):
        turn = self.conv.create_turn(1, self.session.id, "普通问题")
        self.assertEqual(turn.status, "pending")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
