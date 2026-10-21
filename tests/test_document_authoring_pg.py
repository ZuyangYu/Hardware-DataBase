"""PG parity for the document-authoring domain (storage-convergence phase 2).

The seven stores that share ``document_authoring.db`` (work orders/templates,
planning, plan submissions, generation sessions, reviews, revisions, tasks)
run on PostgreSQL through ``src/storage/domain_pg.py`` + the group wiring in
``src/document_authoring/pg_support.py``.

Each test runs the same operations against SQLite and PostgreSQL and compares
the results.  ``HDB_TEST_POSTGRES_DSN`` gates the PG side; a throwaway
database per run keeps real data untouched.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings as app_settings
from src.document_authoring.models import (
    RendererPolicy,
    TemplateSecurityReport,
    TemplateVersion,
)
from src.document_authoring.work_order_store import DocumentAuthoringStore

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")
TEMPLATE_HASH = hashlib.sha256(b"template-bytes").hexdigest()


def _dsn_with_database(dsn: str, name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = name
    return make_conninfo(**values)


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class DocumentAuthoringPgParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_authoring_{uuid.uuid4().hex[:8]}"
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
        for name, value in (
            ("CONTROL_PLANE_BACKEND", app_settings.CONTROL_PLANE_BACKEND),
            ("CONTROL_PLANE_DATABASE_URL", app_settings.CONTROL_PLANE_DATABASE_URL),
            ("STORAGE_DIR", app_settings.STORAGE_DIR),
            ("AUTHORING_STORE_BACKEND", app_settings.AUTHORING_STORE_BACKEND),
            ("AUTHORING_STORE_DATABASE_URL", app_settings.AUTHORING_STORE_DATABASE_URL),
        ):
            self.addCleanup(setattr, app_settings, name, value)
        app_settings.CONTROL_PLANE_BACKEND = "postgres"
        app_settings.CONTROL_PLANE_DATABASE_URL = self.dsn
        app_settings.STORAGE_DIR = self.tmp.name
        # conftest 会把这些键强制成 sqlite 做隔离；本套件要验证"跟随控制面"。
        app_settings.AUTHORING_STORE_BACKEND = ""
        app_settings.AUTHORING_STORE_DATABASE_URL = ""

    def _sqlite_store(self) -> DocumentAuthoringStore:
        return DocumentAuthoringStore(
            db_path=os.path.join(self.tmp.name, "authoring-sqlite.db"),
            artifact_root=os.path.join(self.tmp.name, "artifacts-sqlite"),
        )

    def _pg_store(self) -> DocumentAuthoringStore:
        return DocumentAuthoringStore(
            artifact_root=os.path.join(self.tmp.name, "artifacts-pg")
        )

    def test_conflict_decisions_round_trip_and_replay_on_both_backends(self):
        from src.document_authoring.conflicts.detector import ConflictDetector
        from src.document_authoring.conflicts.normalizer import normalize_claim
        from src.document_authoring.conflicts.policy import ConflictPolicy
        from src.document_authoring.conflicts.store import ConflictDecisionStore

        for backend in (self._sqlite_store(), self._pg_store()):
            with self.subTest(backend=backend._backend):
                store = ConflictDecisionStore(backend_store=backend)
                case = ConflictDetector().detect([
                    normalize_claim(
                        claim_id=f'claim-{value}', field_key='input_voltage', subject='power', predicate='requires',
                        value=value, unit='V', source_kb_id=101,
                        source_role='primary', evidence_id=f'evidence-{value}',
                    ) for value in ('5', '12')
                ])[0]
                work_order_id = f'pg-conflict-{uuid.uuid4().hex}'
                store.save_case(case, tenant_id='default', work_order_id=work_order_id)
                request = dict(
                    conflict_id=case.conflict_id, tenant_id='default', actor_id='alice',
                    expected_fingerprint=case.fingerprint, expected_version=1,
                    expected_snapshot_hash=case.snapshot_hash,
                    expected_evidence_set_hash=case.evidence_set_hash,
                    decision=ConflictPolicy().evaluate(case, purpose='compose').model_copy(
                        update={'selected_claim_id': 'claim-5'},
                    ), idempotency_key='pg-decision',
                )
                first = store.submit_decision(**request)
                self.assertEqual(first, store.submit_decision(**request))
                self.assertEqual(store.get_case(case.conflict_id).row_version, 2)
                self.assertEqual(len(store.list_decisions(case.conflict_id, tenant_id='default')), 1)

    def test_concurrent_conflict_decision_replays_share_one_result_on_both_backends(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from src.document_authoring.conflicts.detector import ConflictDetector
        from src.document_authoring.conflicts.normalizer import normalize_claim
        from src.document_authoring.conflicts.policy import ConflictPolicy
        from src.document_authoring.conflicts.store import ConflictDecisionStore

        for backend in (self._sqlite_store(), self._pg_store()):
            with self.subTest(backend=backend._backend):
                store = ConflictDecisionStore(backend_store=backend)
                case = ConflictDetector().detect([
                    normalize_claim(
                        claim_id=f'claim-{value}', field_key='input_voltage', subject='power', predicate='requires',
                        value=value, unit='V', source_kb_id=101,
                        source_role='primary', evidence_id=f'evidence-{value}',
                    ) for value in ('5', '12')
                ])[0]
                store.save_case(case, tenant_id='default', work_order_id=f'pg-conflict-{uuid.uuid4().hex}')
                barrier = Barrier(4)

                def submit(_index):
                    barrier.wait(timeout=10)
                    return store.submit_decision(
                        case.conflict_id, tenant_id='default', actor_id='alice',
                        expected_fingerprint=case.fingerprint, expected_version=1,
                        expected_snapshot_hash=case.snapshot_hash,
                        expected_evidence_set_hash=case.evidence_set_hash,
                        decision=ConflictPolicy().evaluate(case, purpose='compose').model_copy(
                            update={'selected_claim_id': 'claim-5'},
                        ), idempotency_key='pg-concurrent-decision',
                    )

                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(submit, range(4)))
                self.assertTrue(all(result == results[0] for result in results))
                self.assertEqual(len(store.list_decisions(case.conflict_id, tenant_id='default')), 1)

    def test_submission_worker_contract_rejects_old_worker_on_both_backends(self):
        from pytest import MonkeyPatch
        from tests.test_review_submission_contract import check_submission_review_contract

        for backend in (self._sqlite_store(), self._pg_store()):
            with self.subTest(backend=backend._backend), MonkeyPatch.context() as patch:
                check_submission_review_contract(backend, patch)

    @staticmethod
    def _seed_template(store: DocumentAuthoringStore, suffix: str):
        store.save_renderer_policy(RendererPolicy(renderer_policy_id=f"renderer-{suffix}"))
        return store.save_template(
            TemplateVersion(
                template_version_id=f"tv-{suffix}",
                template_id=f"t-{suffix}",
                format="xlsx",
                content_hash=TEMPLATE_HASH,
                template_schema_id="ts-1",
                template_schema_version="1",
                renderer_policy_id=f"renderer-{suffix}",
                tenant_id="default",
                knowledge_base_name="hardware",
                status="approved",
            ),
            b"template-bytes",
            TemplateSecurityReport(
                report_id=f"rep-{suffix}", content_hash=TEMPLATE_HASH, format="xlsx"
            ),
        )

    def test_template_round_trip_parity(self) -> None:
        sqlite_store = self._sqlite_store()
        pg_store = self._pg_store()
        sqlite_suffix = f"s{uuid.uuid4().hex[:6]}"
        pg_suffix = f"p{uuid.uuid4().hex[:6]}"

        sqlite_template = self._seed_template(sqlite_store, sqlite_suffix)
        pg_template = self._seed_template(pg_store, pg_suffix)

        self.assertEqual(
            sqlite_template.template_version_id, f"tv-{sqlite_suffix}"
        )
        self.assertEqual(pg_template.template_version_id, f"tv-{pg_suffix}")
        # json_extract 改写：PG 侧按模板 ID 排序/过滤必须一致
        sqlite_ids = [t.template_version_id for t in sqlite_store.list_templates()]
        pg_ids = [t.template_version_id for t in pg_store.list_templates()]
        self.assertIn(f"tv-{sqlite_suffix}", sqlite_ids)
        self.assertIn(f"tv-{pg_suffix}", pg_ids)
        self.assertEqual(len(sqlite_ids), len(set(sqlite_ids)))
        self.assertEqual(len(pg_ids), len(set(pg_ids)))

        self.assertEqual(
            sqlite_store.get_template(f"tv-{sqlite_suffix}").content_hash,
            pg_store.get_template(f"tv-{pg_suffix}").content_hash,
        )
        self.assertEqual(
            sqlite_store.read_template_content(f"tv-{sqlite_suffix}"),
            pg_store.read_template_content(f"tv-{pg_suffix}"),
        )

    def test_authoring_history_uses_durable_creation_order_on_both_backends(self):
        """Restore order survives out-of-order import and has a stable ID tie break."""
        from src.document_authoring.models import (
            DocumentArtifact, DocumentHumanEvent, HarnessRun, NodeExecutionReceipt,
        )
        from tests.test_knowledge_base_document_work_orders import make_work_order

        for store in (self._sqlite_store(), self._pg_store()):
            suffix = uuid.uuid4().hex
            template = self._seed_template(store, suffix)
            oldest = datetime(2026, 1, 1, tzinfo=timezone.utc)
            orders = {}
            # Newer rows arrive before an older imported row; b/c share a time.
            for key, offset in (("c", 1), ("a", 0), ("b", 1)):
                order = make_work_order(
                    work_order_id=f"{suffix}-{key}", tenant_id=suffix,
                    project_id=suffix, template_version_id=template.template_version_id,
                    created_at=oldest + timedelta(days=offset),
                )
                orders[key] = store.create_work_order(order)
                store.create_work_order(type(order).model_validate({
                    **order.model_dump(),
                    "work_order_id": f"{suffix}-kb-{key}", "scope_type": "knowledge_base",
                    "project_id": None, "baseline_id": None, "knowledge_base_name": suffix,
                    "baseline_content_hash": "", "input_fingerprint": "",
                }))
            run = store.create_harness_run(HarnessRun(
                harness_run_id=f"run-{suffix}", work_order_id=orders["a"].work_order_id,
                run_manifest_id="manifest", status="queued",
            ))
            run = store.claim_harness_run(run.harness_run_id, "worker", 300)
            for key, offset in (("c", 1), ("a", 0), ("b", 1)):
                created_at = oldest + timedelta(days=offset)
                receipt = store.begin_node_execution_owned(NodeExecutionReceipt(
                    receipt_id=f"{suffix}-receipt-{key}", harness_run_id=run.harness_run_id,
                    node_name="plan_node", unit_id=key, input_fingerprint=key,
                    fencing_token=run.fencing_token, created_at=created_at,
                ), "worker", run.fencing_token)
                store.commit_node_execution_owned(
                    receipt.receipt_id, run.harness_run_id, "worker", run.fencing_token,
                    {"unit_id": key},
                )
                artifact = store.save_artifact(DocumentArtifact(
                    artifact_id=f"{suffix}-artifact-{key}", work_order_id=orders["a"].work_order_id,
                    run_id=run.harness_run_id, stage="review_candidate",
                    content_hash=hashlib.sha256(key.encode()).hexdigest(),
                    validation_report_id="report", integrity_manifest_id="integrity",
                    created_at=created_at,
                ), key.encode(), "docx")
                store.save_human_event(DocumentHumanEvent(
                    event_id=f"{suffix}-event-{key}", work_order_id=orders["a"].work_order_id,
                    run_id=run.harness_run_id, artifact_id=f"{suffix}-artifact-c",
                    unit_id=key, event_type="feedback", subject_artifact_content_hash=artifact.content_hash,
                    actor_id="alice", actor_role="reviewer", created_at=created_at,
                ))
            queries = (
                ("project", lambda: [row.work_order_id for row in store.list_work_orders(suffix, suffix)],
                 [orders[key].work_order_id for key in ("c", "b", "a")]),
                ("knowledge_base", lambda: [row.work_order_id for row in store.list_work_orders_for_knowledge_base(suffix, suffix)],
                 [f"{suffix}-kb-{key}" for key in ("c", "b", "a")]),
                ("receipts", lambda: [row.receipt_id for row in store.list_node_execution_receipts(run.harness_run_id)],
                 [f"{suffix}-receipt-{key}" for key in ("a", "b", "c")]),
                ("artifacts", lambda: [row.artifact_id for row in store.list_artifacts(orders["a"].work_order_id)],
                 [f"{suffix}-artifact-{key}" for key in ("a", "b", "c")]),
                ("human_events", lambda: [row.event_id for row in store.list_human_events(f"{suffix}-artifact-c")],
                 [f"{suffix}-event-{key}" for key in ("a", "b", "c")]),
            )
            for name, query, expected in queries:
                with self.subTest(backend=store._backend, history=name):
                    self.assertEqual(query(), expected)

    def test_work_order_version_lock_rejects_stale_updates_on_both_backends(self):
        from tests.test_knowledge_base_document_work_orders import make_work_order

        for store in (self._sqlite_store(), self._pg_store()):
            with self.subTest(backend=store._backend):
                suffix = uuid.uuid4().hex
                template = self._seed_template(store, suffix)
                original = store.create_work_order(make_work_order(
                    work_order_id=suffix, template_version_id=template.template_version_id,
                ))
                revised = original.model_copy(update={"status": "retrieving", "lock_version": 1})
                self.assertEqual(store.replace_work_order(revised), revised)
                with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
                    store.replace_work_order(original.model_copy(update={"status": "failed", "lock_version": 1}))
                persisted = store.get_work_order(suffix)
                self.assertEqual(persisted.status, "retrieving")
                self.assertEqual(persisted.lock_version, 1)

    def test_all_group_stores_run_on_postgres(self) -> None:
        store = self._pg_store()
        self.assertEqual(store._backend, "postgres")
        self.assertEqual(store.planning._backend, "postgres")
        self.assertEqual(store.planning.submissions._backend, "postgres")
        self.assertEqual(store.generation_sessions._backend, "postgres")
        self.assertIsNone(store.db_path)

    def test_generation_session_round_trip_parity(self) -> None:
        sqlite_store = self._sqlite_store()
        pg_store = self._pg_store()
        sqlite_suffix = f"s{uuid.uuid4().hex[:6]}"
        pg_suffix = f"p{uuid.uuid4().hex[:6]}"
        self._seed_template(sqlite_store, sqlite_suffix)
        self._seed_template(pg_store, pg_suffix)

        sqlite_session = sqlite_store.generation_sessions.create_session(
            tenant_id="default",
            user_id="user-1",
            knowledge_base_name="hardware",
            template_version_id=f"tv-{sqlite_suffix}",
        )
        pg_session = pg_store.generation_sessions.create_session(
            tenant_id="default",
            user_id="user-1",
            knowledge_base_name="hardware",
            template_version_id=f"tv-{pg_suffix}",
        )
        self.assertEqual(sqlite_session.status, pg_session.status)
        self.assertEqual(
            sqlite_store.generation_sessions.get_session(sqlite_session.session_id).user_id,
            pg_store.generation_sessions.get_session(pg_session.session_id).user_id,
        )


if __name__ == "__main__":
    unittest.main()
