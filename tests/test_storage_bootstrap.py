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
from src.storage import auth_mirror, bootstrap, domain_pg

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def test_storage_lock_covers_separate_evaluation_database(monkeypatch):
    monkeypatch.setattr(app_settings, "EVALUATION_STORE_BACKEND", "postgres")
    monkeypatch.setattr(app_settings, "EVALUATION_STORE_DATABASE_URL", "postgresql://localhost/evaluation-test")
    assert bootstrap._postgres_dsns() == ["postgresql://localhost/evaluation-test"]


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
            ("EVALUATION_STORE_BACKEND", app_settings.EVALUATION_STORE_BACKEND),
            ("EVALUATION_STORE_DATABASE_URL", app_settings.EVALUATION_STORE_DATABASE_URL),
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
        app_settings.EVALUATION_STORE_BACKEND = ""
        app_settings.EVALUATION_STORE_DATABASE_URL = ""

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
        self.assertIn("memory_consent_events", tables)
        self.assertIn("memory_sources", tables)
        self.assertTrue({"result_snapshots", "result_export_jobs", "result_export_outbox", "result_artifacts"} <= tables)
        from src.result_exports.store import ResultExportStore

        exports = ResultExportStore(storage_dir=os.path.join(self.tmp.name, "exports"))
        self.assertEqual(exports.list_pending(), [])
        from src.result_exports.models import ResultEnvelope

        payload = dict(owner_user_id=7, tenant_id="default", session_id=3, turn_id="fresh-export",
                       envelope=ResultEnvelope(answer="stable answer", metadata={"z": "last", "a": "first"}))
        snapshot = exports.create_snapshot(**payload)
        self.assertEqual(exports.create_snapshot(**payload).snapshot_id, snapshot.snapshot_id)
        with self.assertRaisesRegex(ValueError, "immutable"):
            exports.create_snapshot(**{**payload, "envelope": ResultEnvelope(answer="changed answer")})
        # 各域的代表表（机械映射建出来的）
        self.assertIn("users", tables)
        self.assertIn("wiki_pages", tables)
        self.assertIn("projects", tables)
        self.assertIn("pipeline_documents", tables)
        self.assertIn("template_versions", tables)
        self.assertIn("document_authoring_compatibility_events", tables)
        self.assertIn("evaluation_execution_schema", tables)
        self.assertIn("evaluation_execution_owners", tables)
        self.assertTrue({"quality_schema", "quality_snapshots", "quality_cases", "quality_feedback_events", "quality_reviews", "quality_outbox"} <= tables)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            constraints = {row[0] for row in conn.execute("SELECT conname FROM pg_constraint WHERE conname IN ('fk_chat_sessions_user', 'fk_chat_messages_session', 'fk_chat_turns_session')").fetchall()}
        self.assertEqual(constraints, {"fk_chat_sessions_user", "fk_chat_messages_session", "fk_chat_turns_session"}, "first bootstrap must establish deletion cascades")

        # 幂等：再跑一次仍然成功
        self.assertEqual(bootstrap.main(), 0, "重复执行必须幂等")
        self.assertEqual(tables, self._table_names())

    def test_existing_core_only_database_gains_export_ledger(self) -> None:
        from src.storage.control_plane.schema import apply_schema
        from src.storage.control_plane.schema_version import record_schema_version

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            apply_schema(conn)
            record_schema_version(conn)
        self.assertEqual(bootstrap.main(), 0)
        self.assertIn("result_export_jobs", self._table_names())
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            unique = conn.execute(
                "SELECT indisunique FROM pg_index WHERE indexrelid = 'idx_result_export_jobs_idempotency'::regclass"
            ).fetchone()[0]
        self.assertTrue(unique)

    def test_hybrid_default_provisions_memory_export_and_auth_mirror(self) -> None:
        # 生产默认形态：认证域留在 SQLite，控制面在 PG。此时仍需要 PG 上的
        # memory / 导出账本，以及认证镜像表 users / departments。
        app_settings.AUTH_STORE_BACKEND = "sqlite"
        self.assertEqual(bootstrap.main(), 0, "混合形态初始化必须成功")
        # 第二次运行时 users 已存在，模板库的镜像才会真正写入，因此要跑两次。
        # 容器里每次 migrate 都是新进程：清掉进程内的模板/就绪缓存模拟这一点。
        with domain_pg._TEMPLATE_LOCK:
            domain_pg._TEMPLATE_CACHE.clear()
        domain_pg._PROVISIONED.clear()
        auth_mirror._COLUMNS_CACHE.clear()
        self.assertEqual(bootstrap.main(), 0, "混合形态重复初始化必须成功")

        tables = self._table_names()
        self.assertIn("memory_records", tables)
        self.assertIn("memory_projection_outbox", tables)
        self.assertIn("result_export_jobs", tables)
        self.assertIn("users", tables)
        self.assertIn("departments", tables)
        # 模板库里的管理员不得被镜像进控制面（真实管理员由 API 启动时镜像）。
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_checkpoint_schema_is_created_by_migrate_and_safe_under_concurrency(self) -> None:
        import threading

        from src.agents.checkpoints.postgres_provider import PostgresCheckpointerProvider

        for name in (
            "AGENT_CHECKPOINT_BACKEND",
            "AGENT_CHECKPOINT_DATABASE_URL",
            "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND",
            "DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL",
        ):
            self.addCleanup(setattr, app_settings, name, getattr(app_settings, name))
        app_settings.AGENT_CHECKPOINT_BACKEND = "postgres"
        app_settings.AGENT_CHECKPOINT_DATABASE_URL = self.dsn
        app_settings.DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND = "postgres"
        app_settings.DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL = self.dsn

        self.assertEqual(bootstrap.main(), 0, "检查点表必须由 migrate 建好")
        tables = self._table_names()
        self.assertIn("checkpoints", tables)
        self.assertIn("checkpoint_migrations", tables)

        # 运行期 saver 并发 setup() 必须是空操作，不得再撞 pg_type 唯一约束。
        errors: list[str] = []

        def worker() -> None:
            provider = PostgresCheckpointerProvider(self.dsn)
            try:
                provider.create()
            except Exception as exc:  # noqa: BLE001 - 收集给断言
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                provider.close()

        for _ in range(3):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertEqual(errors, [])

    def test_runtime_fails_closed_when_checkpoint_schema_is_missing(self) -> None:
        from src.agents.checkpoints.postgres_provider import (
            CheckpointSchemaNotReady,
            PostgresCheckpointerProvider,
            verify_configured_checkpoint_schemas,
        )

        app_settings.AGENT_CHECKPOINT_BACKEND = "postgres"
        app_settings.AGENT_CHECKPOINT_DATABASE_URL = self.dsn
        self.addCleanup(setattr, app_settings, "AGENT_CHECKPOINT_BACKEND", "sqlite")
        self.addCleanup(setattr, app_settings, "AGENT_CHECKPOINT_DATABASE_URL", "")

        # 全新库未经 migrate：运行期必须拒绝，而不是自己建表。
        with self.assertRaises(CheckpointSchemaNotReady) as caught:
            verify_configured_checkpoint_schemas()
        message = str(caught.exception)
        self.assertIn("python -m src.storage.bootstrap", message)  # 修复命令可直接照抄
        self.assertIn("scripts/hdb.sh", message)
        self.assertIn("migrate", message)
        self.assertIn("agent-checkpoint", message)
        provider = PostgresCheckpointerProvider(self.dsn)
        try:
            with self.assertRaisesRegex(CheckpointSchemaNotReady, "bootstrap"):
                provider.create()
        finally:
            provider.close()
        self.assertNotIn("checkpoints", self._table_names(), "运行期不得建表")

        self.assertEqual(bootstrap.main(), 0)
        self.assertEqual(verify_configured_checkpoint_schemas(), ["agent-checkpoint"])

    def test_concurrent_bootstrap_processes_are_serialized(self) -> None:
        import subprocess
        import sys

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for trial in range(3):
            database = f"hdb_bootstrap_race_{uuid.uuid4().hex[:8]}"
            admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
            try:
                admin.execute(f'CREATE DATABASE "{database}"')
            finally:
                admin.close()
            dsn = _dsn_with_database(DSN, database)
            env = dict(os.environ)
            env.update(
                {
                    "CONTROL_PLANE_BACKEND": "postgres",
                    "CONTROL_PLANE_DATABASE_URL": dsn,
                    "AUTH_STORE_BACKEND": "sqlite",
                    "AGENT_CHECKPOINT_BACKEND": "postgres",
                    "AGENT_CHECKPOINT_DATABASE_URL": dsn,
                    "STORAGE_DIR": self.tmp.name,
                    "AUTH_DB_PATH": os.path.join(self.tmp.name, f"auth-{trial}.db"),
                    "AUTH_DEFAULT_ADMIN_PASSWORD": "SmokeTest-2026-Pw",
                }
            )
            logs = [os.path.join(self.tmp.name, f"bootstrap-{trial}-{i}.log") for i in range(3)]
            handles = [open(path, "wb") for path in logs]
            try:
                procs = [
                    subprocess.Popen(
                        [sys.executable, "-m", "src.storage.bootstrap"],
                        cwd=repo_root,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                    )
                    for handle in handles
                ]
                timed_out = []
                for index, proc in enumerate(procs):
                    try:
                        proc.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        timed_out.append(index)
                        proc.kill()
                        proc.wait()
            finally:
                for handle in handles:
                    handle.close()
            try:
                output = [open(path, encoding="utf-8", errors="replace").read()[-600:] for path in logs]
                self.assertEqual(timed_out, [], f"并发 bootstrap 超时: {output}")
                self.assertEqual([proc.returncode for proc in procs], [0, 0, 0], output)
            finally:
                drop = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
                try:
                    drop.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
                finally:
                    drop.close()

    def test_memory_tables_use_semantic_types_and_repair_mechanical_shape(self) -> None:
        """memory 表必须与迁移形状一致（BOOLEAN/TIMESTAMPTZ），并自动修复旧的机械形状。"""
        # 先用机械映射建出旧形状（模拟 bootstrap 的早期实现）
        from src.storage import domain_pg

        domain_pg.provision_domain_group(
            self.dsn,
            (bootstrap._MemoryCatalogTemplate,),
            tables=["memory_sources"],
        )
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            before = conn.execute(
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_name='memory_sources' AND column_name='source_valid'"
            ).fetchone()[0]
        self.assertEqual(before, "bigint", "机械映射应为 bigint（构造旧形状失败）")

        # 再跑完整 bootstrap：应把空表重置为语义形状
        self.assertEqual(bootstrap.main(), 0)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            after = conn.execute(
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_name='memory_sources' AND column_name='source_valid'"
            ).fetchone()[0]
            # 形状修好后，控制面那条布尔查询必须能跑
            conn.execute("SELECT memory_id FROM memory_sources WHERE session_id = 1 AND source_valid")
        self.assertEqual(after, "boolean")

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
        app_settings.EVALUATION_STORE_BACKEND = "sqlite"
        self.assertEqual(bootstrap.main(), 0, "纯 SQLite 部署下应直接通过")
        self.assertEqual(self._table_names(), set(), "不该往 PG 建任何表")


if __name__ == "__main__":
    unittest.main()
