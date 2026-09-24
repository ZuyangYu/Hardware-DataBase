"""PG parity for the domain stores (storage-convergence phase 1).

``ProjectStore`` and ``PipelineDocumentStore`` gain a PostgreSQL backend via
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
from src.projects.models import (
    LogicalDocument,
    ProcessingArtifact,
    Project,
    ProjectBaseline,
    ProjectKnowledgeBinding,
    ProjectSourceBinding,
    SourceAsset,
    SourceSetSnapshot,
    SourceVersion,
)
from src.projects.store import ProjectStore

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
            ("PROJECT_STORE_BACKEND", app_settings.PROJECT_STORE_BACKEND),
            ("PROJECT_STORE_DATABASE_URL", app_settings.PROJECT_STORE_DATABASE_URL),
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
        app_settings.PROJECT_STORE_BACKEND = ""
        app_settings.PROJECT_STORE_DATABASE_URL = ""
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
class ProjectStoreParityTests(_DomainParityCase):
    def _sample_graph(self) -> dict[str, object]:
        """Build the full FK chain: project -> version/artifact -> bindings."""
        project = Project(project_id="proj-1", department_id="dept-7", name="ADAS")
        logical = LogicalDocument(
            document_id="doc-1", title="电源规格书", document_role="design",
            owner_department_id="dept-7",
        )
        asset = SourceAsset(
            asset_id="asset-1",
            original_file_name="schematic.pdf",
            content_hash="hash-1",
            content_kind="circuit",
            parser_kind="spydrnet",
            processing_status="ready",
        )
        version = SourceVersion(
            version_id="ver-1", document_id="doc-1", asset_id="asset-1"
        )
        artifact = ProcessingArtifact(
            artifact_id="art-1",
            asset_id="asset-1",
            processor_kind="spydrnet",
            processor_version="1",
            content_fingerprint="fp-1",
            status="ready",
        )
        baseline = ProjectBaseline(baseline_id="bl-1", project_id="proj-1", name="v1")
        knowledge = ProjectKnowledgeBinding(
            binding_id="pkb-1",
            project_id="proj-1",
            kb_id="kb-9",
            owner_department_id="dept-7",
            binding_type="project_private",
        )
        source_binding = ProjectSourceBinding(
            binding_id="psb-1",
            project_id="proj-1",
            version_id="ver-1",
            usage_type="project_fact",
        )
        snapshot = SourceSetSnapshot(
            source_set_snapshot_id="snap-1",
            work_order_id="wo-1",
            project_id="proj-1",
            baseline_id="bl-1",
            baseline_content_hash="hash-b1",
            baseline_item_ids=["item-1"],
            source_version_ids=["ver-1"],
            authorization_snapshot_id="auth-snap-1",
        )
        return {
            "project": project,
            "logical": logical,
            "asset": asset,
            "version": version,
            "artifact": artifact,
            "baseline": baseline,
            "knowledge": knowledge,
            "source_binding": source_binding,
            "snapshot": snapshot,
        }

    def test_full_graph_round_trip_is_identical(self) -> None:
        graph = self._sample_graph()
        sqlite_store = self._sqlite_twin(ProjectStore, "parity-projects-sqlite.db")
        pg_store = self._pg_store(ProjectStore)

        for store in (sqlite_store, pg_store):
            store.create_project(graph["project"])
            store.create_logical_document(graph["logical"])
            store.create_source_asset(graph["asset"])
            store.create_source_version(graph["version"])
            store.create_processing_artifact(graph["artifact"])
            store.add_knowledge_binding(graph["knowledge"])
            store.add_project_source_binding(graph["source_binding"])
            store.create_baseline(graph["baseline"])
            store.create_source_set_snapshot(graph["snapshot"])

        checks = {
            "project": lambda s: s.get_project("proj-1"),
            "project_list": lambda s: s.list_projects("default", "dept-7"),
            "knowledge_bindings": lambda s: s.list_knowledge_bindings("proj-1", "default"),
            "source_bindings": lambda s: s.list_project_source_bindings("proj-1", "default"),
            "version": lambda s: s.get_source_version("ver-1", "default"),
            "ready_artifacts": lambda s: s.ready_artifacts_for_version("ver-1", "default"),
            "baseline": lambda s: s.get_baseline("bl-1", "default"),
            "snapshot": lambda s: s.get_source_set_snapshot("snap-1", "default"),
        }
        for name, query in checks.items():
            sqlite_result = query(sqlite_store)
            pg_result = query(pg_store)
            if isinstance(sqlite_result, list):
                self.assertEqual(
                    [_serializable(item) for item in sqlite_result],
                    [_serializable(item) for item in pg_result],
                    f"parity mismatch for {name}",
                )
            else:
                self.assertEqual(
                    _serializable(sqlite_result), _serializable(pg_result), name
                )


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


if __name__ == "__main__":
    unittest.main()
