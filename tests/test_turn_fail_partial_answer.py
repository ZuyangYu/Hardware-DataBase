"""Watchdog failures must not vaporise the streamed partial answer."""

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
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, department_id) VALUES (1, 'u1', NULL)"
        )
        conn.commit()


class FailTurnPartialAnswerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._original = {"AUTH_DB_PATH": src.settings.AUTH_DB_PATH}
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        src.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(self._restore_settings)
        _seed_users(self.db_path)
        self.conv = ConversationService(self.db_path)
        self.session = self.conv.create_session(1, "__general__")

    def _restore_settings(self):
        for key, value in self._original.items():
            setattr(src.settings, key, value)

    def _turn(self, query: str):
        return self.conv.create_turn(user_id=1, session_id=self.session.id, query=query)

    def _assistant_content(self, turn) -> str:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT content FROM chat_messages WHERE id = ?",
                (turn.assistant_message_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def test_failed_turn_keeps_streamed_partial_answer(self):
        turn = self._turn("关键器件清单草稿")
        finalization = self.conv.fail_turn_result(
            1,
            turn.id,
            "生成超时，请稍后重试",
            partial_answer="| 序号 | 器件 | 型号 |\n|---|---|---|\n| 1 | MCU | SAK-TC377TP |",
            summary={"evidence": [{"text": "JS F08"}]},
        )
        self.assertEqual(finalization.turn.status, "failed")
        final = self.conv.get_turn(1, turn.id)
        self.assertIn("SAK-TC377TP", final.answer or "")
        self.assertEqual((final.summary or {}).get("evidence"), [{"text": "JS F08"}])
        self.assertIn("SAK-TC377TP", self._assistant_content(final))

    def test_failed_turn_without_partial_keeps_placeholder(self):
        turn = self._turn("普通问题")
        self.conv.fail_turn_result(1, turn.id, "上游错误")
        final = self.conv.get_turn(1, turn.id)
        self.assertEqual(final.status, "failed")
        self.assertEqual((final.answer or "").strip(), "")
        self.assertTrue(self._assistant_content(final).startswith("生成失败："))

    def test_rescue_never_overwrites_fuller_answer(self):
        turn = self._turn("普通问题")
        # 先写入一个已较完整的 answer（等价于 worker 在失败前已落库）
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE chat_turns SET answer = ? WHERE id = ?",
                ("完整答案", turn.id),
            )
            conn.commit()
        self.conv.fail_turn_result(
            1, turn.id, "生成超时", partial_answer="被截断的部分答案"
        )
        final = self.conv.get_turn(1, turn.id)
        self.assertEqual(final.answer, "完整答案")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
