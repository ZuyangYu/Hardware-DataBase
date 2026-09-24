"""PostgreSQL Memory Worker end-to-end chain (v3 PR-3 / §5.1 / §6.4).

The durable memory ledger (catalog / jobs / outbox) is part of the control-plane
atomic group.  This suite runs the real worker loop against a real PostgreSQL
control plane (loaded from a SQLite snapshot) with the LangGraph projection
store kept local, and asserts the reflection job, catalog revision and
checkpoint all commit in PG.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

import src.settings as app_settings
from src.core.auth import ROLE_EMPLOYEE, AuthService
from src.core.conversation import ConversationService
from src.memory.manager import ExtractedMemory, MemoryExtractionOutput
from src.memory.service import MemoryService
from src.memory.store import MemoryStoreRuntime
from src.memory.worker import MemoryWorker
from src.storage.control_plane import apply_schema, load_sqlite_snapshot
from src.storage.control_plane.conversation_service import PostgresConversationService
from src.storage.control_plane.memory_worker import (
    PostgresMemoryCatalogRepository,
    PostgresMemoryJobRepository,
)

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    return make_conninfo(**params)


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, messages, *, scope, user=False):
        self.calls += 1
        return MemoryExtractionOutput(
            (
                ExtractedMemory(
                    {
                        "memory_type": "decision",
                        "title": "电源器件决策",
                        "content": "本项目建议采用 LM76003。",
                        "subject": "power",
                        "confidence": 0.9,
                        "tags": ["power"],
                    },
                    "memory-key",
                    "",
                ),
            )
        )


def _worker_settings() -> SimpleNamespace:
    return SimpleNamespace(
        MEMORY_ENABLED=True,
        MEMORY_JOB_LEASE_SECONDS=60,
        MEMORY_JOB_MAX_RETRIES=3,
        MEMORY_REFLECTION_TIMEOUT_SECONDS=5,
        MEMORY_RECONCILE_INTERVAL_SECONDS=3600,
        MEMORY_RETENTION_DAYS="",
        MEMORY_MODEL="fake",
        AGENT_OLLAMA_MODEL="fake",
        MEMORY_SQLITE_BUSY_TIMEOUT_MS=30000,
        MEMORY_STORE_MAX_SCAN=100,
        MEMORY_STORE_OVERSAMPLE_FACTOR=4,
    )


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class MemoryWorkerPostgresChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_mem_e2e_{uuid.uuid4().hex[:8]}"
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
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self.memory_path = os.path.join(self.tmp.name, "memory.db")
        for name, value in (
            ("AUTH_DEFAULT_ADMIN_PASSWORD", "test-admin-password-123"),
            ("AUTH_DEFAULT_ADMIN_USERNAME", "admin"),
            ("CONTROL_PLANE_BACKEND", app_settings.CONTROL_PLANE_BACKEND),
            ("CONTROL_PLANE_DATABASE_URL", app_settings.CONTROL_PLANE_DATABASE_URL),
            ("MEMORY_DEBOUNCE_SECONDS", app_settings.MEMORY_DEBOUNCE_SECONDS),
            ("AUTH_DB_PATH", app_settings.AUTH_DB_PATH),
        ):
            self.addCleanup(setattr, app_settings, name, value)
        app_settings.MEMORY_DEBOUNCE_SECONDS = 0
        app_settings.AUTH_DB_PATH = self.db_path
        app_settings.CONTROL_PLANE_BACKEND = "sqlite"

        self.auth = AuthService(db_path=self.db_path)
        admin = self.auth.get_user_by_username("admin")
        self.department = self.auth.create_department("hardware")
        owner = self.auth.create_user_as(
            admin, "dept-owner", "password-123", ROLE_EMPLOYEE, self.department.id
        )
        self.employee = self.auth.create_user_as(
            admin, "engineer", "password-123", ROLE_EMPLOYEE, self.department.id
        )
        self.auth.register_knowledge_base("design", owner=owner)
        # 记忆治理以 engineer 身份访问该库：按产品模型需要 owner 把库共享给部门，
        # 否则 KB 读权限检查（正确地）会拒绝。
        self.auth.set_knowledge_base_sharing(
            "design", actor=owner, is_shared=True, department_id=self.department.id
        )
        self.kb_id = self.auth.get_knowledge_base_id(
            "design", department_id=self.department.id
        )
        self.assertIsNotNone(self.kb_id)

        service = ConversationService(self.db_path)
        self.session = service.create_session(
            self.employee.id,
            "design",
            department_id=self.department.id,
            kb_id=self.kb_id,
        )
        turn = service.create_turn(
            self.employee.id, self.session.id, "电源方案采用哪种器件？"
        )
        service.complete_turn(
            self.employee.id, turn.id, "建议采用 LM76003。", {"status": "ok"}
        )
        # A real cutover snapshot carries every control-plane table; make sure
        # the export ledger exists in the source so the loader provisions the
        # PostgreSQL twins used by the terminal domain writer.
        from src.result_exports.store import ResultExportStore

        ResultExportStore(
            self.db_path, storage_dir=os.path.join(self.tmp.name, "exports")
        )

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn)
            report = load_sqlite_snapshot(self.db_path, conn, batch_size=50)
            self.assertTrue(report.ok, report.failures)

        app_settings.CONTROL_PLANE_BACKEND = "postgres"
        app_settings.CONTROL_PLANE_DATABASE_URL = self.dsn

    def _pending_job_rows(self) -> list[dict]:
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            rows = conn.execute(
                "SELECT job_id, status FROM memory_jobs WHERE session_id = %s",
                (self.session.id,),
            ).fetchall()
        return [{"job_id": row[0], "status": row[1]} for row in rows]

    def _run_worker(self):
        runtime = MemoryStoreRuntime(path=self.memory_path)
        self.addCleanup(runtime.close)
        adapter = _FakeAdapter()
        worker = MemoryWorker(
            db_path=self.db_path,
            worker_id="pg-memory-worker",
            jobs=PostgresMemoryJobRepository(self.dsn),
            catalog=PostgresMemoryCatalogRepository(self.dsn),
            runtime=runtime,
            adapter=adapter,
            settings_module=_worker_settings(),
        )
        self.addCleanup(worker.close)
        pass_ = 0
        while worker.run_once() and pass_ < 20:
            pass_ += 1
        return runtime, adapter

    def test_project_reflection_runs_end_to_end_on_postgres(self):
        # The completed turn enqueued the durable outbox row in SQLite; the
        # snapshot above moved it to PG, so the worker must claim it from PG.
        seeded = self._pending_job_rows()
        self.assertEqual(len(seeded), 1)
        self.assertEqual(seeded[0]["status"], "pending")

        runtime, adapter = self._run_worker()

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            job = conn.execute(
                "SELECT status, lease_owner, lease_token FROM memory_jobs"
                " WHERE session_id = %s",
                (self.session.id,),
            ).fetchone()
            self.assertEqual(job[0], "completed")
            self.assertIsNone(job[1])
            self.assertIsNone(job[2])
            checkpoint = conn.execute(
                "SELECT committed_generation, committed_message_id"
                " FROM memory_checkpoints"
            ).fetchone()
            self.assertIsNotNone(checkpoint)
            self.assertEqual(int(checkpoint[0]), 1)
            run_status = conn.execute(
                "SELECT status FROM memory_reflection_runs"
            ).fetchone()
            self.assertEqual(run_status[0], "projected")
            sources = conn.execute(
                "SELECT COUNT(*) FROM memory_sources WHERE source_valid = TRUE"
            ).fetchone()
            self.assertGreaterEqual(int(sources[0]), 2)

        records = PostgresMemoryCatalogRepository(self.dsn).list_records(
            scope="project",
            department_id=self.department.id,
            kb_id=self.kb_id,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "candidate")
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(
            runtime.search(
                (
                    "hdb",
                    "department",
                    str(self.department.id),
                    "kb",
                    str(self.kb_id),
                    "candidate",
                ),
                query=None,
                limit=5,
            )
        )

    def test_source_invalidation_deletes_projection_through_postgres_outbox(self):
        runtime, _adapter = self._run_worker()
        namespace = (
            "hdb",
            "department",
            str(self.department.id),
            "kb",
            str(self.kb_id),
            "candidate",
        )
        self.assertTrue(runtime.search(namespace, query=None, limit=5))

        # Editing/redacting the source message invalidates the projection in PG
        # and queues the durable deletion outbox row.
        from src.storage.control_plane.memory_invalidation import (
            invalidate_message_memory,
        )

        with psycopg.connect(
            self.dsn,
            autocommit=False,
            row_factory=dict_row,
            options="-c TimeZone=UTC",
        ) as conn:
            rows = conn.execute(
                "SELECT DISTINCT message_id FROM memory_sources"
                " WHERE session_id = %s AND source_valid",
                (self.session.id,),
            ).fetchall()
            self.assertTrue(rows)
            affected = sum(
                invalidate_message_memory(
                    conn, self.session.id, int(item["message_id"]), reason="e2e_edit"
                )
                for item in rows
            )
            conn.commit()
        self.assertGreaterEqual(affected, 1)
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            queued = conn.execute(
                "SELECT status FROM memory_deletion_outbox"
            ).fetchall()
            self.assertTrue(queued)
            self.assertTrue(all(item[0] in {"pending", "retrying"} for item in queued))

        # The same worker must drain the deletion outbox against PG and remove
        # the physical projection from the local Store.
        worker = MemoryWorker(
            db_path=self.db_path,
            worker_id="pg-memory-deleter",
            jobs=PostgresMemoryJobRepository(self.dsn),
            catalog=PostgresMemoryCatalogRepository(self.dsn),
            runtime=runtime,
            adapter=_adapter,
            settings_module=_worker_settings(),
        )
        self.addCleanup(worker.close)
        pass_ = 0
        while worker.run_once() and pass_ < 20:
            pass_ += 1

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            statuses = [
                item[0]
                for item in conn.execute(
                    "SELECT status FROM memory_deletion_outbox"
                ).fetchall()
            ]
            self.assertTrue(statuses)
            self.assertTrue(all(status == "completed" for status in statuses))
            record_status = conn.execute(
                "SELECT status FROM memory_records"
            ).fetchone()[0]
            self.assertEqual(record_status, "deleted")
        self.assertFalse(runtime.search(namespace, query=None, limit=5))

    def test_retention_sweep_uses_postgres_path(self):
        runtime, _adapter = self._run_worker()
        namespace = (
            "hdb",
            "department",
            str(self.department.id),
            "kb",
            str(self.kb_id),
            "candidate",
        )
        self.assertTrue(runtime.search(namespace, query=None, limit=5))

        # Project memories only enter retention after leaving the active
        # lifecycle; backdate a rejected record and run the sweep on PG.
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            conn.execute(
                "UPDATE memory_records SET status = 'rejected',"
                " updated_at = now() - interval '10 days'"
            )
        from src.memory.retention import expire_memory_records

        expired = expire_memory_records(self.db_path, retention_days=1)
        self.assertEqual(expired, 1)

        worker = MemoryWorker(
            db_path=self.db_path,
            worker_id="pg-memory-retention",
            jobs=PostgresMemoryJobRepository(self.dsn),
            catalog=PostgresMemoryCatalogRepository(self.dsn),
            runtime=runtime,
            adapter=_adapter,
            settings_module=_worker_settings(),
        )
        self.addCleanup(worker.close)
        pass_ = 0
        while worker.run_once() and pass_ < 20:
            pass_ += 1

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            record = conn.execute(
                "SELECT status FROM memory_records"
            ).fetchone()
            self.assertEqual(record[0], "deleted")
            pending = conn.execute(
                "SELECT COUNT(*) FROM memory_deletion_outbox"
                " WHERE status IN ('pending', 'retrying', 'running')"
            ).fetchone()[0]
            self.assertEqual(int(pending), 0)
            audit = conn.execute(
                "SELECT COUNT(*) FROM memory_audit_events"
                " WHERE event_type = 'retention_expired'"
            ).fetchone()[0]
            self.assertEqual(int(audit), 1)
        self.assertFalse(runtime.search(namespace, query=None, limit=5))

    def _complete_export_turn(self):
        service = PostgresConversationService(self.dsn, pool_size=2)
        self.addCleanup(service.close)
        turn = service.create_turn(
            self.employee.id,
            self.session.id,
            "请把检索结果输出成 Excel 和 PDF",
        )
        self.assertTrue(turn.export_plan.get("formats"))
        claimed = service.claim_turn_result(
            self.employee.id, turn.id, "pg-export-worker"
        )
        completion = service.complete_turn_result(
            self.employee.id,
            turn.id,
            "导出答案",
            {"status": "ok"},
            worker_id="pg-export-worker",
            fencing_token=claimed.turn.fencing_token,
            expected_generation=claimed.turn.session_generation,
            terminal_events_builder=lambda item: [
                ("done", {"answer": item.answer}),
                ("turn_end", {"status": "completed"}),
            ],
        )
        self.assertEqual(completion.turn.status, "completed")
        return turn

    def test_complete_turn_writes_exports_and_memory_outbox_atomically(self):
        turn = self._complete_export_turn()

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            snapshots = conn.execute(
                "SELECT snapshot_id, turn_id FROM result_snapshots"
            ).fetchall()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(str(snapshots[0][1]), turn.id)
            job_formats = [
                item[0]
                for item in conn.execute(
                    "SELECT format FROM result_export_jobs ORDER BY format"
                ).fetchall()
            ]
            self.assertEqual(job_formats, ["pdf", "xlsx"])
            outbox = conn.execute(
                "SELECT target_turn_id, status, generation FROM memory_jobs"
                " WHERE session_id = %s",
                (self.session.id,),
            ).fetchall()
            # Project reflection debounces per session: the same durable row
            # advances its watermark to the newly completed turn.
            self.assertEqual(len(outbox), 1)
            self.assertEqual(str(outbox[0][0]), turn.id)
            self.assertEqual(outbox[0][1], "pending")
            self.assertGreaterEqual(int(outbox[0][2]), 2)

    def test_export_worker_claims_and_publishes_from_postgres(self):
        self._complete_export_turn()
        from src.result_exports.store import ResultExportStore
        from src.result_exports.worker import ResultExportWorker

        export_dir = os.path.join(self.tmp.name, "pg-exports")
        store = ResultExportStore(storage_dir=export_dir)
        worker = ResultExportWorker(store=store, worker_id="pg-export-runner")
        self.addCleanup(worker.stop)
        pass_ = 0
        while worker.run_once() and pass_ < 10:
            pass_ += 1

        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            statuses = [
                item[0]
                for item in conn.execute(
                    "SELECT status FROM result_export_jobs ORDER BY format"
                ).fetchall()
            ]
            self.assertEqual(statuses, ["succeeded", "succeeded"])
            artifacts = conn.execute(
                "SELECT storage_ref, sha256 FROM result_artifacts"
                " ORDER BY created_at"
            ).fetchall()
            self.assertEqual(len(artifacts), 2)
            for storage_ref, digest in artifacts:
                self.assertTrue(storage_ref)
                self.assertRegex(str(digest), r"^[0-9a-f]{64}$")
                self.assertTrue(os.path.exists(os.path.join(export_dir, storage_ref)))

    def test_memory_service_governance_runs_on_postgres(self):
        runtime, _adapter = self._run_worker()
        service = MemoryService(
            db_path=self.db_path,
            jobs=PostgresMemoryJobRepository(self.dsn),
            catalog=PostgresMemoryCatalogRepository(self.dsn),
            store_runtime=runtime,
            auth=self.auth,
        )
        self.addCleanup(service.close)

        records = service.list_memories(
            actor=self.employee, scope="project", kb_name="design"
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        draft = service.update_draft(
            actor=self.employee,
            memory_id=record["memory_id"],
            expected_revision=record["revision"],
            content={
                "memory_type": "decision",
                "title": "电源器件决策（修订）",
                "content": "本项目最终采用 LM76003。",
                "subject": "power",
                "confidence": 0.95,
                "tags": ["power", "reviewed"],
            },
            reason="审核人员修订候选",
            request_id="pg-draft-1",
        )
        self.assertEqual(draft["revision"], 2)
        latest = service.get_memory(record["memory_id"], actor=self.employee)
        verified = service.verify(
            record["memory_id"],
            actor=self.employee,
            expected_revision=latest["revision"],
            evidence_refs=["datasheet:LM76003"],
            reason="已核对 Datasheet",
            request_id="pg-verify-1",
            kb_name="design",
        )
        self.assertEqual(verified["status"], "verification_pending")

        # Drain the verified projection outbox on PG and confirm the durable
        # ledger records the revision without leaving pending work behind.
        worker = MemoryWorker(
            db_path=self.db_path,
            worker_id="pg-memory-service-outbox",
            jobs=PostgresMemoryJobRepository(self.dsn),
            catalog=PostgresMemoryCatalogRepository(self.dsn),
            runtime=runtime,
            adapter=_adapter,
            settings_module=_worker_settings(),
        )
        self.addCleanup(worker.close)
        pass_ = 0
        while worker.run_once() and pass_ < 20:
            pass_ += 1
        with psycopg.connect(
            self.dsn, autocommit=True, options="-c TimeZone=UTC"
        ) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM memory_projection_outbox"
                " WHERE status IN ('pending', 'retrying', 'running')"
            ).fetchone()[0]
            self.assertEqual(int(pending), 0)
            projection_status = conn.execute(
                "SELECT projection_kind FROM memory_projections"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(projection_status, "verified")


if __name__ == "__main__":
    unittest.main()
