"""v3 §5.2: control-plane backend selection stays fail-closed."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from src.core.conversation import ConversationService
from src.settings import validate_provider_configuration
from src.storage.control_plane.conversation_service import PostgresConversationService
from src.storage.control_plane.factory import (
    build_conversation_service,
    build_execution_store,
    close_conversation_services,
)
from src.storage.control_plane.postgres import PostgresExecutionStore
from src.storage.control_plane.sqlite import SqliteExecutionStore

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


class ControlPlaneFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_sqlite_backend_is_the_default(self):
        settings = SimpleNamespace(
            CONTROL_PLANE_BACKEND="sqlite",
            AUTH_DB_PATH=os.path.join(self.tmp.name, "auth.db"),
        )
        store = build_execution_store(settings)
        self.assertIsInstance(store, SqliteExecutionStore)
        store.close()

    def test_unknown_backend_is_rejected(self):
        settings = SimpleNamespace(CONTROL_PLANE_BACKEND="mysql")
        with self.assertRaises(RuntimeError):
            build_execution_store(settings)

    def test_postgres_backend_requires_an_explicit_url(self):
        settings = SimpleNamespace(CONTROL_PLANE_BACKEND="postgres")
        with self.assertRaises(RuntimeError):
            build_execution_store(settings)

    def test_conversation_service_dispatches_sqlite_and_postgres(self):
        sqlite_settings = SimpleNamespace(
            CONTROL_PLANE_BACKEND="sqlite",
            AUTH_DB_PATH=os.path.join(self.tmp.name, "auth.db"),
        )
        service = build_conversation_service(sqlite_settings)
        self.assertIsInstance(service, ConversationService)
        self.assertIsInstance(ConversationService(sqlite_settings.AUTH_DB_PATH), ConversationService)

    @unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
    def test_postgres_conversation_service_builds_from_settings(self):
        settings = SimpleNamespace(
            CONTROL_PLANE_BACKEND="postgres",
            CONTROL_PLANE_DATABASE_URL=DSN,
            CONTROL_PLANE_POOL_SIZE=1,
        )
        service = build_conversation_service(settings)
        try:
            self.assertIsInstance(service, PostgresConversationService)
            self.assertIs(build_conversation_service(settings), service)
        finally:
            service.close()
        replacement = build_conversation_service(settings)
        try:
            self.assertIsNot(replacement, service)
            self.assertFalse(replacement.closed)
        finally:
            close_conversation_services()

    @unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
    def test_postgres_backend_builds_from_settings(self):
        settings = SimpleNamespace(
            CONTROL_PLANE_BACKEND="postgres",
            CONTROL_PLANE_DATABASE_URL=DSN,
            CONTROL_PLANE_POOL_SIZE=2,
        )
        store = build_execution_store(settings)
        try:
            self.assertIsInstance(store, PostgresExecutionStore)
        finally:
            store.close()

    @unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
    def test_runtime_factories_dispatch_to_postgres(self):
        import src.settings as app_settings
        from src.memory.catalog import MemoryCatalogRepository
        from src.memory.jobs import MemoryJobRepository
        from src.result_exports.store import ResultExportStore
        from src.storage.control_plane.memory_worker import (
            PostgresMemoryCatalogRepository,
            PostgresMemoryJobRepository,
        )
        from src.storage.control_plane.result_exports import (
            PostgresResultExportStore,
        )

        for name in (
            "CONTROL_PLANE_BACKEND",
            "CONTROL_PLANE_DATABASE_URL",
            "CONTROL_PLANE_POOL_SIZE",
        ):
            self.addCleanup(setattr, app_settings, name, getattr(app_settings, name))
        app_settings.CONTROL_PLANE_BACKEND = "postgres"
        app_settings.CONTROL_PLANE_DATABASE_URL = DSN
        app_settings.CONTROL_PLANE_POOL_SIZE = 1

        jobs = MemoryJobRepository()
        try:
            self.assertIsInstance(jobs, PostgresMemoryJobRepository)
        finally:
            jobs._connect().close()
        catalog = MemoryCatalogRepository()
        try:
            self.assertIsInstance(catalog, PostgresMemoryCatalogRepository)
        finally:
            catalog.close()
        exports = ResultExportStore(storage_dir=os.path.join(self.tmp.name, "exports"))
        self.assertIsInstance(exports, PostgresResultExportStore)
        service = ConversationService()
        try:
            self.assertIsInstance(service, PostgresConversationService)
        finally:
            service.close()


class ControlPlaneGuardTests(unittest.TestCase):
    def test_unknown_backend_is_rejected_by_the_guard(self):
        with self.assertRaises(ValueError):
            validate_provider_configuration({"CONTROL_PLANE_BACKEND": "bogus"})

    def test_postgres_requires_url_and_passes_with_it(self):
        with self.assertRaises(ValueError) as missing_url:
            validate_provider_configuration({"CONTROL_PLANE_BACKEND": "postgres"})
        self.assertIn("CONTROL_PLANE_DATABASE_URL", str(missing_url.exception))

        # v3 PR-3：门面分派、记忆账本与导出账本 PG 化 + 停写切换预演完成后，
        # 显式配置 DSN 即可启用；缺失 schema 由 readiness fail-closed 拒绝。
        validate_provider_configuration(
            {
                "CONTROL_PLANE_BACKEND": "postgres",
                "CONTROL_PLANE_DATABASE_URL": "postgresql://user:pass@example/hdb",
            }
        )

    def test_sqlite_backend_passes_the_guard(self):
        validate_provider_configuration({"CONTROL_PLANE_BACKEND": "sqlite"})


if __name__ == "__main__":
    unittest.main()
