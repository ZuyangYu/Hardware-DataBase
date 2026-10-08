"""v3 §10: per-graph savers over one PostgreSQL pool; SQLite stays shared.

A PostgreSQL saver carries its own instance lock, so a process-level singleton
serializes every concurrent graph's checkpoint access.  ``create_checkpointer``
returns a fresh saver per graph share of the provider's pool; SQLite keeps the
singleton because one connection must stay behind one saver lock.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
import uuid

from langgraph.checkpoint.base import empty_checkpoint

import src.settings as settings
from src.agents.checkpoints import (
    create_checkpointer,
    get_checkpointer,
    get_checkpointer_provider,
    reset_checkpointer,
)

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


class _BackendSettingsMixin:
    def _configure(self, **values):
        self._original = {
            key: getattr(settings, key, None)
            for key in (
                "AGENT_CHECKPOINT_BACKEND",
                "AGENT_CHECKPOINT_DB_PATH",
                "AGENT_CHECKPOINT_DATABASE_URL",
            )
        }
        self.addCleanup(self._restore)
        for key, value in values.items():
            setattr(settings, key, value)
        reset_checkpointer()
        self.addCleanup(reset_checkpointer)

    def _restore(self):
        for key, value in self._original.items():
            setattr(settings, key, value)


class SqlitePerGraphTests(_BackendSettingsMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._configure(
            AGENT_CHECKPOINT_BACKEND="sqlite",
            AGENT_CHECKPOINT_DB_PATH=os.path.join(self.tmp.name, "checkpoints.db"),
        )

    def test_sqlite_keeps_the_process_singleton(self):
        self.assertIs(create_checkpointer(), get_checkpointer())


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class PostgresPerGraphTests(_BackendSettingsMixin, unittest.TestCase):
    def setUp(self):
        self._configure(
            AGENT_CHECKPOINT_BACKEND="postgres",
            AGENT_CHECKPOINT_DATABASE_URL=DSN,
        )

    def test_each_graph_gets_its_own_saver_over_one_provider(self):
        first = create_checkpointer()
        second = create_checkpointer()
        self.assertIsNot(first, second)
        self.assertIsNot(first.lock, second.lock)
        provider = get_checkpointer_provider()
        self.assertIsNotNone(provider)
        self.assertIs(provider, get_checkpointer_provider())

    def test_concurrent_savers_round_trip_without_shared_instance_lock(self):
        results: list[str] = []
        errors: list[str] = []

        barrier = threading.Barrier(4)

        def worker(index: int) -> None:
            saver = None
            thread_id = f"hdb-per-graph-{os.getpid()}-{index}-{uuid.uuid4().hex}"
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            try:
                barrier.wait(timeout=10)
                saver = create_checkpointer()
                saver.put(config, empty_checkpoint(), {"step": 1, "source": "input"}, {})
                if saver.get_tuple(config) is None:
                    errors.append(f"missing checkpoint for {thread_id}")
                else:
                    results.append(thread_id)
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    if saver is not None:
                        saver.delete_thread(thread_id)
                except Exception:
                    pass

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)


if __name__ == "__main__":
    unittest.main()
