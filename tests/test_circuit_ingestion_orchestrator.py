import os
import shutil
import tempfile
import unittest

from src.pipelines.ingestion import CircuitPipelineHandler, IngestionOrchestrator, IngestionScope
from src.pipelines.registry import DATASET_CIRCUIT, PIPELINE_REGISTRY, PROCESSOR_KIND_CIRCUIT


class _Store:
    def __init__(self):
        self.documents = []
        self.deleted = []
        self.progress_updates = []

    def find_by_hash(self, kb_name, dataset_kind, content_hash, department_id=None):
        return None

    def upsert_document(self, **kwargs):
        self.documents.append(kwargs)

    def get_document(self, kb_name, document_name, dataset_kind, department_id=None):
        return type("Record", (), {"id": 7})()

    def update_document_progress_by_id(self, record_id, progress, stage, status=None, error_message=None):
        self.progress_updates.append(
            {
                "record_id": record_id,
                "progress": progress,
                "stage": stage,
                "status": status,
                "error_message": error_message,
            }
        )

    def delete_document_by_id(self, record_id):
        self.deleted.append(record_id)


class _Archive:
    def __init__(self, root):
        self.root = root
        self.last_archived_path = ""

    def archive_source_file(self, kb_name, file_path, source_group, department_id=None):
        os.makedirs(self.root, exist_ok=True)
        archived_path = os.path.join(self.root, os.path.basename(file_path))
        shutil.copy2(file_path, archived_path)
        self.last_archived_path = archived_path
        return archived_path, os.path.basename(file_path), source_group or "netlist data"

    def kb_path(self, kb_name, department_id=None):
        return self.root


class _CircuitIndex:
    def index_file(self, **kwargs):
        raise ValueError("parse boom")


class CircuitIngestionOrchestratorTests(unittest.TestCase):
    def test_circuit_index_failure_preserves_failed_record_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            store = _Store()
            archive = _Archive(os.path.join(tmp, "archive"))
            handler = CircuitPipelineHandler(
                spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_CIRCUIT),
                store=store,
                circuit_index=_CircuitIndex(),
            )
            orchestrator = IngestionOrchestrator(
                backend_name="test",
                store=store,
                archive=archive,
                handlers={PROCESSOR_KIND_CIRCUIT: handler},
                audit_callback=lambda *args, **kwargs: None,
                content_hash_callback=lambda path: "abcdef123456",
                remote_document_exists_callback=lambda record: False,
            )

            result = orchestrator.upload_files(
                [source],
                IngestionScope(kb_name="kb_hw", department_id="dept_hw", uploaded_by="alice"),
                default_dataset_kind=DATASET_CIRCUIT,
                default_dataset_id="",
            )

            self.assertEqual(result.failed_count, 1)
            self.assertEqual(store.deleted, [])
            self.assertTrue(os.path.exists(archive.last_archived_path))
            self.assertEqual(store.progress_updates[-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
