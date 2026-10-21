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
import threading
import unittest
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings as app_settings
from src.document_authoring.models import (
    AuthoringExecutionEvent,
    DocumentWorkOrder,
    HarnessRun,
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

    # -- concurrency contract (PG only: SQLite serialises via BEGIN IMMEDIATE) --

    def _seed_work_order_and_run(
        self, store: DocumentAuthoringStore, suffix: str
    ) -> HarnessRun:
        order = store.create_work_order(
            DocumentWorkOrder(
                work_order_id=f"wo-{suffix}",
                tenant_id="tenant-a",
                project_id="project-a",
                baseline_id="baseline-a",
                baseline_content_hash="baseline-hash",
                source_set_snapshot_id="snapshot-a",
                template_version_id="template-a",
                document_schema_id="schema-a",
                document_schema_version="1",
                template_schema_id="template-schema-a",
                template_schema_version="1",
                retrieval_policy_version="1",
                renderer_policy_version="1",
                target_format="xlsx",
                execution_mode="internal_harness",
                harness_policy_id="harness-policy",
                harness_policy_version="1",
                created_by="alice",
            )
        )
        return store.create_harness_run(
            HarnessRun(
                harness_run_id=f"run-{suffix}",
                work_order_id=order.work_order_id,
                run_manifest_id=f"manifest-{suffix}",
                status="queued",
                max_retries=1,
            )
        )

    def test_concurrent_claim_grants_a_single_fencing_token(self) -> None:
        """Two workers claiming the same run must not both receive a lease.

        Regression: ``BEGIN IMMEDIATE`` is rewritten to plain ``BEGIN`` on
        PostgreSQL, so without an explicit ``FOR UPDATE`` both workers read
        ``fencing_token=N`` and write ``N+1`` -- a split-brain lease.
        """
        store = self._pg_store()
        run = self._seed_work_order_and_run(store, f"claim-{uuid.uuid4().hex[:6]}")
        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            barrier.wait(timeout=10)
            try:
                claimed = store.claim_harness_run(
                    run.harness_run_id, name, lease_seconds=60
                )
                outcome: tuple[str, object, str] = ("ok", claimed.fencing_token, name)
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome = ("error", f"{type(exc).__name__}: {exc}", name)
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(f"worker-{index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        winners = [outcome for outcome in results if outcome[0] == "ok"]
        self.assertEqual(
            len(winners),
            1,
            f"exactly one worker must win the claim, got {results}",
        )
        self.assertEqual(winners[0][1], 1)
        stored = store.get_harness_run(run.harness_run_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "running")
        self.assertEqual(stored.fencing_token, 1)
        self.assertEqual(stored.lease_owner, winners[0][2])

    def test_stale_owner_write_is_rejected_after_reclaim(self) -> None:
        """Fencing must reject the deposed worker's write, not interleave it."""
        store = self._pg_store()
        run = self._seed_work_order_and_run(store, f"fence-{uuid.uuid4().hex[:6]}")
        first = store.claim_harness_run(run.harness_run_id, "worker-old", lease_seconds=60)
        paused = store.request_harness_run_state(run.harness_run_id, "paused")
        self.assertEqual(paused.fencing_token, first.fencing_token + 1)
        store.queue_harness_retry(run.harness_run_id, max_retries=1)
        reclaimed = store.claim_harness_run(run.harness_run_id, "worker-new", lease_seconds=60)
        self.assertGreater(reclaimed.fencing_token, first.fencing_token)

        with self.assertRaises(Exception):
            store.update_harness_run_owned(
                run.harness_run_id,
                "worker-old",
                first.fencing_token,
                current_node="stale-write",
            )

    def test_concurrent_event_append_allocates_unique_sequences(self) -> None:
        """Sequence allocation is read-modify-write and needs the run row lock."""
        store = self._pg_store()
        run = self._seed_work_order_and_run(store, f"seq-{uuid.uuid4().hex[:6]}")
        barrier = threading.Barrier(2)
        sequences: list[int] = []
        lock = threading.Lock()

        def append(index: int) -> None:
            event = AuthoringExecutionEvent(
                event_id=f"evt-{uuid.uuid4().hex}",
                event_type="tool_called",
                tenant_id="tenant-a",
                work_order_id=run.work_order_id,
                harness_run_id=run.harness_run_id,
                idempotency_key=f"key-{uuid.uuid4().hex}",
                executor="agent_field_harness",
                tool_name="retrieve_evidence",
                field_id="field-0",
                sanitized_payload={"evidence_ids": ["ev-1"], "issue_codes": []},
            )
            barrier.wait(timeout=10)
            stored = store.append_execution_event(event)
            with lock:
                sequences.append(stored.sequence)

        threads = [
            threading.Thread(target=append, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(sequences), 2, sequences)
        self.assertEqual(
            sorted(sequences),
            [1, 2],
            f"concurrent appends must not share a sequence: {sequences}",
        )


if __name__ == "__main__":
    unittest.main()
