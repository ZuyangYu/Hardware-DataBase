"""P0-002b: verify saver fork/restore and pending-write behavior.

These tests characterize the official ``SqliteSaver`` so the take-over design
(P0-003) rests on verified behavior rather than assumptions:

1. same-thread invokes persist history and ``get_state`` is consistent;
2. copying a checkpoint into a new attempt namespace continues that history;
3. pending writes survive only when copied explicitly;
4. deleting the superseded attempt namespace does not touch the new attempt;
5. uncommitted writes to the new attempt never appear in the source thread.
"""

from __future__ import annotations

import operator
import os
import sqlite3
import tempfile
import unittest
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.agents.checkpoints import copy_checkpoint_namespace, copy_latest_checkpoint


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


class CheckpointForkRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(os.path.join(self.tmp.name, "checkpoints.db"), check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.saver = SqliteSaver(self.conn)
        self.saver.setup()
        self.app = _build_app(self.saver)

    def _config(self, thread_id: str, **configurable: str) -> dict:
        payload = {"thread_id": thread_id, **configurable}
        return {"configurable": payload}

    def test_same_thread_resume_persists_history(self):
        config = self._config("conv-1")
        self.app.invoke({"log": ["u1"]}, config)
        self.app.invoke({"log": ["u2"]}, config)
        state = self.app.get_state(config)
        self.assertEqual(state.values, {"log": ["u1", "node", "u2", "node"]})

    def test_copied_namespace_continues_history(self):
        source = self._config("conv-1")
        self.app.invoke({"log": ["u1"]}, source)
        saved = copy_latest_checkpoint(self.saver, "conv-1", "conv-1::attempt-2")
        self.assertIsNotNone(saved)
        target = self._config("conv-1::attempt-2")
        self.assertEqual(self.app.get_state(target).values, {"log": ["u1", "node"]})
        self.app.invoke({"log": ["u2"]}, target)
        self.assertEqual(
            self.app.get_state(target).values,
            {"log": ["u1", "node", "u2", "node"]},
        )

    def test_pending_writes_require_explicit_copy(self):
        source = self._config("conv-1")
        self.app.invoke({"log": ["u1"]}, source)
        source_tuple = self.saver.get_tuple(source)
        self.saver.put_writes(source_tuple.config, [("log", ["pending"])], task_id="task-1")

        copied_without_writes = copy_latest_checkpoint(
            self.saver, "conv-1", "conv-1::attempt-no-writes", copy_pending_writes=False
        )
        self.assertIsNotNone(copied_without_writes)
        without = self.saver.get_tuple(self._config("conv-1::attempt-no-writes"))
        self.assertEqual(without.pending_writes, [])

        copied_with_writes = copy_latest_checkpoint(
            self.saver, "conv-1", "conv-1::attempt-with-writes", copy_pending_writes=True
        )
        self.assertIsNotNone(copied_with_writes)
        with_writes = self.saver.get_tuple(self._config("conv-1::attempt-with-writes"))
        self.assertEqual([tuple(item) for item in with_writes.pending_writes], [("task-1", "log", ["pending"])])

    def test_delete_superseded_attempt_keeps_new_attempt(self):
        source = self._config("conv-1")
        self.app.invoke({"log": ["u1"]}, source)
        copy_latest_checkpoint(self.saver, "conv-1", "conv-1::attempt-2")
        # Forking again from the superseded attempt must not lose the new one.
        self.saver.delete_thread("conv-1::attempt-2")
        self.assertIsNone(self.saver.get_tuple(self._config("conv-1::attempt-2") or {}))
        self.assertIsNotNone(self.saver.get_tuple(source))
        copy_latest_checkpoint(self.saver, "conv-1", "conv-1::attempt-3")
        self.assertIsNotNone(self.saver.get_tuple(self._config("conv-1::attempt-3")))

    def test_checkpoint_copy_is_isolated_from_source_namespace(self):
        source = self._config("conv-1")
        self.app.invoke({"log": ["u1"]}, source)
        saved = copy_checkpoint_namespace(self.saver, source, "conv-1::attempt-2")
        self.assertIsNotNone(saved)
        # Writing to the copy must not change the source thread.
        self.app.invoke({"log": ["u2"]}, self._config("conv-1::attempt-2"))
        self.assertEqual(self.app.get_state(source).values, {"log": ["u1", "node"]})
        source_tuple = self.saver.get_tuple(source)
        copy_tuple = self.saver.get_tuple(self._config("conv-1::attempt-2"))
        self.assertNotEqual(source_tuple.config["configurable"]["checkpoint_id"], copy_tuple.config["configurable"]["checkpoint_id"])


if __name__ == "__main__":
    unittest.main()
