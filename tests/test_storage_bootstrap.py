"""一次性存储初始化（``python -m src.storage.bootstrap``）在全新库上的行为。

容器编排把 ``migrate`` 当作 api/worker 的启动门槛（``service_completed_successfully``），
所以这里必须钉住三件事：

- 全新 PostgreSQL：建控制面 schema + 记录版本 + 逐域建表，全部就绪；
- 幂等：再跑一次不报错、不重复建；
- 版本不符：``control_plane_schema`` 里是别的版本时 fail-closed（非零退出）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings as app_settings
from src.storage import bootstrap

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = name
    return make_conninfo(**values)


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class StorageBootstrapTests(unittest.TestCase):
    """每个测试一个全新库：模拟"新机器第一次起容器"。"""

    def setUp(self) -> None:
        self.database = f"hdb_bootstrap_{uuid.uuid4().hex[:8]}"
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{self.database}"')
        finally:
            admin.close()

        def _drop() -> None:
            admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
            try:
                admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
            finally:
                admin.close()

        self.addCleanup(_drop)
        self.dsn = _dsn_with_database(DSN, self.database)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (
            ("CONTROL_PLANE_BACKEND", app_settings.CONTROL_PLANE_BACKEND),
            ("CONTROL_PLANE_DATABASE_URL", app_settings.CONTROL_PLANE_DATABASE_URL),
            ("STORAGE_DIR", app_settings.STORAGE_DIR),
            ("AUTH_DB_PATH", app_settings.AUTH_DB_PATH),
            ("AUTH_STORE_BACKEND", app_settings.AUTH_STORE_BACKEND),
            ("AUTH_STORE_DATABASE_URL", app_settings.AUTH_STORE_DATABASE_URL),
            ("PROJECT_STORE_BACKEND", app_settings.PROJECT_STORE_BACKEND),
            ("PIPELINE_DOCUMENT_STORE_BACKEND", app_settings.PIPELINE_DOCUMENT_STORE_BACKEND),
            ("AUTHORING_STORE_BACKEND", app_settings.AUTHORING_STORE_BACKEND),
            ("COMPATIBILITY_STORE_BACKEND", app_settings.COMPATIBILITY_STORE_BACKEND),
        ):
            self.addCleanup(setattr, app_settings, name, value)
        app_settings.CONTROL_PLANE_BACKEND = "postgres"
        app_settings.CONTROL_PLANE_DATABASE_URL = self.dsn
        app_settings.STORAGE_DIR = self.tmp.name
        app_settings.AUTH_DB_PATH = os.path.join(self.tmp.name, "auth.db")
        # 域后端留空 = 跟随控制面（全新部署的默认形态）
        app_settings.AUTH_STORE_BACKEND = ""
        app_settings.AUTH_STORE_DATABASE_URL = ""
        app_settings.PROJECT_STORE_BACKEND = ""
        app_settings.PIPELINE_DOCUMENT_STORE_BACKEND = ""
        app_settings.AUTHORING_STORE_BACKEND = ""
        app_settings.COMPATIBILITY_STORE_BACKEND = ""

    def _table_names(self) -> set[str]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public'"
            ).fetchall()
        return {str(row[0]) for row in rows}

    def test_fresh_database_is_initialized_and_idempotent(self) -> None:
        self.assertEqual(bootstrap.main(), 0, "全新库初始化必须成功")

        tables = self._table_names()
        # 控制面核心表 + 版本表
        self.assertIn("control_plane_schema", tables)
        self.assertIn("chat_turns", tables)
        # 各域的代表表（机械映射建出来的）
        self.assertIn("users", tables)
        self.assertIn("wiki_pages", tables)
        self.assertIn("projects", tables)
        self.assertIn("pipeline_documents", tables)
        self.assertIn("template_versions", tables)
        self.assertIn("document_authoring_compatibility_events", tables)

        # 幂等：再跑一次仍然成功
        self.assertEqual(bootstrap.main(), 0, "重复执行必须幂等")
        self.assertEqual(tables, self._table_names())

    def test_version_mismatch_fails_closed(self) -> None:
        self.assertEqual(bootstrap.main(), 0)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE control_plane_schema SET value = '99' WHERE key = 'schema_version'"
            )
        self.assertNotEqual(bootstrap.main(), 0, "版本不符必须非零退出")

    def test_sqlite_backend_is_a_noop(self) -> None:
        app_settings.CONTROL_PLANE_BACKEND = "sqlite"
        app_settings.PROJECT_STORE_BACKEND = "sqlite"
        app_settings.PIPELINE_DOCUMENT_STORE_BACKEND = "sqlite"
        app_settings.AUTHORING_STORE_BACKEND = "sqlite"
        app_settings.AUTH_STORE_BACKEND = "sqlite"
        app_settings.COMPATIBILITY_STORE_BACKEND = "sqlite"
        self.assertEqual(bootstrap.main(), 0, "纯 SQLite 部署下应直接通过")
        self.assertEqual(self._table_names(), set(), "不该往 PG 建任何表")


if __name__ == "__main__":
    unittest.main()
