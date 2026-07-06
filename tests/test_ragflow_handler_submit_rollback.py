import unittest
from types import SimpleNamespace

from src.pipelines.ingestion import ArchivedFile, IngestionScope, RAGFlowDocumentHandler
from src.pipelines.document_rag.ragflow_backend import RAGFlowAPIError, RAGFlowBackend
from src.pipelines.registry import PIPELINE_REGISTRY, PROCESSOR_KIND_RAGFLOW


class _FailingStore:
    def __init__(self):
        self.remote_deletes = []

    def upsert_document(self, **kwargs):
        raise RuntimeError("store write failed")

    def delete_document_by_remote_id(self, dataset_id, document_id):
        self.remote_deletes.append((dataset_id, document_id))


class _Inspection:
    def to_warning_message(self):
        return ""

    def to_metadata(self):
        return {}


class _NotOwnerStatusClient:
    def __init__(self):
        self.deleted = []
        self.parsed = []

    def upload_document(self, dataset_id, archived_path):
        return "remote-1"

    def update_document_metadata(self, dataset_id, document_id, metadata):
        return None

    def wait_document_ready(self, dataset_id, document_id):
        raise RAGFlowAPIError({"code": 102, "message": "You don't own the document remote-1."})

    def parse_documents(self, dataset_id, document_ids):
        self.parsed.append((dataset_id, list(document_ids)))

    def delete_documents(self, dataset_id, document_ids):
        self.deleted.append((dataset_id, list(document_ids)))


class RAGFlowHandlerSubmitRollbackTests(unittest.TestCase):
    def test_submit_cleans_remote_when_store_write_fails(self):
        store = _FailingStore()
        cleanups = []
        spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_RAGFLOW)
        handler = RAGFlowDocumentHandler(
            spec=spec,
            store=store,
            submit_callback=lambda *args, **kwargs: SimpleNamespace(document_id="remote-1"),
            cleanup_remote_callback=lambda dataset_id, document_id: cleanups.append((dataset_id, document_id)),
        )
        scope = IngestionScope(kb_name="kb", department_id="dept_a", uploaded_by="user")
        archived = ArchivedFile(
            original_path="source.pdf",
            archived_path="archive.pdf",
            filename="source.pdf",
            source_group="docs",
            relative_local_path="docs/source.pdf",
            file_size=123,
            content_hash="abc",
            inspection=_Inspection(),
        )

        with self.assertRaises(RuntimeError):
            handler.submit(scope, archived, "design", "dataset-1")

        self.assertEqual(cleanups, [("dataset-1", "remote-1")])
        self.assertEqual(store.remote_deletes, [("dataset-1", "remote-1")])

    def test_submit_continues_when_status_is_not_readable_after_upload(self):
        backend = object.__new__(RAGFlowBackend)
        client = _NotOwnerStatusClient()
        backend._client_instance = client
        backend._metadata = lambda *args, **kwargs: {}

        result = backend._submit_archived_document(
            "dataset-1",
            "kb",
            "source.pdf",
            "archive.pdf",
            "docs",
        )

        self.assertEqual(result.document_id, "remote-1")
        self.assertEqual(client.parsed, [("dataset-1", ["remote-1"])])
        self.assertEqual(client.deleted, [])


if __name__ == "__main__":
    unittest.main()
