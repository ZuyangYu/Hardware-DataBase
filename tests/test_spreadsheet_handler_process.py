import unittest

from src.pipelines.document_store import PipelineDocumentRecord
from src.pipelines.ingestion import SpreadsheetPipelineHandler
from src.pipelines.registry import PIPELINE_REGISTRY, PROCESSOR_KIND_SPREADSHEET
from src.pipelines.spreadsheet.pipeline import SpreadsheetIndexResult


class _Store:
    def __init__(self):
        self.progress_updates = []
        self.released = []
        self.deleted = []

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

    def release_parse_claim(self, record_id):
        self.released.append(record_id)

    def delete_document_by_id(self, record_id):
        self.deleted.append(record_id)


class _Archive:
    def resolve_record_path(self, record):
        return f"D:/archive/{record.department_id}/{record.kb_name}/{record.document_name}"

    def remove_record_archive(self, record):
        return None


def _record():
    return PipelineDocumentRecord(
        id=42,
        kb_id=314,
        kb_name="kb_hw",
        document_name="hardware.xlsx",
        original_file_name="hardware.xlsx",
        dataset_kind="table",
        dataset_id="",
        document_id="table:abc",
        source_group="design",
        department_id="dept_hw",
        uploaded_by="alice",
        status="archived",
        processor_kind=PROCESSOR_KIND_SPREADSHEET,
        local_path="design/hardware.xlsx",
        content_hash="abc123",
    )


class SpreadsheetPipelineHandlerProcessTests(unittest.TestCase):
    def test_process_record_builds_department_scoped_index_request(self):
        store = _Store()
        calls = []

        def parse_index(request, progress_callback=None):
            calls.append(request)
            self.assertIsNotNone(progress_callback)
            progress_callback(55, "halfway")
            return SpreadsheetIndexResult(ok=True, status="indexed", message="indexed ok")

        handler = SpreadsheetPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET),
            store=store,
            ensure_worker_callback=lambda: None,
            parse_index_callback=parse_index,
        )

        result = handler.process_record(_record(), _Archive())

        self.assertTrue(result.ok)
        self.assertEqual(result.audit_action, "spreadsheet_upload_indexed")
        self.assertEqual(result.audit_metadata["store_id"], 42)
        self.assertEqual(result.audit_metadata["kb_id"], 314)
        self.assertEqual(result.audit_metadata["processor_kind"], PROCESSOR_KIND_SPREADSHEET)
        self.assertEqual(store.released, [42])
        self.assertEqual(store.progress_updates[-1]["status"], "indexed")
        self.assertEqual(calls[0].department_id, "dept_hw")
        self.assertEqual(calls[0].kb_id, 314)
        self.assertEqual(calls[0].kb_name, "kb_hw")
        self.assertEqual(calls[0].record_id, 42)
        self.assertTrue(calls[0].file_path.endswith("dept_hw/kb_hw/hardware.xlsx"))

    def test_delete_record_keeps_store_row_when_index_cleanup_fails(self):
        store = _Store()
        handler = SpreadsheetPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET),
            store=store,
            ensure_worker_callback=lambda: None,
            delete_index_callback=lambda record: (_ for _ in ()).throw(RuntimeError("index cleanup failed")),
        )

        result = handler.delete_record(_record(), _Archive())

        self.assertFalse(result.ok)
        self.assertEqual(store.deleted, [])
        self.assertIn("spreadsheet index", result.errors[0])

    def test_on_stale_existing_cleans_orphaned_table_index(self):
        # Regression for #2: when a stale spreadsheet mapping is rebuilt, the
        # old record_id's table index rows must be dropped (otherwise the new
        # submit creates a new record_id and leaves the old rows as orphans).
        deleted = []
        handler = SpreadsheetPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET),
            store=_Store(),
            ensure_worker_callback=lambda: None,
            delete_index_callback=lambda record: deleted.append(record.id),
        )

        handler.on_stale_existing(_record())

        self.assertEqual(deleted, [42])

    def test_on_stale_existing_without_delete_index_is_safe(self):
        # No delete_index configured (e.g. construct path in tests) must not raise.
        handler = SpreadsheetPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET),
            store=_Store(),
            ensure_worker_callback=lambda: None,
        )

        handler.on_stale_existing(_record())  # should not raise

    def test_can_reuse_existing_returns_false_for_dead_letter(self):
        # Regression for #3: a permanently-failed (dead_letter) spreadsheet record
        # must NOT be reused. The worker never reclaims it (retry_count exhausted),
        # so reuse would silently skip the file. Returning False routes it through
        # the stale-rebuild path so re-upload recovers it.
        handler = SpreadsheetPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET),
            store=_Store(),
            ensure_worker_callback=lambda: None,
        )
        record = _record()
        record.status = "dead_letter"

        self.assertFalse(handler.can_reuse_existing(record))

    def test_can_reuse_existing_still_requeues_failed(self):
        # A transient "failed" record is still recoverable via the worker, so it
        # must remain reusable (re-queued). Guards against over-broadening #3.
        store = _Store()
        worker_calls = []
        handler = SpreadsheetPipelineHandler(
            spec=PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET),
            store=store,
            ensure_worker_callback=lambda: worker_calls.append(True),
        )
        record = _record()
        record.status = "failed"

        self.assertTrue(handler.can_reuse_existing(record))
        self.assertEqual(worker_calls, [True])


if __name__ == "__main__":
    unittest.main()
