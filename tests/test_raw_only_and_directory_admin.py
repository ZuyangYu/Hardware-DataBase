from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.ingestion import IngestionOrchestrator, IngestionScope, RawOnlyPipelineHandler
from src.pipelines.registry import PROCESSOR_KIND_RAW
from src.services import pipeline_directories
from src.services.pipeline_directories import DirectoryValidationError, PipelineDirectoryService


class _Archive:
    def __init__(self, root):
        self.root = root

    def archive_source_file(self, kb_name, file_path, source_group, department_id=None, **kwargs):
        target = os.path.join(self.root, os.path.basename(file_path))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(file_path, "rb") as source, open(target, "wb") as dest:
            dest.write(source.read())
        return target, os.path.basename(file_path), source_group or "未分类"

    def kb_path(self, kb_name, department_id=None, create=False):
        return self.root

    def record_archive_exists(self, record):
        return True

    def inspect_record_archive(self, record):
        return {}


def _scope():
    return IngestionScope(
        kb_name="kb", department_id="dept", kb_id=7, uploaded_by="tester",
        source_group="未分类", directory_id=3,
    )


def test_raw_only_handler_archives_without_creating_parse_work(tmp_path):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    source = tmp_path / "unknown.bin"
    source.write_bytes(b"opaque")
    handler = RawOnlyPipelineHandler(store=store)

    result = handler.submit(
        _scope(),
        archived=SimpleNamespace(
            filename="unknown.bin",
            original_path=str(source),
            archived_path=str(source),
            source_group="未分类",
            relative_local_path="directories/3/未分类/unknown.bin",
            file_size=6,
            content_hash="abcdef1234567890",
            inspection=SimpleNamespace(to_warning_message=lambda: "", to_metadata=lambda: {}),
            logical_relative_path="docs/unknown.bin",
            directory_id=3,
        ),
        default_dataset_kind="design",
        default_dataset_id="",
    )

    record = store.get_document("kb", "unknown.bin", "raw", department_id="dept", directory_id=3)
    assert result.success is True
    assert record.processor_kind == PROCESSOR_KIND_RAW
    assert record.status == "raw_archived"
    assert record.content_kind == "raw_file"
    assert record.parse_progress == 100


def test_unknown_file_uses_raw_only_only_when_enabled(tmp_path, monkeypatch):
    import src.settings

    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    archive = _Archive(str(tmp_path / "archives"))
    orchestrator = IngestionOrchestrator(
        backend_name="ragflow",
        store=store,
        archive=archive,
        handlers={},
        audit_callback=lambda *args, **kwargs: None,
        content_hash_callback=lambda path: "hash-unknown",
        remote_document_exists_callback=lambda record: True,
    )
    source = tmp_path / "opaque.bin"
    source.write_bytes(b"opaque")

    monkeypatch.setattr(src.settings, "DIRECTORY_RAW_ONLY_UNKNOWN", False)
    skipped = orchestrator.upload_files([str(source)], _scope(), "design", "")
    assert skipped.skipped_count == 1
    assert skipped.file_results[0].error_code == "unsupported_file_type"

    monkeypatch.setattr(src.settings, "DIRECTORY_RAW_ONLY_UNKNOWN", True)
    accepted = orchestrator.upload_files([str(source)], _scope(), "design", "")
    assert accepted.success_count == 1
    record = store.get_document(
        "kb", "opaque.bin", "raw", department_id="dept", directory_id=3
    )
    assert record is not None and record.processor_kind == PROCESSOR_KIND_RAW


def test_move_directory_updates_subtree_paths_and_rejects_cycles(tmp_path):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    service = PipelineDirectoryService(store=store)
    a = service.create(kb_name="kb", department_id="dept", kb_id=7, parent_id=0, name="a", created_by="t")
    b = service.create(kb_name="kb", department_id="dept", kb_id=7, parent_id=a["id"], name="b", created_by="t")
    c = service.create(kb_name="kb", department_id="dept", kb_id=7, parent_id=0, name="c", created_by="t")

    moved = service.move(kb_name="kb", department_id="dept", directory_id=b["id"], parent_id=c["id"])
    assert moved["path"] == "c/b"
    with pytest.raises(DirectoryValidationError, match="自身或其子目录"):
        service.move(kb_name="kb", department_id="dept", directory_id=c["id"], parent_id=b["id"])


def test_recursive_directory_delete_removes_files_and_tree(tmp_path):
    from src.core.app_pipeline import AppPipeline

    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    service = PipelineDirectoryService(store=store)
    parent = service.create(kb_name="kb", department_id="dept", kb_id=7, parent_id=0, name="docs", created_by="t")
    child = service.create(kb_name="kb", department_id="dept", kb_id=7, parent_id=parent["id"], name="power", created_by="t")
    store.upsert_document(
        kb_name="kb", document_name="spec.pdf", original_file_name="spec.pdf",
        dataset_kind="design", dataset_id="d", document_id="remote-1",
        source_group="文档资料", department_id="dept", uploaded_by="t",
        kb_id=7, directory_id=child["id"], status="parsed", content_hash="hash",
    )
    deleted = []

    class _Documents:
        def delete_document(self, file_id, kb_name, ctx=None):
            deleted.append(file_id)
            store.delete_document_by_id(int(str(file_id).split(":", 1)[1]))
            return SimpleNamespace(ok=True, message="deleted")

    pipeline = object.__new__(AppPipeline)
    pipeline.backend = SimpleNamespace(store=store)
    pipeline.documents = _Documents()
    result = pipeline.delete_directory_recursive(
        "kb", parent["id"], ctx=SimpleNamespace(metadata={"department_id": "dept"})
    )
    assert result["ok"] is True
    assert result["deleted_files"] == 1
    assert result["deleted_directories"] == 2
    assert deleted
    assert store.list_documents("kb", "dept") == []
    assert store.get_directory(parent["id"], "kb", "dept") is None


def test_reconciliation_reports_missing_archive_and_orphan_file(tmp_path, monkeypatch):
    import src.settings
    from src.services.pipeline_reconciliation import reconcile_pipeline

    monkeypatch.setattr(src.settings, "PIPELINE_ARCHIVE_ROOT", str(tmp_path / "archives"))
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    service = PipelineDirectoryService(store=store)
    directory = service.create(kb_name="kb", department_id="dept", kb_id=7, parent_id=0, name="docs", created_by="t")
    store.upsert_document(
        kb_name="kb", document_name="missing.pdf", original_file_name="missing.pdf",
        dataset_kind="design", dataset_id="d", document_id="remote-1",
        source_group="文档资料", department_id="dept", uploaded_by="t",
        kb_id=7, directory_id=directory["id"], status="parsed",
        content_hash="hash", local_path="directories/1/文档资料/missing.pdf",
    )
    orphan = tmp_path / "archives" / "departments" / "dept" / "kbs" / "kb" / "directories" / "1" / "文档资料" / "orphan.pdf"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")

    report = reconcile_pipeline(kb_name="kb", department_id="dept", limit=20, store=store)
    assert report["ok"] is False
    assert report["missing_archives"][0]["name"] == "missing.pdf"
    assert "directories/1/文档资料/orphan.pdf" in report["orphan_archives"]


def test_directory_quota_rejects_batch_over_limit(tmp_path, monkeypatch):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    service = PipelineDirectoryService(store=store)
    monkeypatch.setattr(pipeline_directories, "DIRECTORY_MAX_FILES", 2)
    monkeypatch.setattr(pipeline_directories, "DIRECTORY_MAX_BYTES", 0)
    directory = service.create(kb_name="kb", department_id="dept", kb_id=7, parent_id=0, name="docs", created_by="t")
    with pytest.raises(DirectoryValidationError, match="文件数超过配额"):
        service.assert_quota(
            kb_name="kb", department_id="dept",
            incoming={int(directory["id"]): (3, 0)},
        )
