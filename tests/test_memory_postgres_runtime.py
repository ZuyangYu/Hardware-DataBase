"""P1: PostgreSQL memory projection runtime (rebuildable store)."""

from __future__ import annotations

import os
import unittest

import src.settings
from src.memory.store import (
    PostgresMemoryStoreRuntime,
    build_memory_index_config,
    create_memory_store,
)


class MemoryPostgresRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._original_backend = src.settings.MEMORY_STORE_BACKEND
        self._original_url = src.settings.MEMORY_DATABASE_URL
        self.addCleanup(setattr, src.settings, "MEMORY_STORE_BACKEND", self._original_backend)
        self.addCleanup(setattr, src.settings, "MEMORY_DATABASE_URL", self._original_url)

    def test_postgres_without_url_is_an_explicit_error(self):
        src.settings.MEMORY_STORE_BACKEND = "postgres"
        src.settings.MEMORY_DATABASE_URL = ""
        with self.assertRaises(RuntimeError):
            create_memory_store()

    def test_unknown_backend_is_rejected(self):
        src.settings.MEMORY_STORE_BACKEND = "mysql"
        with self.assertRaises(ValueError):
            create_memory_store()

    def test_sqlite_backend_still_builds(self):
        src.settings.MEMORY_STORE_BACKEND = "sqlite"
        runtime = create_memory_store()
        try:
            self.assertTrue(runtime.health()["ok"])
        finally:
            runtime.close()

    def test_large_embedding_falls_back_to_exact_search(self):
        # pgvector 的 hnsw/ivfflat 上限 2000 维：2048 维必须走 flat，
        # 否则 langgraph setup() 建索引失败并静默降级成无语义检索。
        config = build_memory_index_config(src.settings, dims=2048, embedding=object())
        self.assertEqual(config["ann_index_config"], {"kind": "flat"})
        self.assertEqual(config["dims"], 2048)

    def test_small_embedding_keeps_default_ann_index(self):
        config = build_memory_index_config(src.settings, dims=1536, embedding=object())
        self.assertNotIn("ann_index_config", config)

    def test_explicit_index_kind_is_honored(self):
        original = src.settings.MEMORY_EMBEDDING_INDEX_KIND
        self.addCleanup(setattr, src.settings, "MEMORY_EMBEDDING_INDEX_KIND", original)
        src.settings.MEMORY_EMBEDDING_INDEX_KIND = "hnsw"
        config = build_memory_index_config(src.settings, dims=2048, embedding=object())
        self.assertEqual(config["ann_index_config"], {"kind": "hnsw"})

    @unittest.skipUnless(
        os.environ.get("HDB_TEST_POSTGRES_DSN", ""), "HDB_TEST_POSTGRES_DSN is not configured"
    )
    def test_postgres_round_trip(self):
        runtime = PostgresMemoryStoreRuntime(database_url=os.environ["HDB_TEST_POSTGRES_DSN"])
        try:
            self.assertTrue(runtime.health()["ok"])
            namespace = ("memory", "test", "candidate")
            runtime.put(namespace, "m1", {"content": {"content": "隔离电压 5kV"}})
            items = runtime.search(namespace, query="隔离电压", limit=5)
            self.assertTrue(any(getattr(item, "key", "") == "m1" for item in items))
            namespaces = runtime.list_namespaces(prefix=("memory", "test"), limit=10)
            self.assertIn(namespace, namespaces)
            runtime.delete(namespace, "m1")
            self.assertFalse(
                any(getattr(item, "key", "") == "m1" for item in runtime.search(namespace, limit=5))
            )
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
