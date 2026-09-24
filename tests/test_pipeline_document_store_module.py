import gc
import os
import sqlite3
import tempfile
import unittest

from src.pipelines.document_store import PipelineDocumentRecord, PipelineDocumentStore


class PipelineDocumentStoreModuleTests(unittest.TestCase):
    def test_public_store_is_not_a_ragflow_subclass_wrapper(self):
        self.assertEqual(PipelineDocumentStore.__module__, "src.pipelines.document_store_sqlite")
        self.assertEqual(PipelineDocumentRecord.__module__, "src.pipelines.document_store_sqlite")
        self.assertEqual(PipelineDocumentStore.__name__, "PipelineDocumentStore")

    def test_sqlite_store_uses_pipeline_table_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)

            self.assertTrue(store.db_path.endswith("pipeline_documents.db"))
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                }
            finally:
                conn.close()
                del store
                gc.collect()

            self.assertIn("pipeline_documents", tables)
            self.assertIn("pipeline_datasets", tables)
            self.assertNotIn("ragflow_documents", tables)
            self.assertNotIn("ragflow_datasets", tables)
            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(pipeline_documents)").fetchall()
                }
            finally:
                conn.close()
            self.assertIn("kb_id", columns)

    def test_get_dataset_returns_the_persisted_id_and_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PipelineDocumentStore(db_path=os.path.join(tmp, "pipeline_documents.db"))
            store.save_dataset("design", "dataset-new", "ADAS_new")

            self.assertEqual(store.get_dataset("design"), ("dataset-new", "ADAS_new"))
            self.assertIsNone(store.get_dataset("missing"))

    def test_upsert_document_persists_kb_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="a.xlsx",
                dataset_kind="table",
                dataset_id="dataset",
                document_id="doc-a",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user_a",
                kb_id=42,
                status="archived",
                processor_kind="spreadsheet_table",
            )
            record = store.get_document("kb", "a.xlsx", "table", department_id="dept_a")

            self.assertEqual(record.kb_id, 42)

            store.upsert_document(
                kb_name="kb",
                document_name="a.xlsx",
                dataset_kind="table",
                dataset_id="dataset",
                document_id="doc-a",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user_a",
                kb_id=43,
                status="archived",
                processor_kind="spreadsheet_table",
            )
            record = store.get_document("kb", "a.xlsx", "table", department_id="dept_a")
            self.assertEqual(record.kb_id, 43)

            del store
            gc.collect()

    def test_id_lookup_and_delete_can_be_department_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="a.pdf",
                dataset_kind="design",
                dataset_id="dataset",
                document_id="doc-a",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user_a",
            )
            record = store.get_document("kb", "a.pdf", "design", department_id="dept_a")

            self.assertIsNotNone(store.get_document_by_id_scoped(record.id, "dept_a"))
            self.assertIsNone(store.get_document_by_id_scoped(record.id, "dept_b"))
            store.delete_document_by_id_scoped(record.id, "dept_b")
            self.assertIsNotNone(store.get_document_by_id_scoped(record.id, "dept_a"))
            store.delete_document_by_id_scoped(record.id, "dept_a")
            self.assertIsNone(store.get_document_by_id_scoped(record.id, "dept_a"))

            del store
            gc.collect()

    def test_document_stats_by_kb_filters_department(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="a.xlsx",
                dataset_kind="table",
                dataset_id="dataset",
                document_id="doc-a",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user_a",
                status="failed",
                processor_kind="spreadsheet_table",
            )
            store.upsert_document(
                kb_name="kb",
                document_name="b.xlsx",
                dataset_kind="table",
                dataset_id="dataset",
                document_id="doc-b",
                source_group="docs",
                department_id="dept_b",
                uploaded_by="user_b",
                status="parsing",
                processor_kind="spreadsheet_table",
            )

            self.assertEqual(store.document_stats_by_kb(department_id="dept_a"), {"kb": {"files": 1, "failed": 1, "parsing": 0}})
            self.assertEqual(store.document_stats_by_kb(department_id="dept_b"), {"kb": {"files": 1, "failed": 0, "parsing": 1}})
            with self.assertRaises(ValueError):
                store.document_stats_by_kb()

            del store
            gc.collect()

    def test_claim_next_parse_record_filters_processor_kind_and_recovers_stale_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="rag.pdf",
                dataset_kind="design",
                dataset_id="dataset",
                document_id="doc-rag",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user_a",
                status="archived",
                processor_kind="ragflow",
            )
            store.upsert_document(
                kb_name="kb",
                document_name="sheet.xlsx",
                dataset_kind="table",
                dataset_id="dataset",
                document_id="doc-sheet",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user_a",
                status="archived",
                processor_kind="spreadsheet_table",
            )

            claimed = store.claim_next_parse_record("worker-1", processor_kinds=("ragflow",))
            self.assertEqual(claimed.document_name, "rag.pdf")
            self.assertEqual(claimed.processor_kind, "ragflow")
            self.assertIsNone(store.claim_next_parse_record("worker-2", processor_kinds=("unknown",)))

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    UPDATE pipeline_documents
                    SET worker_id = 'stale-worker',
                        worker_started_at = datetime('now', '-2 hours'),
                        worker_heartbeat_at = datetime('now', '-2 hours'),
                        status = 'processing',
                        upload_status = 'processing'
                    WHERE document_name = 'sheet.xlsx'
                    """
                )
                conn.commit()
            finally:
                conn.close()

            recovered = store.claim_next_parse_record(
                "worker-3",
                processor_kinds=("spreadsheet_table",),
                stale_after_seconds=60,
            )
            self.assertEqual(recovered.document_name, "sheet.xlsx")
            self.assertEqual(recovered.worker_id, "worker-3")
            self.assertEqual(recovered.status, "processing")

            del store
            gc.collect()

    def test_scoped_delete_requires_department_and_preserves_other_departments(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            for department_id in ("dept_a", "dept_b"):
                store.upsert_document(
                    kb_name="kb",
                    document_name=f"{department_id}.xlsx",
                    dataset_kind="table",
                    dataset_id="dataset",
                    document_id=f"doc-{department_id}",
                    source_group="docs",
                    department_id=department_id,
                    uploaded_by="user",
                    status="archived",
                    processor_kind="spreadsheet_table",
                )

            with self.assertRaises(ValueError):
                store.delete_documents_by_kb("kb", department_id="")
            with self.assertRaises(ValueError):
                store.list_documents("kb")
            with self.assertRaises(ValueError):
                store.get_document("kb", "dept_a.xlsx", "table")
            with self.assertRaises(ValueError):
                store.delete_document("kb", "dept_a.xlsx", "table")

            store.delete_documents_by_kb("kb", department_id="dept_a")
            self.assertEqual([record.document_name for record in store.list_documents("kb", department_id="dept_a")], [])
            self.assertEqual([record.document_name for record in store.list_documents("kb", department_id="dept_b")], ["dept_b.xlsx"])

            del store
            gc.collect()

    def test_document_stats_by_kb_identity_keeps_same_name_departments_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="shared",
                document_name="a.xlsx",
                dataset_kind="table",
                dataset_id="",
                document_id="doc-a",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user_a",
                kb_id=101,
                status="failed",
                processor_kind="spreadsheet_table",
            )
            store.upsert_document(
                kb_name="shared",
                document_name="b.xlsx",
                dataset_kind="table",
                dataset_id="",
                document_id="doc-b",
                source_group="docs",
                department_id="dept_b",
                uploaded_by="user_b",
                kb_id=202,
                status="parsing",
                processor_kind="spreadsheet_table",
            )

            self.assertEqual(
                store.document_stats_by_kb_identity(),
                {
                    "kb_id:101": {"files": 1, "failed": 1, "parsing": 0},
                    "kb_id:202": {"files": 1, "failed": 0, "parsing": 1},
                },
            )

            del store
            gc.collect()

    def test_explicit_unscoped_lookup_requires_non_ambiguous_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            for department_id in ("dept_a", "dept_b"):
                store.upsert_document(
                    kb_name="kb",
                    document_name="shared.xlsx",
                    dataset_kind="table",
                    dataset_id="dataset",
                    document_id=f"doc-{department_id}",
                    source_group="docs",
                    department_id=department_id,
                    uploaded_by="user",
                    status="archived",
                    processor_kind="spreadsheet_table",
                )

            self.assertEqual(
                [record.department_id for record in store.list_documents_unscoped("kb")],
                ["dept_a", "dept_b"],
            )
            with self.assertRaises(ValueError):
                store.get_document_unscoped("kb", "shared.xlsx", "table")

            del store
            gc.collect()

    def test_error_message_is_written_alongside_legacy_ragflow_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="a.pdf",
                dataset_kind="design",
                dataset_id="dataset",
                document_id="doc-a",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user",
                status="parsing",
            )
            store.update_document_status("dataset", "doc-a", "failed", "boom")
            record = store.get_document("kb", "a.pdf", "design", department_id="dept_a")

            self.assertEqual(record.error_message, "boom")
            self.assertEqual(record.ragflow_error, "boom")

            del store
            gc.collect()

    def test_update_document_status_marks_indexed_as_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="a.pdf",
                dataset_kind="design",
                dataset_id="dataset",
                document_id="doc-a",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user",
                status="parsing",
            )

            store.update_document_status("dataset", "doc-a", "indexed")
            record = store.get_document("kb", "a.pdf", "design", department_id="dept_a")

            self.assertEqual(record.status, "indexed")
            self.assertTrue(record.parse_completed_at)

            del store
            gc.collect()

    def test_claim_next_parse_record_dead_letters_after_max_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="bad.xlsx",
                dataset_kind="table",
                dataset_id="dataset",
                document_id="doc-bad",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="user",
                status="archived",
                processor_kind="spreadsheet_table",
            )
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("UPDATE pipeline_documents SET retry_count = 3 WHERE document_name = 'bad.xlsx'")
                conn.commit()
            finally:
                conn.close()

            self.assertIsNone(store.claim_next_parse_record("worker", processor_kinds=("spreadsheet_table",), max_retries=3))
            record = store.get_document("kb", "bad.xlsx", "table", department_id="dept_a")
            self.assertEqual(record.status, "dead_letter")
            self.assertEqual(record.error_message, "Exceeded maximum background parse retries")

            del store
            gc.collect()


if __name__ == "__main__":
    unittest.main()
