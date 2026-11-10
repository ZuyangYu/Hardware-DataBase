from __future__ import annotations

import pytest

from src.pipelines.document_store import PipelineDocumentStore
from src.services.pipeline_directories import (
    DirectoryValidationError,
    PipelineDirectoryService,
    delete_directory_tree,
)


def _service(tmp_path):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    return store, PipelineDirectoryService(store=store)


def _document(store, *, directory_id: int, name: str = "spec.pdf"):
    store.upsert_document(
        kb_name="kb",
        document_name=name,
        original_file_name=name,
        dataset_kind="design",
        dataset_id="dataset",
        document_id=f"doc-{directory_id}-{name}",
        source_group="文档资料",
        department_id="dept",
        uploaded_by="tester",
        kb_id=7,
        directory_id=directory_id,
        status="parsed",
        content_hash=f"hash-{directory_id}-{name}",
    )


def test_directory_tree_derives_paths_and_renames_without_changing_ids(tmp_path):
    store, service = _service(tmp_path)

    root = service.get(kb_name="kb", department_id="dept", directory_id=0)
    assert root["path"] == ""
    docs = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="Docs", created_by="tester",
    )
    power = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=docs["id"],
        name="Power", created_by="tester",
    )
    record_id = power["id"]

    assert service.path_for_directory(
        kb_name="kb", department_id="dept", directory_id=record_id
    ) == "Docs/Power"
    assert service.get(
        kb_name="kb", department_id="dept", directory_id=record_id
    )["path"] == "Docs/Power"

    renamed = service.rename(
        kb_name="kb", department_id="dept", directory_id=docs["id"], name="Specifications"
    )
    assert renamed["id"] == docs["id"]
    assert service.path_for_directory(
        kb_name="kb", department_id="dept", directory_id=record_id
    ) == "Specifications/Power"


def test_directory_names_are_unique_among_siblings_but_scoped_by_kb_and_department(tmp_path):
    store, service = _service(tmp_path)
    service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="Specs", created_by="tester",
    )
    with pytest.raises(DirectoryValidationError, match="同级目录已存在"):
        service.create(
            kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
            name=" specs ", created_by="tester",
        )

    other = service.create(
        kb_name="other-kb", department_id="dept", kb_id=8, parent_id=0,
        name="Specs", created_by="tester",
    )
    assert other["path"] == "Specs"
    with pytest.raises(LookupError):
        service.get(kb_name="kb", department_id="other-dept", directory_id=other["id"])


def test_only_empty_directories_can_be_deleted(tmp_path):
    store, service = _service(tmp_path)
    parent = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="Docs", created_by="tester",
    )
    child = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=parent["id"],
        name="Power", created_by="tester",
    )
    with pytest.raises(DirectoryValidationError, match="子目录"):
        service.delete_empty(kb_name="kb", department_id="dept", directory_id=parent["id"])
    service.delete_empty(kb_name="kb", department_id="dept", directory_id=child["id"])

    _document(store, directory_id=parent["id"])
    with pytest.raises(DirectoryValidationError, match="不为空"):
        service.delete_empty(kb_name="kb", department_id="dept", directory_id=parent["id"])
    record = store.find_document_by_path("kb", "dept", parent["id"], "spec.pdf")
    store.delete_document_by_id(record.id)
    service.delete_empty(kb_name="kb", department_id="dept", directory_id=parent["id"])
    with pytest.raises(LookupError):
        service.get(kb_name="kb", department_id="dept", directory_id=parent["id"])


def test_pipeline_schema_report_validates_directory_aware_shape(tmp_path):
    from src.services.pipeline_reconciliation import pipeline_schema_report

    store, service = _service(tmp_path)
    folder = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="docs", created_by="tester",
    )
    _document(store, directory_id=folder["id"])
    _document(store, directory_id=0, name="legacy.pdf")
    report = pipeline_schema_report(store)
    assert report["ok"] is True
    assert report["documents"] == 2
    assert report["directories"] == 1
    assert report["root_backfilled_documents"] == 1
    assert report["duplicate_paths"] == []
    assert report["orphan_directories"] == []


def test_file_identity_and_hash_dedup_are_directory_scoped(tmp_path):
    store, service = _service(tmp_path)
    first_dir = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="one", created_by="tester",
    )
    second_dir = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="two", created_by="tester",
    )
    _document(store, directory_id=first_dir["id"], name="same.pdf")
    _document(store, directory_id=second_dir["id"], name="same.pdf")
    first = store.find_by_hash("kb", "design", f"hash-{first_dir['id']}-same.pdf", "dept", directory_id=first_dir["id"])
    second = store.find_by_hash("kb", "design", f"hash-{second_dir['id']}-same.pdf", "dept", directory_id=second_dir["id"])
    assert first is not None and second is not None
    assert first.id != second.id

    with pytest.raises(ValueError, match="文件路径冲突"):
        store.upsert_document(
            kb_name="kb",
            document_name="same.pdf",
            original_file_name="same.pdf",
            dataset_kind="design",
            dataset_id="dataset",
            document_id="different-content",
            source_group="文档资料",
            department_id="dept",
            uploaded_by="tester",
            kb_id=7,
            directory_id=first_dir["id"],
            status="parsed",
            content_hash="different-hash",
        )


def test_move_and_rename_is_atomic_on_sibling_conflict(tmp_path):
    store, service = _service(tmp_path)
    source = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="source", created_by="tester",
    )
    target = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="target", created_by="tester",
    )
    child = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=source["id"],
        name="power", created_by="tester",
    )
    grandchild = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=child["id"],
        name="rails", created_by="tester",
    )
    service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=target["id"],
        name="power", created_by="tester",
    )

    with pytest.raises(DirectoryValidationError, match="同级目录已存在"):
        service.move(
            kb_name="kb",
            department_id="dept",
            directory_id=child["id"],
            parent_id=target["id"],
            name="power",
        )
    # The failed rename must not leave the move half-applied.
    assert service.get(
        kb_name="kb", department_id="dept", directory_id=child["id"]
    )["parent_id"] == source["id"]

    moved = service.move(
        kb_name="kb",
        department_id="dept",
        directory_id=child["id"],
        parent_id=target["id"],
        name="renamed",
    )
    assert moved["parent_id"] == target["id"]
    assert moved["depth"] == 2
    assert service.path_for_directory(
        kb_name="kb", department_id="dept", directory_id=int(grandchild["id"])
    ) == "target/renamed/rails"
    assert service.get(
        kb_name="kb", department_id="dept", directory_id=int(grandchild["id"])
    )["depth"] == 3


def test_recursive_delete_is_blocked_by_active_import(tmp_path):
    store, service = _service(tmp_path)
    folder = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="docs", created_by="tester",
    )
    store.create_directory_import_job(
        kb_id=7,
        kb_name="kb",
        department_id="dept",
        directory_id=folder["id"],
        created_by="tester",
        files=[
            {
                "relative_path": "spec.pdf",
                "directory_id": folder["id"],
                "original_name": "spec.pdf",
                "staged_path": str(tmp_path / "spec.pdf"),
                "file_size": 1,
            }
        ],
    )
    result = delete_directory_tree(
        store=store,
        documents=None,
        kb_name="kb",
        department_id="dept",
        directory_id=folder["id"],
    )
    assert result["ok"] is False
    assert "正在进行的导入任务" in result["message"]
    assert service.get(
        kb_name="kb", department_id="dept", directory_id=folder["id"]
    )["id"] == folder["id"]


def test_recursive_delete_reports_directory_failure(tmp_path, monkeypatch):
    store, service = _service(tmp_path)
    folder = service.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="docs", created_by="tester",
    )
    monkeypatch.setattr(store, "delete_directory", lambda *args, **kwargs: False)
    result = delete_directory_tree(
        store=store,
        documents=None,
        kb_name="kb",
        department_id="dept",
        directory_id=folder["id"],
    )
    assert result["ok"] is False
    assert result["deleted_directories"] == 0
    assert result["errors"]
