"""P1-001: checkpointer provider assembly, backend selection and settings."""

from __future__ import annotations

import os
import tempfile
import unittest

import src.settings
from src.agents.checkpoints import (
    PostgresCheckpointerProvider,
    SqliteCheckpointerProvider,
    build_checkpointer_provider,
    get_checkpointer,
    normalize_backend,
    reset_checkpointer,
)


class CheckpointerProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._original_backend = src.settings.AGENT_CHECKPOINT_BACKEND
        self._original_path = src.settings.AGENT_CHECKPOINT_DB_PATH
        self._original_url = src.settings.AGENT_CHECKPOINT_DATABASE_URL
        self.addCleanup(setattr, src.settings, "AGENT_CHECKPOINT_BACKEND", self._original_backend)
        self.addCleanup(setattr, src.settings, "AGENT_CHECKPOINT_DB_PATH", self._original_path)
        self.addCleanup(setattr, src.settings, "AGENT_CHECKPOINT_DATABASE_URL", self._original_url)
        self.addCleanup(reset_checkpointer)
        reset_checkpointer()

    def test_normalize_backend(self):
        self.assertEqual(normalize_backend("POSTGRES"), "postgres")
        self.assertEqual(normalize_backend(" postgres "), "postgres")
        self.assertEqual(normalize_backend("bogus"), "sqlite")
        self.assertEqual(normalize_backend(None), "sqlite")

    def test_sqlite_provider_round_trip(self):
        path = os.path.join(self.tmp.name, "agent_checkpoints.db")
        provider = SqliteCheckpointerProvider(path)
        saver = provider.create()
        saver.setup()
        saver.delete_thread("missing-thread")
        self.assertTrue(os.path.isfile(path))
        provider.close()

    def test_factory_singleton_follows_settings(self):
        src.settings.AGENT_CHECKPOINT_BACKEND = "sqlite"
        src.settings.AGENT_CHECKPOINT_DB_PATH = os.path.join(self.tmp.name, "singleton.db")
        first = get_checkpointer()
        self.assertIs(first, get_checkpointer())
        reset_checkpointer()
        second = get_checkpointer()
        self.assertIsNot(first, second)

    def test_factory_defaults_to_sqlite_for_unknown_backend(self):
        provider = build_checkpointer_provider(backend="mysql", sqlite_path=os.path.join(self.tmp.name, "x.db"))
        self.assertIsInstance(provider, SqliteCheckpointerProvider)

    def test_postgres_provider_requires_database_url(self):
        provider = PostgresCheckpointerProvider("")
        with self.assertRaises(ValueError):
            provider.create()

    def test_closed_postgres_provider_revalidates_schema_before_reopening(self):
        from unittest.mock import patch
        from src.agents.checkpoints.postgres_provider import CheckpointSchemaNotReady

        provider = PostgresCheckpointerProvider("postgresql://unused/database")
        provider._verified = True
        provider.close()
        with patch("src.agents.checkpoints.postgres_provider.verify_checkpoint_schema",
                   side_effect=CheckpointSchemaNotReady("schema removed after close")):
            with self.assertRaisesRegex(CheckpointSchemaNotReady, "schema removed"):
                provider.create()

    def test_settings_validation_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            src.settings.validate_settings_values({"AGENT_CHECKPOINT_BACKEND": "mysql"})
        src.settings.validate_settings_values({"AGENT_CHECKPOINT_BACKEND": "postgres"})



class PostgresCheckpointerIntegrationTests(unittest.TestCase):
    """Opt-in: set HDB_TEST_POSTGRES_DSN to run against a real database."""

    dsn = os.environ.get("HDB_TEST_POSTGRES_DSN", "")

    @unittest.skipUnless(dsn, "HDB_TEST_POSTGRES_DSN is not configured")
    def test_postgres_saver_round_trip(self):
        from src.agents.checkpoints.postgres_provider import ensure_checkpoint_schema

        ensure_checkpoint_schema(self.dsn)  # 与生产一致：schema 先由 migrate 建好
        provider = PostgresCheckpointerProvider(self.dsn)
        saver = provider.create()
        try:
            saver.setup()
            saver.delete_thread("hdb-p1-provider-smoke")
        finally:
            provider.close()


if __name__ == "__main__":
    unittest.main()
