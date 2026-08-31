"""Task 5a: governed record-id filtering and read-back authorization for document_rag."""

from __future__ import annotations



from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.pipelines.document_store import PipelineDocumentRecord
from src.pipelines.document_store_sqlite import PipelineDocumentStore


def _store(records):
    class _Store:
        def list_documents(self, kb_name, department_id=None):
            return [
                record
                for record in records
                if record.kb_name == kb_name
                and (department_id is None or record.department_id == department_id)
            ]

    return _Store()


class _Client:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def retrieve(self, question, dataset_ids, top_k, metadata_condition=None, should_cancel=None):
        self.calls.append((question, dataset_ids, top_k, metadata_condition))
        if self.responses:
            return self.responses.pop(0)
        return []


def _record(
    *,
    id=1,
    document_id="remote-a",
    document_name="datasheet_a.pdf",
    original_file_name="datasheet_a.pdf",
    department_id="dept_a",
    status="parsed",
    content_hash="hash-a",
    source_version_id="",
    revision="",
) -> PipelineDocumentRecord:
    return PipelineDocumentRecord(
        id=id,
        kb_name="kb",
        document_name=document_name,
        original_file_name=original_file_name,
        dataset_kind="design",
        dataset_id="dataset-design",
        document_id=document_id,
        source_group="设计数据",
        department_id=department_id,
        uploaded_by="tester",
        status=status,
        processor_kind="ragflow",
        content_hash=content_hash,
        source_version_id=source_version_id,
        revision=revision,
    )


def _backend(client, records) -> RAGFlowBackend:
    backend = object.__new__(RAGFlowBackend)
    backend.name = "ragflow"
    backend.client = client
    backend.store = _store(records)
    backend._dataset_ids = {"governance": "dataset-g", "设计数据": "dataset-d"}
    backend._ensure_physical_datasets = lambda: None
    backend._check_kb_access = lambda kb_name, ctx, required="read": None
    return backend


def _ctx(department="dept_a"):
    return RequestContext(metadata={"department_id": department})


def _chunk(remote_id, chunk_id, content):
    return {"id": chunk_id, "document_id": remote_id, "content": content, "similarity": 0.9}


def _record_a(**overrides):
    return _record(id=1, **overrides)


# ── strict allowed_record_ids path ───────────────────────────────────────────


def test_allowed_record_ids_return_only_linked_record_chunks():
    record_a = _record(id=1, document_id="remote-a", original_file_name="same.pdf", document_name="same.pdf")
    record_b = _record(id=2, document_id="remote-b", original_file_name="same.pdf", document_name="same.pdf")
    client = _Client([[_chunk("remote-a", "c1", "A evidence"), _chunk("remote-b", "c2", "B evidence")]])
    backend = _backend(client, [record_a, record_b])

    evidences = backend.retrieve(
        "kb", "query", top_k=5, ctx=_ctx(),
        filters={"allowed_record_ids": [1]},
    )

    contents = [item.content for item in evidences]
    assert contents == ["A evidence"]
    assert all(item.metadata.get("local_record_id") == 1 for item in evidences)


def test_same_name_documents_never_leak_through_strict_path():
    # Same KB, same department AND same original file name: only the linked
    # record's remote chunks may come back.
    record_a = _record(id=1, document_id="remote-a", original_file_name="twin.pdf")
    record_b = _record(id=2, document_id="remote-b", original_file_name="twin.pdf")
    client = _Client([[_chunk("remote-b", "c2", "B evidence"), _chunk("remote-a", "c1", "A evidence")]])
    backend = _backend(client, [record_a, record_b])

    evidences = backend.retrieve("kb", "query", top_k=5, ctx=_ctx(), filters={"allowed_record_ids": [2]})

    assert [item.content for item in evidences] == ["B evidence"]
    assert {item.metadata["local_record_id"] for item in evidences} == {2}


def test_unauthorized_or_unknown_allowed_ids_fail_closed_empty():
    record_a = _record(id=1, department_id="dept_a")
    cross_department = _record(id=2, department_id="dept_b")
    client = _Client()
    backend = _backend(client, [record_a, cross_department])

    # ID outside the caller's department → empty, no retrieval call at all.
    evidences = backend.retrieve("kb", "q", top_k=5, ctx=_ctx(), filters={"allowed_record_ids": [2]})
    assert evidences == []
    assert client.calls == []

    # Unknown record id → fail closed.
    evidences = backend.retrieve("kb", "q", top_k=5, ctx=_ctx(), filters={"allowed_record_ids": [99]})
    assert evidences == []
    assert client.calls == []

    # Empty allow-list → fail closed, never a broad search.
    evidences = backend.retrieve("kb", "q", top_k=5, ctx=_ctx(), filters={"allowed_record_ids": []})
    assert evidences == []
    assert client.calls == []


def test_non_completed_parse_status_fails_closed():
    pending = _record(id=3, status="uploaded")
    client = _Client()
    backend = _backend(client, [pending])

    evidences = backend.retrieve("kb", "q", top_k=5, ctx=_ctx(), filters={"allowed_record_ids": [3]})

    assert evidences == []
    assert client.calls == []


def test_version_stamp_mismatch_fails_closed():
    record = _record(id=4, content_hash="hash-current", revision="r2", source_version_id="v9")
    client = _Client()
    backend = _backend(client, [record])
    filters = {
        "allowed_record_ids": [4],
        "link_stamps": {
            "4": {"content_hash": "hash-stale", "source_version_id": "v9", "revision": "r2"},
        },
    }

    evidences = backend.retrieve("kb", "q", top_k=5, ctx=_ctx(), filters=filters)

    assert evidences == []
    assert client.calls == []


def test_matching_version_stamp_passes_and_single_call_no_broad_retry():
    record = _record(id=4, content_hash="hash-current", revision="r2", source_version_id="v9")
    client = _Client([[]])  # first (and only) call returns nothing
    backend = _backend(client, [record])
    filters = {
        "allowed_record_ids": [4],
        "link_stamps": {
            "4": {"content_hash": "hash-current", "source_version_id": "v9", "revision": "r2"},
        },
    }

    evidences = backend.retrieve("kb", "q", top_k=5, ctx=_ctx(), filters=filters)

    assert evidences == []
    # Strict path must not fall back to an unconditioned broad retrieve.
    assert len(client.calls) == 1


def test_without_allowed_record_ids_existing_behavior_unchanged():
    record_a = _record(id=1, document_id="remote-a")
    record_b = _record(id=2, document_id="remote-b")
    client = _Client([
        [_chunk("remote-a", "c1", "A"), _chunk("remote-b", "c2", "B")],
    ])
    backend = _backend(client, [record_a, record_b])

    evidences = backend.retrieve("kb", "q", top_k=5, ctx=_ctx())

    assert sorted(item.content for item in evidences) == ["A", "B"]


# ── DocumentProfile + invalidation outbox ────────────────────────────────────


def _sqlite_store(tmp_path):
    store = PipelineDocumentStore(db_path=str(tmp_path / "docs.db"))
    store.upsert_document(
        kb_name="kb",
        document_name="datasheet.pdf",
        original_file_name="datasheet.pdf",
        dataset_kind="design",
        dataset_id="dataset-design",
        document_id="remote-1",
        source_group="设计数据",
        department_id="dept_a",
        uploaded_by="tester",
        content_hash="hash-a",
    )
    record = store.get_document("kb", "datasheet.pdf", "design", department_id="dept_a")
    return store, record.id


def test_document_profile_roundtrip_and_confirmation(tmp_path):
    store, record_id = _sqlite_store(tmp_path)

    assert store.get_document_profile(record_id) is None

    store.confirm_document_identity(
        record_id,
        mpn_values=["GCM155R71C104KA55D"],
        manufacturer="Murata",
        origin={"field": "metadata.mpn", "confirmed_by": "alice"},
    )
    profile = store.get_document_profile(record_id)

    assert profile["record_id"] == record_id
    assert profile["mpn_values"] == ["GCM155R71C104KA55D"]
    assert profile["manufacturer"] == "Murata"
    assert profile["identity_origin"]["confirmed_by"] == "alice"
    # Governed stamps are copied from the lifecycle record.
    assert profile["remote_document_id"] == "remote-1"


def test_lifecycle_changes_write_invalidation_events(tmp_path):
    store, record_id = _sqlite_store(tmp_path)

    store.update_document_status_by_id(record_id, "parsed")
    store.delete_document_by_id(record_id)

    events = store.list_pending_profile_events()

    kinds = [event["event_kind"] for event in events]
    assert "parse_status" in kinds
    assert "deleted" in kinds

    store.mark_profile_event_processed(events[0]["id"])
    remaining = store.list_pending_profile_events()
    assert all(event["id"] != events[0]["id"] for event in remaining)


def test_profile_is_rebuildable_from_records(tmp_path):
    store, record_id = _sqlite_store(tmp_path)
    store.confirm_document_identity(record_id, mpn_values=["X"], manufacturer="M", origin={})

    store.rebuild_document_profiles()

    profile = store.get_document_profile(record_id)
    assert profile is not None
    assert profile["mpn_values"] == ["X"]
