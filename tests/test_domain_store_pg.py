"""PG parity for the domain stores (storage-convergence phase 1).

``PipelineDocumentStore`` gains a PostgreSQL backend via
``src/storage/domain_pg.py``: tables are provisioned from the store's own
SQLite bootstrap with the *mechanical* type mapping (TEXT timestamps keep the
"''"-sentinel semantics), and the same statements then run through
``PgSqlAdapterConnection``.

Each test runs the same operations against SQLite and PostgreSQL and compares
the results (read parity).  They use the project-managed PostgreSQL instance
(``HDB_TEST_POSTGRES_DSN``) and a throwaway database per run, so they never
touch real data.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import datetime

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings as app_settings
from src.pipelines.document_store import PipelineDocumentStore
from src.services.pipeline_directories import PipelineDirectoryService
from src.storage import domain_pg

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = name
    return make_conninfo(**values)


def _uuid_hex() -> str:

    return uuid.uuid4().hex[:8]


class _DomainParityCase(unittest.TestCase):
    """Shared setup: throwaway PG database + settings cleanup."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_domain_parity_{_uuid_hex()}"
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{cls.database}"')
        finally:
            admin.close()
        cls.dsn = _dsn_with_database(DSN, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{cls.database}" WITH (FORCE)')
        finally:
            admin.close()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._settings_backup = [
            ("CONTROL_PLANE_BACKEND", app_settings.CONTROL_PLANE_BACKEND),
            ("CONTROL_PLANE_DATABASE_URL", app_settings.CONTROL_PLANE_DATABASE_URL),
            ("STORAGE_DIR", app_settings.STORAGE_DIR),
            ("PIPELINE_DOCUMENT_STORE_BACKEND", app_settings.PIPELINE_DOCUMENT_STORE_BACKEND),
            ("PIPELINE_DOCUMENT_DATABASE_URL", app_settings.PIPELINE_DOCUMENT_DATABASE_URL),
            (
                "PIPELINE_DOCUMENT_STORE_BACKEND",
                app_settings.PIPELINE_DOCUMENT_STORE_BACKEND,
            ),
            (
                "PIPELINE_DOCUMENT_DATABASE_URL",
                app_settings.PIPELINE_DOCUMENT_DATABASE_URL,
            ),
        ]
        for name, value in self._settings_backup:
            self.addCleanup(setattr, app_settings, name, value)
        app_settings.CONTROL_PLANE_BACKEND = "postgres"
        app_settings.CONTROL_PLANE_DATABASE_URL = self.dsn
        # 数据端守卫按 STORAGE_DIR 找旧 SQLite 文件；测试里指到临时目录，
        # 守卫视为"全新部署"直接放行。
        app_settings.STORAGE_DIR = self.tmp.name
        # conftest 会把这些键强制成 sqlite 做隔离；本套件要验证"跟随控制面"。
        app_settings.PIPELINE_DOCUMENT_STORE_BACKEND = ""
        app_settings.PIPELINE_DOCUMENT_DATABASE_URL = ""

    def _sqlite_twin(self, store_cls: type, name: str):
        return store_cls(db_path=os.path.join(self.tmp.name, name))

    def _pg_store(self, store_cls: type):
        # 临时目录里没有同名 SQLite 文件 => 数据端守卫直接放行（全新部署）。
        return store_cls()

    @staticmethod
    def _dump(store) -> dict:
        return {key: _serializable(value) for key, value in store.__dict__.items()}


def _serializable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    return value


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")

@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class PipelineDocumentStoreParityTests(_DomainParityCase):
    def _seed(self, store) -> object:
        store.save_dataset("design", "dataset-1", "ADAS_new")
        store.upsert_document(
            kb_name="kb",
            document_name="a.xlsx",
            dataset_kind="table",
            dataset_id="dataset-1",
            document_id="doc-a",
            source_group="docs",
            department_id="dept_a",
            uploaded_by="user_a",
            kb_id=42,
            status="archived",
            processor_kind="spreadsheet_table",
        )
        return store.get_document("kb", "a.xlsx", "table", department_id="dept_a")

    def test_upsert_get_dataset_and_stats_parity(self) -> None:
        sqlite_store = self._sqlite_twin(PipelineDocumentStore, "parity-pipeline-sqlite.db")
        pg_store = self._pg_store(PipelineDocumentStore)

        sqlite_record = self._seed(sqlite_store)
        pg_record = self._seed(pg_store)

        self.assertEqual(sqlite_record.kb_id, pg_record.kb_id)
        self.assertEqual(sqlite_record.status, pg_record.status)
        self.assertEqual(
            sqlite_store.get_dataset("design"), pg_store.get_dataset("design")
        )
        self.assertEqual(
            sqlite_store.document_stats_by_kb("dept_a").keys(),
            pg_store.document_stats_by_kb("dept_a").keys(),
        )
        self.assertEqual(
            sqlite_store.document_stats_by_kb("dept_a"),
            pg_store.document_stats_by_kb("dept_a"),
        )
        self.assertIsNone(sqlite_store.get_dataset("missing"))
        self.assertIsNone(pg_store.get_dataset("missing"))

    def test_claim_next_parse_record_parity(self) -> None:
        sqlite_store = self._sqlite_twin(PipelineDocumentStore, "parity-claim-sqlite.db")
        pg_store = self._pg_store(PipelineDocumentStore)
        self._seed(sqlite_store)
        self._seed(pg_store)

        sqlite_claim = sqlite_store.claim_next_parse_record("worker-sqlite")
        pg_claim = pg_store.claim_next_parse_record("worker-pg")

        self.assertIsNotNone(sqlite_claim)
        self.assertIsNotNone(pg_claim)
        self.assertEqual(sqlite_claim.status, pg_claim.status)
        self.assertEqual(sqlite_claim.worker_id, "worker-sqlite")
        self.assertEqual(pg_claim.worker_id, "worker-pg")
        # 已被认领的记录不会被同一 worker 再次认领。
        self.assertIsNone(sqlite_store.claim_next_parse_record("worker-sqlite-2"))
        self.assertIsNone(pg_store.claim_next_parse_record("worker-pg-2"))

    def test_parse_completed_flow_parity(self) -> None:
        sqlite_store = self._sqlite_twin(PipelineDocumentStore, "parity-parse-sqlite.db")
        pg_store = self._pg_store(PipelineDocumentStore)
        self._seed(sqlite_store)
        self._seed(pg_store)
        for store in (sqlite_store, pg_store):
            record = store.get_document("kb", "a.xlsx", "table", department_id="dept_a")
            store.update_document_progress_by_id(
                record.id,
                100,
                "解析完成",
                status="parsed",
            )

        sqlite_record = sqlite_store.get_document("kb", "a.xlsx", "table", department_id="dept_a")
        pg_record = pg_store.get_document("kb", "a.xlsx", "table", department_id="dept_a")
        self.assertEqual(sqlite_record.status, pg_record.status)
        self.assertEqual(sqlite_record.parse_stage, pg_record.parse_stage)
        self.assertEqual(sqlite_record.parse_progress, pg_record.parse_progress)
        # CURRENT_TIMESTAMP 改写：PG 侧时间戳必须是可解析的 ISO 文本
        self.assertParseTimestamp(pg_record.updated_at)

    def assertParseTimestamp(self, value: str) -> None:
        self.assertIsNotNone(value)
        datetime.fromisoformat(str(value))

    def test_same_basename_is_allowed_in_different_directories(self) -> None:
        """The legacy path unique index must not survive the directory upgrade."""

        store = self._pg_store(PipelineDocumentStore)
        directories = PipelineDirectoryService(store=store)
        first = directories.create(
            kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
            name="docs", created_by="tester",
        )
        second = directories.create(
            kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
            name="design", created_by="tester",
        )
        for directory, suffix in ((first, "docs"), (second, "design")):
            store.upsert_document(
                kb_name="kb",
                document_name="spec.pdf",
                original_file_name="spec.pdf",
                dataset_kind="design",
                dataset_id="dataset",
                document_id=f"remote-{suffix}",
                source_group="文档资料",
                department_id="dept",
                uploaded_by="tester",
                kb_id=7,
                directory_id=directory["id"],
                status="parsed",
                content_hash=f"hash-{suffix}",
            )
        records = store.list_documents("kb", "dept")
        assert len(records) == 2
        assert {record.directory_id for record in records} == {
            first["id"],
            second["id"],
        }

    def test_stale_table_without_identity_is_repaired_on_provision(self) -> None:
        """存量表缺 identity 时自愈：补 identity + 序列推进，之后插入不再撞 NOT NULL。

        回归背景：PG 迁移早期建出的 pipeline_* 表没有 ``GENERATED BY DEFAULT AS
        IDENTITY``，应用不带 id 的插入（状态刷新事件/新上传记录）会直接失败。
        ``CREATE TABLE IF NOT EXISTS`` 不会纠正旧形状，因此补在 provision 里。
        """

        with psycopg.connect(self.dsn, autocommit=True) as admin:
            # 同类测试共享一次性库；先丢掉既有的规范表，重建为"旧形状"（无 identity）。
            admin.execute("DROP TABLE IF EXISTS pipeline_profile_outbox")
            admin.execute(
                """
                CREATE TABLE pipeline_profile_outbox (
                    id BIGINT NOT NULL,
                    record_id BIGINT,
                    event_kind TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT,
                    PRIMARY KEY (id)
                )
                """
            )
            admin.execute(
                "INSERT INTO pipeline_profile_outbox (id, event_kind) VALUES (7, 'legacy')"
            )

        # 构造 store 触发 provision；清缓存确保对本测试的库真实执行一次。
        domain_pg._PROVISIONED.clear()
        store = self._pg_store(PipelineDocumentStore)
        # 修复前这里会抛 not-null violation（id 无默认值）。
        store.record_profile_event(None, "repaired")

        with psycopg.connect(self.dsn) as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM pipeline_profile_outbox ORDER BY id"
                ).fetchall()
            ]
            identity = conn.execute(
                """
                SELECT is_identity FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'pipeline_profile_outbox'
                  AND column_name = 'id'
                """
            ).fetchone()[0]
        # 既有行（id=7）保留，新插入拿到 8（序列已推进到 max+1）。
        self.assertEqual(ids, [7, 8])
        self.assertEqual(identity, "YES")


if __name__ == "__main__":
    unittest.main()


class _FakeColumnConn:
    def __init__(self, existing: set[str]):
        self.existing = existing
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None):
        if "information_schema.columns" in sql:
            rows = [(name,) for name in sorted(self.existing)]

            class _Rows(list):
                def fetchall(self):
                    return list(self)

            return _Rows(rows)
        self.statements.append((sql, params))
        return []


def test_add_missing_column_uses_mechanical_default():
    """Upgraded PG tables get new columns before indexes reference them."""

    conn = _FakeColumnConn({"id", "kb_name"})
    domain_pg._add_missing_columns(
        conn,
        "pipeline_documents",
        [
            {"name": "id", "type": "INTEGER", "pk": 1, "notnull": 1, "dflt_value": None},
            {
                "name": "directory_id",
                "type": "INTEGER",
                "pk": 0,
                "notnull": 1,
                "dflt_value": "0",
            },
        ],
    )

    assert len(conn.statements) == 1
    sql = conn.statements[0][0]
    assert 'ADD COLUMN "directory_id"' in sql
    assert "BIGINT" in sql
    assert "NOT NULL" in sql
    assert "DEFAULT 0" in sql


class _FakeIndexConn:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None):
        if "pg_indexes" in sql:
            rows = [
                (
                    "ux_pipeline_documents_kb_name_department_id_document_name_datas",
                    "CREATE UNIQUE INDEX ux_pipeline_documents_kb_name_department_id_document_name_datas "
                    "ON public.pipeline_documents USING btree (kb_name, department_id, document_name, dataset_kind)",
                ),
                (
                    "ux_pipeline_documents_kb_name_department_id_directory_id_docume",
                    "CREATE UNIQUE INDEX ux_pipeline_documents_kb_name_department_id_directory_id_docume "
                    "ON public.pipeline_documents USING btree (kb_name, department_id, directory_id, document_name)",
                ),
            ]

            class _Rows(list):
                def fetchall(self):
                    return list(self)

            return _Rows(rows)
        self.statements.append(sql)
        return []


def test_legacy_document_name_unique_index_is_dropped():
    conn = _FakeIndexConn()
    domain_pg._drop_superseded_pipeline_document_indexes(conn, "pipeline_documents")

    assert len(conn.statements) == 1
    assert "DROP INDEX" in conn.statements[0]
    assert "document_name_datas" in conn.statements[0]
    assert "directory_id" not in conn.statements[0]
