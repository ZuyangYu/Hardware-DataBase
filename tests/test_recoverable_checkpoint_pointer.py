"""P0-003: recoverable checkpoint pointer registration and take-over fork."""

from __future__ import annotations

import operator
import os
import sqlite3
import tempfile
import unittest
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

import src.settings
from src.agents.checkpoints import prepare_attempt
from src.agents.checkpoints.observer import install_checkpoint_observer
from src.core.conversation import ConversationService, StaleTurnOwner


def _seed_users(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
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


class _State(TypedDict):
    log: Annotated[list[str], operator.add]


def _append(state: _State) -> _State:
    return {"log": ["node"]}


def _build_app(saver: SqliteSaver):
    builder = StateGraph(_State)
    builder.add_node("append", _append)
    builder.add_edge(START, "append")
    builder.add_edge("append", END)
    return builder.compile(checkpointer=saver)


class RecoverablePointerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._original = src.settings.CONVERSATION_SERIALIZE_SAME_THREAD
        self.addCleanup(
            setattr, src.settings, "CONVERSATION_SERIALIZE_SAME_THREAD", self._original
        )
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        _seed_users(self.db_path)
        self.conv = ConversationService(self.db_path)
        self.session = self.conv.create_session(1, "__general__")
        self.turn = self.conv.create_turn(user_id=1, session_id=self.session.id, query="q1")
        self.claimed = self.conv.claim_turn(1, self.turn.id, "worker-a")
        self.assertIsNotNone(self.claimed)

    def _ref(self) -> dict:
        return {
            "backend": "sqlite",
            "thread_id": f"{self.session.id}::attempt-{self.claimed.attempt_id}",
            "checkpoint_ns": "",
            "checkpoint_id": "ckpt-1",
            "attempt_id": self.claimed.attempt_id,
            "fencing_token": self.claimed.fencing_token,
            "generation": self.claimed.session_generation,
        }

    def test_pointer_roundtrip_and_owner_cas(self):
        self.assertTrue(
            self.conv.update_recoverable_checkpoint_ref(
                self.turn.id,
                "worker-a",
                self.claimed.fencing_token,
                self._ref(),
                expected_generation=self.claimed.session_generation,
            )
        )
        refs = self.conv.get_checkpoint_refs(self.session.id)
        self.assertEqual(refs["recoverable"]["checkpoint_id"], "ckpt-1")
        self.assertEqual(refs["fencing_token"], self.claimed.fencing_token)
        with self.assertRaises(StaleTurnOwner):
            self.conv.update_recoverable_checkpoint_ref(
                self.turn.id, "worker-b", self.claimed.fencing_token + 1, self._ref()
            )

    def test_cancel_revokes_pointer_updates(self):
        self.conv.request_turn_cancel(1, self.turn.id)
        with self.assertRaises(StaleTurnOwner):
            self.conv.update_recoverable_checkpoint_ref(
                self.turn.id,
                "worker-a",
                self.claimed.fencing_token,
                self._ref(),
                expected_generation=self.claimed.session_generation,
            )

    def test_clear_session_drops_pointers(self):
        self.conv.update_recoverable_checkpoint_ref(
            self.turn.id, "worker-a", self.claimed.fencing_token, self._ref()
        )
        self.conv.clear_session(1, self.session.id)
        refs = self.conv.get_checkpoint_refs(self.session.id)
        self.assertIsNone(refs["recoverable"])
        self.assertIsNone(refs["committed"])
        self.assertEqual(refs["generation"], 1)


class CheckpointObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(
            os.path.join(self.tmp.name, "checkpoints.db"), check_same_thread=False
        )
        self.addCleanup(self.conn.close)
        self.saver = SqliteSaver(self.conn)
        self.saver.setup()
        self.app = _build_app(self.saver)

    def _config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def test_observer_reports_only_matching_thread(self):
        seen: list[dict] = []
        handle = install_checkpoint_observer(
            self.saver, seen.append, thread_filter="conv-1::attempt-1"
        )
        try:
            self.app.invoke({"log": ["u1"]}, self._config("conv-1::attempt-1"))
            matching = len(seen)
            self.assertGreaterEqual(matching, 1)
            self.assertTrue(all(item["thread_id"] == "conv-1::attempt-1" for item in seen))
            self.assertTrue(all(item["checkpoint_id"] for item in seen))
            self.app.invoke({"log": ["u1"]}, self._config("other-thread"))
            self.assertEqual(len(seen), matching)
        finally:
            handle.uninstall()
        self.app.invoke({"log": ["u2"]}, self._config("conv-1::attempt-1"))
        self.assertEqual(len(seen), matching, "uninstalled observer must stay silent")

    def test_prepare_attempt_resumes_from_recoverable_ref(self):
        config = self._config("conv-1")
        self.app.invoke({"log": ["u1"]}, config)
        checkpoint = self.saver.get_tuple(config)
        ref = {
            "thread_id": "conv-1",
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint.config["configurable"]["checkpoint_id"],
        }
        attempt = prepare_attempt(
            self.saver,
            "conv-1",
            2,
            attempt_id="attempt-abc",
            resume_ref=ref,
            pointer_path=os.path.join(self.tmp.name, "pointers.db"),
        )
        self.assertEqual(attempt, "conv-1::attempt-attempt-abc")
        copied = self.saver.get_tuple(self._config(attempt))
        self.assertIsNotNone(copied, "attempt namespace must be forked from the ref")
        self.app.invoke({"log": ["u2"]}, self._config(attempt))
        state = self.app.get_state(self._config(attempt))
        self.assertEqual(state.values, {"log": ["u1", "node", "u2", "node"]})


if __name__ == "__main__":
    unittest.main()
