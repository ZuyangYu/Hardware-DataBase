from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.pipelines.document_rag.schemas import IngestFileResult, IngestResult
from src.pipelines.document_store import PipelineDocumentStore
from src.services.directory_imports import DirectoryImportService
from src.services.pipeline_directories import DirectoryValidationError, PipelineDirectoryService


class _Pipeline:
    def __init__(self, result: IngestResult | None = None):
        self.result = result or IngestResult(
            success_count=1,
            total_count=1,
            file_results=[
                IngestFileResult(
                    index=0,
                    relative_path="docs/spec.pdf",
                    status="success",
                    record_id=42,
                )
            ],
        )
        self.calls: list[tuple] = []

    def upload_files(self, files, kb_name, **kwargs):
        self.calls.append((files, kb_name, kwargs))
        return self.result


def _service(tmp_path, monkeypatch, pipeline=None):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    directories = PipelineDirectoryService(store=store)
    base = directories.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="incoming", created_by="tester",
    )
    monkeypatch.setattr(
        "src.services.request_context.build_context_for_user",
        lambda user, kb_name=None, auth=None: SimpleNamespace(
            has_kb_permission=lambda *args, **kwargs: True
        ),
    )
    auth = SimpleNamespace(
        get_user_by_username=lambda username: SimpleNamespace(
            id=3, username=username, is_active=True, department_id="dept"
        )
    )
    service = DirectoryImportService(
        store=store, pipeline=pipeline or _Pipeline(), auth=auth
    )
    return store, directories, service, base


def _entry(tmp_path, name, relative_path):
    staged = tmp_path / name
    staged.write_bytes(name.encode())
    return {
        "relative_path": relative_path,
        "source_group": "文档资料",
        "staged_path": str(staged),
        "file_size": staged.stat().st_size,
    }


def test_import_job_resolves_nested_directories_and_runs_files(tmp_path, monkeypatch):
    store, directories, service, base = _service(tmp_path, monkeypatch)
    entry = _entry(tmp_path, "spec.pdf", "docs/spec.pdf")

    job = service.create_job(
        kb_name="kb",
        department_id="dept",
        kb_id=7,
        directory_id=base["id"],
        created_by="tester",
        entries=[entry],
    )
    assert job["total_files"] == 1
    files = store.list_directory_import_files(job["id"], "kb", "dept")
    assert files[0]["relative_path"] == "incoming/docs/spec.pdf"
    assert files[0]["status"] == "queued"
    assert directories.path_for_directory(
        kb_name="kb", department_id="dept", directory_id=files[0]["directory_id"]
    ) == "incoming/docs"

    assert service.run_once("worker-1") is True
    refreshed = store.get_directory_import_job(job["id"], "kb", "dept")
    assert refreshed["status"] == "completed"
    assert refreshed["success_count"] == 1
    files = store.list_directory_import_files(job["id"], "kb", "dept")
    assert files[0]["status"] == "success"
    assert files[0]["record_id"] == 42
    assert not os.path.exists(entry["staged_path"])


def test_cancel_marks_queued_files_and_retry_requeues_them(tmp_path, monkeypatch):
    store, directories, service, base = _service(tmp_path, monkeypatch)
    entries = [
        _entry(tmp_path, "a.pdf", "a.pdf"),
        _entry(tmp_path, "b.pdf", "b.pdf"),
    ]
    job = service.create_job(
        kb_name="kb", department_id="dept", kb_id=7, directory_id=base["id"],
        created_by="tester", entries=entries,
    )
    cancelled = store.request_directory_import_cancel(job["id"], "kb", "dept")
    assert cancelled["status"] == "cancelled"
    assert cancelled["skipped_count"] == 2
    assert service.run_once("worker-1") is False

    retried = store.retry_directory_import(job["id"], "kb", "dept")
    assert retried["status"] == "queued"
    assert all(
        row["status"] == "queued"
        for row in store.list_directory_import_files(job["id"], "kb", "dept")
    )
    assert service.run_once("worker-2") is True


def test_import_manifest_rejects_traversal_before_creating_directories(tmp_path, monkeypatch):
    store, directories, service, base = _service(tmp_path, monkeypatch)
    entry = _entry(tmp_path, "escape.pdf", "../escape.pdf")
    with pytest.raises(DirectoryValidationError):
        service.create_job(
            kb_name="kb", department_id="dept", kb_id=7, directory_id=base["id"],
            created_by="tester", entries=[entry],
        )
    assert store.list_directories("kb", "dept", parent_id=base["id"]) == []


def test_all_skipped_batch_is_failed_not_cancelled(tmp_path, monkeypatch):
    pipeline = _Pipeline(
        IngestResult(
            success_count=0,
            total_count=1,
            skipped_count=1,
            file_results=[
                IngestFileResult(
                    index=0,
                    relative_path="archive.zip",
                    status="skipped",
                    error_code="unsupported_file_type",
                    message="unsupported",
                )
            ],
        )
    )
    store, directories, service, base = _service(tmp_path, monkeypatch, pipeline)
    entry = _entry(tmp_path, "archive.zip", "archive.zip")
    job = service.create_job(
        kb_name="kb", department_id="dept", kb_id=7, directory_id=base["id"],
        created_by="tester", entries=[entry],
    )
    assert service.run_once("worker-1") is True
    refreshed = store.get_directory_import_job(job["id"], "kb", "dept")
    assert refreshed["status"] == "failed"
    assert refreshed["skipped_count"] == 1
    assert "unsupported" in refreshed["last_error"]


def test_import_history_can_be_deleted_without_touching_imported_files(tmp_path, monkeypatch):
    store, directories, service, base = _service(tmp_path, monkeypatch)
    entry = _entry(tmp_path, "spec.pdf", "docs/spec.pdf")
    job = service.create_job(
        kb_name="kb", department_id="dept", kb_id=7, directory_id=base["id"],
        created_by="tester", entries=[entry],
    )
    with pytest.raises(ValueError, match="仍在运行"):
        store.delete_directory_import_job(job["id"], "kb", "dept")

    assert service.run_once("worker-1") is True
    store.upsert_document(
        kb_name="kb", document_name="spec.pdf", original_file_name="spec.pdf",
        dataset_kind="design", dataset_id="d", document_id="remote-1",
        source_group="文档资料", department_id="dept", uploaded_by="tester",
        kb_id=7, directory_id=base["id"], status="parsed", content_hash="hash",
    )

    staged_paths = store.delete_directory_import_job(job["id"], "kb", "dept")
    assert staged_paths
    assert store.get_directory_import_job(job["id"], "kb", "dept") is None
    assert (
        store.get_document(
            "kb", "spec.pdf", "design", department_id="dept",
            directory_id=base["id"],
        )
        is not None
    )


def test_knowledge_base_delete_removes_import_rows_and_returns_staged_paths(tmp_path, monkeypatch):
    store, directories, service, base = _service(tmp_path, monkeypatch)
    entry = _entry(tmp_path, "spec.pdf", "docs/spec.pdf")
    job = service.create_job(
        kb_name="kb", department_id="dept", kb_id=7, directory_id=base["id"],
        created_by="tester", entries=[entry],
    )
    staged = store.delete_directory_imports_by_kb("kb", "dept")
    assert staged == [entry["staged_path"]]
    assert store.get_directory_import_job(job["id"], "kb", "dept") is None
    assert store.list_directory_import_files(job["id"], "kb", "dept") == []


def test_import_file_failure_isolated_from_other_rows(tmp_path, monkeypatch):
    pipeline = _Pipeline(
        IngestResult(
            success_count=0,
            total_count=1,
            failed_count=1,
            file_results=[
                IngestFileResult(
                    index=0,
                    relative_path="bad.pdf",
                    status="failed",
                    error_code="parse_submission_failed",
                    message="boom",
                )
            ],
        )
    )
    store, directories, service, base = _service(tmp_path, monkeypatch, pipeline)
    entry = _entry(tmp_path, "bad.pdf", "bad.pdf")
    job = service.create_job(
        kb_name="kb", department_id="dept", kb_id=7, directory_id=base["id"],
        created_by="tester", entries=[entry],
    )
    assert service.run_once("worker-1") is True
    refreshed = store.get_directory_import_job(job["id"], "kb", "dept")
    assert refreshed["status"] == "failed"
    row = store.list_directory_import_files(job["id"], "kb", "dept")[0]
    assert row["status"] == "failed"
    assert row["error_code"] == "parse_submission_failed"
    assert os.path.exists(entry["staged_path"])


def test_stale_uploading_file_is_reclaimed(tmp_path, monkeypatch):
    store, directories, service, base = _service(tmp_path, monkeypatch)
    entry = _entry(tmp_path, "spec.pdf", "spec.pdf")
    job = service.create_job(
        kb_name="kb", department_id="dept", kb_id=7, directory_id=base["id"],
        created_by="tester", entries=[entry],
    )
    files = store.list_directory_import_files(job["id"], "kb", "dept")
    file_id = int(files[0]["id"])
    conn = store._connect()
    try:
        conn.execute(
            """
            UPDATE directory_import_files
            SET status = 'uploading', worker_id = 'dead-worker',
                worker_started_at = '2000-01-01 00:00:00'
            WHERE id = ?
            """,
            (file_id,),
        )
    finally:
        conn.close()

    claimed = store.claim_directory_import_file("worker-2")
    assert claimed is not None
    assert int(claimed["id"]) == file_id
    assert claimed["worker_id"] == "worker-2"
    assert claimed["status"] == "uploading"
