import hashlib
import os
import tempfile
import unittest

import src.settings
from src.pipelines.ingestion import (
    HandlerResult,
    IngestionOrchestrator,
    IngestionScope,
    PipelineHandler,
    _INGEST_LOCKS,
    _ingest_lock,
)
from src.pipelines.registry import PROCESSOR_KIND_RAGFLOW, PipelineSpec
from src.services.document_archive import DocumentArchiveManager
from src.services.pipeline_asset_cleanup import PipelineAssetCleanupService


def _sha256_file(path: str) -> str:
    with open(path, "rb") as file_obj:
        return hashlib.sha256(file_obj.read()).hexdigest()


class _Archive:
    def __init__(self, root):
        self.root = root

    def kb_path(self, kb_name, department_id=None):
        if department_id in (None, ""):
            return os.path.join(self.root, "legacy", kb_name)
        return os.path.join(self.root, "departments", str(department_id), "kbs", kb_name)


class _Spreadsheets:
    def __init__(self, root):
        self.root = root

    def kb_index_path(self, department_id, kb_name, create=False):
        return os.path.join(self.root, "table_indexes", str(department_id), kb_name)


class _FailingSubmitHandler(PipelineHandler):
    spec = PipelineSpec(
        key="test",
        label="Test",
        processor_kind=PROCESSOR_KIND_RAGFLOW,
        content_kind="document_text",
        supported_extensions=frozenset({".pdf"}),
        dataset_kind="test",
    )

    def __init__(self):
        self.rollback_calls = []

    def existing_record_dataset_kind(self, default_dataset_kind: str) -> str:
        return default_dataset_kind

    def submit(self, scope, archived, default_dataset_kind, default_dataset_id, progress_callback=None):
        return HandlerResult(
            success=False,
            message=f"[failed] {archived.filename}: parse failed",
            record_id=123,
        )

    def rollback(self, result, scope):
        self.rollback_calls.append((result, scope))


class _Store:
    def find_by_hash(self, *args, **kwargs):
        return None

    def delete_document_by_id(self, record_id):
        return None


class PipelineAssetCleanupServiceTests(unittest.TestCase):
    def test_cleanup_knowledge_base_removes_department_scoped_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = _Archive(tmp)
            spreadsheets = _Spreadsheets(tmp)
            department_archive = archive.kb_path("kb", department_id="dept_a")
            spreadsheet_index = spreadsheets.kb_index_path("dept_a", "kb")
            os.makedirs(department_archive)
            os.makedirs(spreadsheet_index)

            result = PipelineAssetCleanupService(archive=archive, spreadsheets=spreadsheets).cleanup_knowledge_base(
                "kb",
                "dept_a",
            )

            self.assertTrue(result.ok)
            self.assertFalse(os.path.exists(department_archive))
            self.assertFalse(os.path.exists(spreadsheet_index))

    def test_cleanup_knowledge_base_removes_conversation_assets_scoped_by_department(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = src.settings.STORAGE_DIR
            try:
                src.settings.STORAGE_DIR = os.path.join(tmp, "storage")
                from src.external_conversations.store import ExternalConversationStore

                store = ExternalConversationStore()
                dept_a_dir = store.scope_dir("dept_a", "kb", create=True)
                dept_b_dir = store.scope_dir("dept_b", "kb", create=True)

                result = PipelineAssetCleanupService().cleanup_knowledge_base("kb", "dept_a")

                self.assertTrue(result.ok, result.errors)
                self.assertFalse(os.path.exists(dept_a_dir))
                self.assertTrue(os.path.exists(dept_b_dir))
            finally:
                src.settings.STORAGE_DIR = old_root

    def test_document_archive_uses_pipeline_archive_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = src.settings.PIPELINE_ARCHIVE_ROOT
            try:
                src.settings.PIPELINE_ARCHIVE_ROOT = os.path.join(tmp, "pipeline_archives")
                archive = DocumentArchiveManager()
                path = archive.kb_path("kb", department_id="dept_a", create=True)
                self.assertTrue(path.startswith(os.path.abspath(src.settings.PIPELINE_ARCHIVE_ROOT)))
                self.assertTrue(path.endswith(os.path.join("departments", "dept_a", "kbs", "kb")))
            finally:
                src.settings.PIPELINE_ARCHIVE_ROOT = old_root

    def test_document_archive_allocates_unique_name_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = src.settings.PIPELINE_ARCHIVE_ROOT
            try:
                src.settings.PIPELINE_ARCHIVE_ROOT = os.path.join(tmp, "pipeline_archives")
                source = os.path.join(tmp, "same.pdf")
                with open(source, "wb") as file_obj:
                    file_obj.write(b"first")

                archive = DocumentArchiveManager()
                first_path, first_name, _ = archive.archive_source_file("kb", source, "docs", department_id="dept_a")
                with open(source, "wb") as file_obj:
                    file_obj.write(b"second")
                second_path, second_name, _ = archive.archive_source_file("kb", source, "docs", department_id="dept_a")

                self.assertNotEqual(first_path, second_path)
                self.assertNotEqual(first_name, second_name)
                with open(first_path, "rb") as file_obj:
                    self.assertEqual(file_obj.read(), b"first")
                with open(second_path, "rb") as file_obj:
                    self.assertEqual(file_obj.read(), b"second")
            finally:
                src.settings.PIPELINE_ARCHIVE_ROOT = old_root

    def test_ingest_lock_is_removed_after_context_exits(self):
        _INGEST_LOCKS.clear()
        scope = IngestionScope(kb_name="kb", department_id="dept_a")

        with _ingest_lock(scope, "hash-a"):
            self.assertIn("dept_a:kb:hash-a", _INGEST_LOCKS)

        self.assertNotIn("dept_a:kb:hash-a", _INGEST_LOCKS)

    def test_failed_submission_removes_archived_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = src.settings.PIPELINE_ARCHIVE_ROOT
            try:
                src.settings.PIPELINE_ARCHIVE_ROOT = os.path.join(tmp, "pipeline_archives")
                source = os.path.join(tmp, "broken.pdf")
                with open(source, "wb") as file_obj:
                    file_obj.write(b"broken")

                handler = _FailingSubmitHandler()
                orchestrator = IngestionOrchestrator(
                    backend_name="test",
                    store=_Store(),
                    archive=DocumentArchiveManager(),
                    handlers={PROCESSOR_KIND_RAGFLOW: handler},
                    audit_callback=lambda *args, **kwargs: None,
                    content_hash_callback=_sha256_file,
                    remote_document_exists_callback=lambda record: False,
                )

                result = orchestrator.upload_files(
                    [source],
                    IngestionScope(kb_name="kb", department_id="dept_a", source_group="docs"),
                    default_dataset_kind="test",
                    default_dataset_id="dataset-test",
                )

                self.assertEqual(result.failed_count, 1)
                self.assertEqual(len(handler.rollback_calls), 1)
                archive_root = DocumentArchiveManager().kb_path("kb", department_id="dept_a")
                archived_files = [
                    os.path.join(root, name)
                    for root, _, names in os.walk(archive_root)
                    for name in names
                ]
                self.assertEqual(archived_files, [])
            finally:
                src.settings.PIPELINE_ARCHIVE_ROOT = old_root


if __name__ == "__main__":
    unittest.main()
