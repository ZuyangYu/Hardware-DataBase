from __future__ import annotations

from types import SimpleNamespace

from src.pipelines.document_store import PipelineDocumentStore
from src.services.pipeline_directories import PipelineDirectoryService, document_relative_path
from src.services.retrieval_targets import resolve_turn_targets


def _record(store, directory_id, record_id, content_hash):
    store.upsert_document(
        kb_name="kb",
        document_name="spec.pdf",
        original_file_name="spec.pdf",
        dataset_kind="design",
        dataset_id="dataset",
        document_id=f"remote-{record_id}",
        source_group="文档资料",
        department_id="dept",
        uploaded_by="tester",
        kb_id=7,
        directory_id=directory_id,
        status="parsed",
        content_hash=content_hash,
    )


def test_relative_path_target_selects_the_correct_same_named_file(tmp_path):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    directories = PipelineDirectoryService(store=store)
    docs = directories.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="docs", created_by="tester",
    )
    design = directories.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="design", created_by="tester",
    )
    _record(store, docs["id"], 1, "hash-docs")
    _record(store, design["id"], 2, "hash-design")
    records = store.list_documents("kb", "dept")
    source = SimpleNamespace(kb_id=7, kb_name="kb", department_id="dept")

    assert {
        document_relative_path(store, record) for record in records
    } == {"docs/spec.pdf", "design/spec.pdf"}
    resolved = resolve_turn_targets(
        "请读取 docs/spec.pdf",
        sources=[source],
        document_store=store,
    )
    assert resolved.resolution == "resolved"
    assert [target.record_id for target in resolved.targets] == [records[0].id]

    ambiguous = resolve_turn_targets(
        "请读取 spec.pdf",
        sources=[source],
        document_store=store,
    )
    assert ambiguous.resolution == "target_ambiguous"
    assert ambiguous.targets == ()
