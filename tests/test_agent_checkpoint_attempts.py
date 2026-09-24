"""P0a: attempt-scoped agent checkpoints with a canonical pointer CAS."""

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
from src.agents.checkpoints import (
    StaleAttemptCommit,
    attempt_thread_id,
    commit_attempt,
    forget_thread_state,
    get_checkpointer,
    prepare_attempt,
    purge_superseded_attempts,
    reset_checkpointer,
)


class _State(TypedDict):
    log: Annotated[list[str], operator.add]


def _build_app(saver: SqliteSaver):
    def append(state: _State) -> _State:
        return {"log": ["node"]}

    builder = StateGraph(_State)
    builder.add_node("append", append)
    builder.add_edge(START, "append")
    builder.add_edge("append", END)
    return builder.compile(checkpointer=saver)


def _config(thread_id: str, checkpoint_id: str = "") -> dict:
    configurable: dict = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


class CheckpointAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "agent_checkpoints.db")
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.saver = SqliteSaver(self.conn)
        self.saver.setup()
        self.app = _build_app(self.saver)
        self.visible = "conv-1"

    def _seed_canonical(self):
        self.app.invoke({"log": ["u1"]}, _config(self.visible))

    def test_prepare_forks_state_without_touching_visible(self):
        self._seed_canonical()
        attempt = prepare_attempt(self.saver, self.visible, 1, pointer_path=self.db_path)
        self.assertEqual(attempt, attempt_thread_id(self.visible, 1))
        self.assertEqual(self.app.get_state(_config(attempt)).values, {"log": ["u1", "node"]})
        self.assertEqual(self.app.get_state(_config(self.visible)).values, {"log": ["u1", "node"]})
        # Preparing again is idempotent.
        self.assertEqual(
            prepare_attempt(self.saver, self.visible, 1, pointer_path=self.db_path), attempt
        )

    def test_commit_moves_pointer_and_next_attempt_forks_committed_state(self):
        self._seed_canonical()
        attempt_one = prepare_attempt(self.saver, self.visible, 1, pointer_path=self.db_path)
        self.app.invoke({"log": ["u2"]}, _config(attempt_one))
        self.assertTrue(commit_attempt(self.saver, self.visible, attempt_one, 1, pointer_path=self.db_path))

        attempt_two = prepare_attempt(self.saver, self.visible, 2, pointer_path=self.db_path)
        self.assertEqual(
            self.app.get_state(_config(attempt_two)).values,
            {"log": ["u1", "node", "u2", "node"]},
        )

    def test_stale_commit_is_rejected_after_newer_pointer(self):
        self._seed_canonical()
        attempt_one = prepare_attempt(self.saver, self.visible, 1, pointer_path=self.db_path)
        self.assertTrue(commit_attempt(self.saver, self.visible, attempt_one, 1, pointer_path=self.db_path))
        attempt_two = prepare_attempt(self.saver, self.visible, 2, pointer_path=self.db_path)
        self.assertTrue(commit_attempt(self.saver, self.visible, attempt_two, 2, pointer_path=self.db_path))

        self.app.invoke({"log": ["late"]}, _config(attempt_one))
        with self.assertRaises(StaleAttemptCommit):
            commit_attempt(self.saver, self.visible, attempt_one, 1, pointer_path=self.db_path)
        # Canonical pointer still reflects attempt two.
        self.assertEqual(
            self.app.get_state(_config(self.visible)).values,
            {"log": ["u1", "node"]},
        )
        import sqlite3 as _sqlite3
        from contextlib import closing

        with closing(_sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT fencing_token FROM hdb_checkpoint_pointers WHERE visible_thread = ?",
                (self.visible,),
            ).fetchone()
        self.assertEqual(int(row[0]), 2)

    def test_purge_removes_superseded_attempts(self):
        self._seed_canonical()
        attempt_one = prepare_attempt(self.saver, self.visible, 1, pointer_path=self.db_path)
        commit_attempt(self.saver, self.visible, attempt_one, 1, pointer_path=self.db_path)
        attempt_two = prepare_attempt(self.saver, self.visible, 2, pointer_path=self.db_path)
        commit_attempt(self.saver, self.visible, attempt_two, 2, pointer_path=self.db_path)
        self.assertIsNone(self.saver.get_tuple(_config(attempt_one)))
        self.assertIsNotNone(self.saver.get_tuple(_config(attempt_two)))
        removed = purge_superseded_attempts(self.saver, self.visible, 2, pointer_path=self.db_path)
        self.assertEqual(removed, 0)

    def test_forget_removes_visible_attempts_and_pointer(self):
        self._seed_canonical()
        attempt_one = prepare_attempt(self.saver, self.visible, 1, pointer_path=self.db_path)
        commit_attempt(self.saver, self.visible, attempt_one, 1, pointer_path=self.db_path)
        attempt_two = prepare_attempt(self.saver, self.visible, 2, pointer_path=self.db_path)
        forget_thread_state(self.saver, self.visible, pointer_path=self.db_path)
        self.assertIsNone(self.saver.get_tuple(_config(self.visible)))
        self.assertIsNone(self.saver.get_tuple(_config(attempt_two)))
        import sqlite3 as _sqlite3
        from contextlib import closing

        with closing(_sqlite3.connect(self.db_path)) as conn:
            pointers = conn.execute(
                "SELECT COUNT(*) FROM hdb_checkpoint_pointers WHERE visible_thread = ?",
                (self.visible,),
            ).fetchone()[0]
            attempts = conn.execute(
                "SELECT COUNT(*) FROM hdb_checkpoint_attempts WHERE visible_thread = ?",
                (self.visible,),
            ).fetchone()[0]
        self.assertEqual((int(pointers), int(attempts)), (0, 0))

    def test_legacy_token_zero_is_a_noop(self):
        self._seed_canonical()
        self.assertEqual(prepare_attempt(self.saver, self.visible, 0, pointer_path=self.db_path), self.visible)
        self.assertFalse(
            commit_attempt(self.saver, self.visible, self.visible, 0, pointer_path=self.db_path)
        )

    def test_runner_forget_thread_purges_attempts(self):
        from src.agents import runner as runner_mod

        original_path = src.settings.AGENT_CHECKPOINT_DB_PATH
        src.settings.AGENT_CHECKPOINT_DB_PATH = self.db_path
        reset_checkpointer()
        try:
            checkpointer = get_checkpointer()
            app = _build_app(checkpointer)
            app.invoke({"log": ["u1"]}, _config(self.visible))
            attempt = prepare_attempt(checkpointer, self.visible, 1)
            self.assertNotEqual(attempt, self.visible)
            self.assertTrue(runner_mod.forget_thread(self.visible))
            self.assertIsNone(checkpointer.get_tuple(_config(self.visible)))
            self.assertIsNone(checkpointer.get_tuple(_config(attempt)))
        finally:
            reset_checkpointer()
            src.settings.AGENT_CHECKPOINT_DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
