import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.pipelines.document_store import PipelineDocumentRecord, PipelineDocumentStore
from src.pipelines.ingestion import IngestionOrchestrator, RAGFlowDocumentHandler
from src.pipelines.registry import PIPELINE_REGISTRY, PROCESSOR_KIND_RAGFLOW


def _record(record_id=1, *, status="parsed", shared_read_only=True):
    return PipelineDocumentRecord(
        id=record_id,
        kb_name="kb",
        document_name="shared.pdf",
        original_file_name="shared.pdf",
        dataset_kind="design",
        dataset_id="observability-dataset",
        document_id="observability-document",
        source_group="docs",
        department_id="dept_a",
        uploaded_by="observability",
        status=status,
        processor_kind=PROCESSOR_KIND_RAGFLOW,
        shared_read_only=shared_read_only,
    )


class SharedReadOnlyDocumentTests(unittest.TestCase):
    def test_store_round_trips_shared_read_only_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pipeline_documents.db")
            store = PipelineDocumentStore(db_path=db_path)
            store.upsert_document(
                kb_name="kb",
                document_name="shared.pdf",
                dataset_kind="design",
                dataset_id="observability-dataset",
                document_id="observability-document",
                source_group="docs",
                department_id="dept_a",
                uploaded_by="observability",
                status="parsed",
                processor_kind=PROCESSOR_KIND_RAGFLOW,
                shared_read_only=True,
            )

            record = store.get_document("kb", "shared.pdf", "design", department_id="dept_a")
            self.assertTrue(record.shared_read_only)

            with sqlite3.connect(db_path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_documents)")}
            self.assertIn("shared_read_only", columns)

            with self.assertRaises(PermissionError):
                store.upsert_document(
                    kb_name="kb",
                    document_name="shared.pdf",
                    dataset_kind="design",
                    dataset_id="develop-dataset",
                    document_id="new-document",
                    source_group="docs",
                    department_id="dept_a",
                    uploaded_by="develop",
                    status="parsing",
                    processor_kind=PROCESSOR_KIND_RAGFLOW,
                )

            self.assertEqual(
                store.get_document("kb", "shared.pdf", "design", department_id="dept_a").document_id,
                "observability-document",
            )

    def test_shared_record_is_reusable_without_a_local_archive(self):
        orchestrator = IngestionOrchestrator(
            backend_name="ragflow",
            store=SimpleNamespace(),
            archive=SimpleNamespace(record_archive_exists=lambda record: False),
            handlers={},
            audit_callback=lambda *args, **kwargs: None,
            content_hash_callback=lambda path: "hash",
            remote_document_exists_callback=lambda record: True,
        )
        handler = SimpleNamespace(can_reuse_existing=lambda record: True)

        self.assertTrue(orchestrator._existing_record_is_reusable(_record(), handler))

    def test_handler_refuses_to_delete_shared_record(self):
        remote_deletes = []
        archive_removals = []
        local_deletes = []
        handler = RAGFlowDocumentHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_RAGFLOW),
            store=SimpleNamespace(delete_document_by_id=lambda record_id: local_deletes.append(record_id)),
            submit_callback=lambda *args, **kwargs: None,
            cleanup_remote_callback=lambda *args: remote_deletes.append(args),
        )
        archive = SimpleNamespace(remove_record_archive=lambda record: archive_removals.append(record.id))

        result = handler.delete_record(_record(), archive)

        self.assertFalse(result.ok)
        self.assertIn("只读", result.message)
        self.assertEqual(remote_deletes, [])
        self.assertEqual(archive_removals, [])
        self.assertEqual(local_deletes, [])

    def test_backend_refuses_direct_delete_of_shared_record(self):
        record = _record()
        backend = object.__new__(RAGFlowBackend)
        backend.name = "ragflow"
        backend.store = SimpleNamespace(
            get_document_by_id_scoped=lambda record_id, department_id: record,
        )
        backend._check_kb_access = lambda *args, **kwargs: None
        backend._audit = lambda *args, **kwargs: None

        result = backend.delete_document(
            "kb",
            "ragflow:1",
            ctx=RequestContext(metadata={"department_id": "dept_a"}),
        )

        self.assertFalse(result.ok)
        self.assertIn("只读", result.message)

    def test_backend_refuses_deleting_kb_containing_shared_records(self):
        record = _record()
        deleted = []
        backend = object.__new__(RAGFlowBackend)
        backend.name = "ragflow"
        backend.store = SimpleNamespace(
            list_documents=lambda kb_name, department_id: [record],
            delete_documents_by_kb=lambda *args, **kwargs: deleted.append((args, kwargs)),
        )
        backend._check_kb_access = lambda *args, **kwargs: None

        result = backend.delete_knowledge_base(
            "kb",
            ctx=RequestContext(
                user_id="admin",
                roles=["admin"],
                metadata={"department_id": "dept_a"},
            ),
        )

        self.assertFalse(result.ok)
        self.assertIn("只读", result.message)
        self.assertEqual(deleted, [])

    def test_timeout_guard_never_deletes_or_fails_shared_record(self):
        record = _record(status="parsing")
        remote_deletes = []
        status_updates = []
        backend = object.__new__(RAGFlowBackend)
        backend._cleanup_remote_document = lambda *args: remote_deletes.append(args)
        backend.store = SimpleNamespace(
            update_document_status_by_id=lambda *args: status_updates.append(args),
        )

        message = backend._mark_ragflow_parse_timed_out(record)

        self.assertIn("只读", message)
        self.assertEqual(remote_deletes, [])
        self.assertEqual(status_updates, [])

    def test_retrieve_includes_the_physical_dataset_owned_by_observability(self):
        record = _record()
        client = SimpleNamespace(retrieve_calls=[])

        def retrieve(question, dataset_ids, top_k, metadata_condition=None):
            client.retrieve_calls.append((question, dataset_ids, top_k, metadata_condition))
            return []

        client.retrieve = retrieve
        backend = object.__new__(RAGFlowBackend)
        backend.name = "ragflow"
        backend.client = client
        backend.store = SimpleNamespace(
            list_documents=lambda kb_name, department_id: [record],
        )
        backend._dataset_ids = {
            "governance": "develop-dataset",
            "design": "develop-dataset",
        }
        backend._ensure_physical_datasets = lambda: None
        backend._check_kb_access = lambda *args, **kwargs: None

        backend.retrieve(
            "kb",
            "shared query",
            ctx=RequestContext(metadata={"department_id": "dept_a"}),
        )

        self.assertTrue(client.retrieve_calls)
        self.assertTrue(any("observability-dataset" in call[1] for call in client.retrieve_calls))
        self.assertTrue(all(len(call[1]) == 1 for call in client.retrieve_calls))


if __name__ == "__main__":
    unittest.main()
