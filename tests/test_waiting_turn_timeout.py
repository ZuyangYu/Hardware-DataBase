"""P0-004: attachment wait cap, conditional transition and queue visibility."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

import src.settings
from src.core.conversation import ConversationService
from src.observability import metrics as obs_metrics


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


class WaitingTurnTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._original_db = src.settings.AUTH_DB_PATH
        self._original_max = src.settings.WAITING_TURN_MAX_SECONDS
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", self._original_db)
        self.addCleanup(setattr, src.settings, "WAITING_TURN_MAX_SECONDS", self._original_max)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        src.settings.AUTH_DB_PATH = self.db_path
        src.settings.WAITING_TURN_MAX_SECONDS = 600
        _seed_users(self.db_path)
        self.conv = ConversationService(self.db_path)
        self.session = self.conv.create_session(1, "__general__")

    def _turn(self, query: str):
        return self.conv.create_turn(user_id=1, session_id=self.session.id, query=query)

    def _mark_waiting(self, turn_id: str, *, age_seconds: int = 0) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                UPDATE chat_turns
                SET status = 'waiting_for_attachments',
                    created_at = datetime('now', ?)
                WHERE id = ?
                """,
                (f"-{int(age_seconds)} seconds", turn_id),
            )
            conn.commit()

    def test_expired_waiting_turn_fails_and_releases_session(self):
        first = self._turn("q1")
        second = self._turn("q2")
        self._mark_waiting(first.id, age_seconds=1200)
        expired = self.conv.expire_waiting_turns()
        self.assertEqual(expired, 1)
        updated = self.conv.get_turn(1, first.id)
        self.assertEqual(updated.status, "failed")
        self.assertIn("超时", updated.error_message)
        events = self.conv.list_turn_events(1, first.id)
        timeout_events = [event for event in events if event.event_type == "attachment_wait_timeout"]
        self.assertEqual(len(timeout_events), 1)
        self.assertEqual(timeout_events[0].payload.get("error_code"), "attachment_wait_timeout")
        claimed = self.conv.claim_turn(1, second.id, "worker-a")
        self.assertIsNotNone(claimed)

    def test_fresh_waiting_turn_is_not_expired(self):
        turn = self._turn("q1")
        self._mark_waiting(turn.id, age_seconds=5)
        self.assertEqual(self.conv.expire_waiting_turns(), 0)
        self.assertEqual(self.conv.get_turn(1, turn.id).status, "waiting_for_attachments")

    def test_settlement_racing_timeout_can_only_win_once(self):
        turn = self._turn("q1")
        self._mark_waiting(turn.id, age_seconds=1200)
        self.assertTrue(
            self.conv.transition_waiting_turn(turn.id, to_status="pending")
        )
        self.assertFalse(
            self.conv.transition_waiting_turn(
                turn.id,
                to_status="failed",
                error_message="too late",
                reason_code="attachment_wait_timeout",
            )
        )
        self.assertEqual(self.conv.expire_waiting_turns(), 0)
        self.assertEqual(self.conv.get_turn(1, turn.id).status, "pending")

    def test_queue_context_exposes_blocker_and_deadline(self):
        first = self._turn("q1")
        second = self._turn("q2")
        self._mark_waiting(first.id, age_seconds=10)
        waiting_context = self.conv.turn_queue_context(first.id)
        self.assertEqual(waiting_context["queue_reason"], "waiting_for_attachments")
        self.assertIsNotNone(waiting_context["wait_deadline"])
        blocked_context = self.conv.turn_queue_context(second.id)
        self.assertEqual(blocked_context["blocked_by_turn_id"], first.id)
        self.assertEqual(blocked_context["queue_reason"], "waiting_for_attachments")
        self.conv.transition_waiting_turn(
            first.id,
            to_status="failed",
            error_message="解析失败",
        )
        self.assertEqual(self.conv.turn_queue_context(second.id)["queue_reason"], "")

    def test_timeout_records_attachment_wait_metric(self):
        turn = self._turn("q1")
        self._mark_waiting(turn.id, age_seconds=1200)
        original = obs_metrics.histogram
        calls: list[tuple[str, dict]] = []

        def spy(name, value, *, attributes=None, unit="s", description=""):
            calls.append((name, dict(attributes or {})))
            return original(name, value, attributes=attributes, unit=unit, description=description)

        obs_metrics.histogram = spy
        try:
            self.conv.expire_waiting_turns()
        finally:
            obs_metrics.histogram = original
        self.assertIn(
            ("hdb.conversation.attachment_wait", {"status": "timed_out"}),
            calls,
        )


if __name__ == "__main__":
    unittest.main()
