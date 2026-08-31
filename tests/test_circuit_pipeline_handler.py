import os
import tempfile
import unittest

from src.circuit.index_service import CircuitIndexResult
from src.pipelines.ingestion import ArchivedFile, CircuitPipelineHandler, IngestionScope
from src.pipelines.registry import CONTENT_KIND_CIRCUIT, DATASET_CIRCUIT, PIPELINE_REGISTRY, PROCESSOR_KIND_CIRCUIT


class _Inspection:
    def to_warning_message(self):
        return ""

    def to_metadata(self):
        return {}


class _Store:
    def __init__(self):
        self.documents = []
        self.deleted = []
        self.progress_updates = []

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


class _CircuitIndex:
    def __init__(self, *, fail=False, result=None):
        self.fail = fail
        self.result = result
        self.index_calls = []
        self.deleted = []

    def index_file(self, **kwargs):
        self.index_calls.append(kwargs)
        if self.fail:
            raise ValueError("parse boom")
        return self.result or type("Result", (), {"warnings": ["weak net name"], "stats": {"net_count": 1}})()

    def delete_record(self, record):
        self.deleted.append(record)


class CircuitPipelineHandlerTests(unittest.TestCase):
    def _archived_file(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "main_board.edf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("(edif main_board)")
        archived = ArchivedFile(
            original_path=path,
            archived_path=path,
            filename="main_board.edf",
            source_group="netlist data",
            relative_local_path="design-data/main_board.edf",
            file_size=os.path.getsize(path),
            content_hash="abcdef123456",
            inspection=_Inspection(),
        )
        return tmp, archived

    def test_submit_indexes_circuit_file_without_remote_upload(self):
        store = _Store()
        circuit_index = _CircuitIndex()
        handler = CircuitPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_CIRCUIT),
            store=store,
            circuit_index=circuit_index,
        )
        tmp, archived = self._archived_file()
        with tmp:
            result = handler.submit(
                IngestionScope(kb_name="kb_hw", department_id="dept_hw", uploaded_by="alice", kb_id=3),
                archived,
                default_dataset_kind="design",
                default_dataset_id="ragflow-dataset",
            )

        self.assertTrue(result.success)
        self.assertFalse(result.uploaded_to_remote)
        self.assertEqual(result.audit_action, "circuit_upload_indexed")
        self.assertEqual(store.documents[0]["dataset_kind"], DATASET_CIRCUIT)
        self.assertEqual(store.documents[0]["dataset_id"], "")
        self.assertEqual(store.documents[0]["document_id"], "circuit:abcdef123456")
        self.assertEqual(store.documents[0]["content_kind"], CONTENT_KIND_CIRCUIT)
        self.assertEqual(store.documents[0]["processor_kind"], PROCESSOR_KIND_CIRCUIT)
        self.assertEqual(store.documents[0]["status"], "archived")
        self.assertEqual(result.status, "indexed")
        self.assertEqual(store.progress_updates[-1]["status"], "indexed")
        self.assertEqual(store.progress_updates[-1]["progress"], 100)
        self.assertEqual(circuit_index.index_calls[0]["kb_name"], "kb_hw")
        self.assertEqual(circuit_index.index_calls[0]["record_id"], 7)
        self.assertEqual(circuit_index.index_calls[0]["file_path"], archived.archived_path)
        self.assertEqual(circuit_index.index_calls[0]["original_name"], "main_board.edf")
        self.assertEqual(circuit_index.index_calls[0]["department_id"], "dept_hw")
        self.assertEqual(circuit_index.index_calls[0]["uploaded_by"], "alice")

    def test_submit_marks_record_failed_when_circuit_indexing_fails(self):
        store = _Store()
        handler = CircuitPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_CIRCUIT),
            store=store,
            circuit_index=_CircuitIndex(fail=True),
        )
        tmp, archived = self._archived_file()
        with tmp:
            result = handler.submit(
                IngestionScope(kb_name="kb_hw", department_id="dept_hw", uploaded_by="alice", kb_id=3),
                archived,
                default_dataset_kind="design",
                default_dataset_id="ragflow-dataset",
            )

        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.audit_action, "circuit_upload_failed")
        self.assertEqual(store.progress_updates[-1]["status"], "failed")
        self.assertEqual(store.progress_updates[-1]["progress"], 100)
        self.assertIn("parse boom", store.progress_updates[-1]["error_message"])

    def test_submit_persists_and_exposes_degraded_derived_index_status(self):
        store = _Store()
        circuit_index = _CircuitIndex(result=CircuitIndexResult(
            ok=True,
            status="degraded",
            message="Indexed circuit design with graph index unavailable",
            warnings=["Graph index persistence failed."],
            stats={"instance_count": 3, "graph_node_count": 0},
            design_id="main_board",
        ))
        handler = CircuitPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_CIRCUIT),
            store=store,
            circuit_index=circuit_index,
        )
        progress = []
        tmp, archived = self._archived_file()
        with tmp:
            result = handler.submit(
                IngestionScope(kb_name="kb_hw", department_id="dept_hw", uploaded_by="alice", kb_id=3),
                archived,
                default_dataset_kind="design",
                default_dataset_id="ragflow-dataset",
                progress_callback=lambda percent, message: progress.append((percent, message)),
            )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.warnings, ["Graph index persistence failed."])
        self.assertIn("graph index unavailable", result.message)
        self.assertEqual(store.progress_updates[-1]["status"], "degraded")
        self.assertIn("graph index unavailable", store.progress_updates[-1]["stage"])
        self.assertEqual(progress[-1][0], 100)
        self.assertIn("degraded", progress[-1][1])
        self.assertEqual(result.audit_action, "circuit_upload_degraded")
        self.assertEqual(result.audit_metadata["status"], "degraded")
        self.assertEqual(result.audit_metadata["circuit_index_status"], "degraded")
        self.assertEqual(result.audit_metadata["circuit_index_message"], circuit_index.result.message)
        self.assertEqual(result.audit_metadata["circuit_index_warnings"], circuit_index.result.warnings)
        self.assertEqual(result.audit_metadata["circuit_stats"], circuit_index.result.stats)

    def test_delete_record_calls_circuit_index_cleanup_before_store_delete(self):
        store = _Store()
        circuit_index = _CircuitIndex()
        handler = CircuitPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_CIRCUIT),
            store=store,
            circuit_index=circuit_index,
        )
        record = type("Record", (), {"id": 9, "document_name": "main_board.edf", "local_path": ""})()
        archive = type("Archive", (), {"remove_record_archive": lambda self, record: None})()

        result = handler.delete_record(record, archive)

        self.assertTrue(result.ok)
        self.assertEqual(circuit_index.deleted, [record])
        self.assertEqual(store.deleted, [9])


if __name__ == "__main__":
    unittest.main()
