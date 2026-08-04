from __future__ import annotations

"""Stage 5 Task 2: RAGFlow ``balanced_route`` filter drops the source_group
hard filter while keeping the frozen ``source_names`` scope.

Mirrors the ``_Client``/``_Store``/``_backend`` fake pattern in
``test_ragflow_metadata_fallback.py``.
"""

from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.pipelines.document_store import PipelineDocumentRecord


# A query with >= 2 material keywords routes high-confidence to MATERIAL_GROUP
# ("物料数据"), so without balanced_route the server-side metadata_condition
# restricts to that group.
MATERIAL_QUERY = "bom 物料"


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
    """Pops canned responses in order, recording each call."""

    def __init__(self, responses: list[list[dict]]):
        self.responses = list(responses)
        self.calls: list[tuple[str, list[str], int, dict | None]] = []

    def retrieve(self, question, dataset_ids, top_k, metadata_condition=None):
        self.calls.append((question, dataset_ids, top_k, metadata_condition))
        return self.responses.pop(0)


class _RoutingClient:
    """Returns different chunks depending on whether the server-side
    metadata_condition restricts to a source_group.

    This simulates the P3 blind spot: when the route restricts to group X,
    RAGFlow returns chunks from group X documents (none of which are in the
    frozen set), so raw chunks > 0 and the 0-chunk fallback never fires. With
    no source_group condition (balanced_route), the frozen document's chunks
    come back.
    """

    def __init__(self, group_chunks: list[dict], balanced_chunks: list[dict]):
        self.group_chunks = group_chunks
        self.balanced_chunks = balanced_chunks
        self.calls: list[tuple[str, list[str], int, dict | None]] = []

    def retrieve(self, question, dataset_ids, top_k, metadata_condition=None):
        self.calls.append((question, dataset_ids, top_k, metadata_condition))
        if _has_source_group_condition(metadata_condition):
            return list(self.group_chunks)
        return list(self.balanced_chunks)


def _has_source_group_condition(metadata_condition) -> bool:
    if not metadata_condition:
        return False
    return any(
        condition.get("name") == "source_group"
        for condition in metadata_condition.get("conditions", [])
    )


def _record(
    *,
    document_id="remote-design",
    document_name="design.pdf",
    source_group="设计数据",
) -> PipelineDocumentRecord:
    return PipelineDocumentRecord(
        id=1,
        kb_name="kb",
        document_name=document_name,
        original_file_name=document_name,
        dataset_kind="design",
        dataset_id="dataset-design",
        document_id=document_id,
        source_group=source_group,
        department_id="dept_a",
        uploaded_by="tester",
        status="parsed",
        processor_kind="ragflow",
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


def _ctx():
    return RequestContext(metadata={"department_id": "dept_a"})


def test_balanced_route_omits_source_group_condition():
    chunk = {
        "id": "chunk-1", "document_id": "remote-design",
        "content": "design evidence", "similarity": 0.9,
    }
    # balanced_route=True
    client_on = _Client([[chunk]])
    backend_on = _backend(client_on, [_record()])
    backend_on.retrieve("kb", MATERIAL_QUERY, top_k=5, ctx=_ctx(),
                        filters={"source_names": ["design.pdf"], "balanced_route": True})
    # balanced_route=False
    client_off = _Client([[chunk]])
    backend_off = _backend(client_off, [_record()])
    backend_off.retrieve("kb", MATERIAL_QUERY, top_k=5, ctx=_ctx(),
                         filters={"source_names": ["design.pdf"]})

    on_condition = client_on.calls[0][3]
    off_condition = client_off.calls[0][3]
    assert not _has_source_group_condition(on_condition), (
        "balanced_route must drop the source_group server-side condition"
    )
    assert _has_source_group_condition(off_condition), (
        "without balanced_route the source_group condition must be present"
    )


def test_balanced_route_keeps_source_names_filter():
    design_chunk = {
        "id": "chunk-design", "document_id": "remote-design",
        "content": "design evidence", "similarity": 0.9,
    }
    other_record = _record(
        document_id="remote-other", document_name="other.pdf", source_group="设计数据",
    )
    other_chunk = {
        "id": "chunk-other", "document_id": "remote-other",
        "content": "other evidence", "similarity": 0.8,
    }
    client = _Client([[design_chunk, other_chunk]])
    backend = _backend(client, [_record(), other_record])

    evidence = backend.retrieve("kb", MATERIAL_QUERY, top_k=5, ctx=_ctx(),
                                filters={"source_names": ["design.pdf"], "balanced_route": True})

    # source_names filter is retained: only the frozen design.pdf survives.
    assert [item.id for item in evidence] == ["chunk-design"]


def test_balanced_route_survives_cross_group_chunk():
    # Frozen record is in the design group; the query routes to material.
    # Without balanced_route the server returns only non-frozen material
    # chunks (raw > 0, so no 0-chunk fallback) -> all dropped -> empty.
    # With balanced_route the frozen design chunk comes back.
    material_chunk = {
        "id": "chunk-material", "document_id": "remote-material",
        "content": "material evidence", "similarity": 0.9,
    }
    design_chunk = {
        "id": "chunk-design", "document_id": "remote-design",
        "content": "design evidence", "similarity": 0.85,
    }

    # balanced_route=False: group condition -> material chunks (non-frozen) -> empty
    client_off = _RoutingClient(group_chunks=[material_chunk], balanced_chunks=[design_chunk])
    backend_off = _backend(client_off, [_record()])
    evidence_off = backend_off.retrieve("kb", MATERIAL_QUERY, top_k=5, ctx=_ctx(),
                                        filters={"source_names": ["design.pdf"]})
    assert evidence_off == []
    # Only one retrieve call: raw chunks > 0, so the 0-chunk fallback did not fire.
    assert len(client_off.calls) == 1

    # balanced_route=True: no group condition -> design chunk (frozen) -> hit
    client_on = _RoutingClient(group_chunks=[material_chunk], balanced_chunks=[design_chunk])
    backend_on = _backend(client_on, [_record()])
    evidence_on = backend_on.retrieve("kb", MATERIAL_QUERY, top_k=5, ctx=_ctx(),
                                      filters={"source_names": ["design.pdf"], "balanced_route": True})
    assert [item.id for item in evidence_on] == ["chunk-design"]


def test_balanced_route_metadata_has_empty_source_groups():
    chunk = {
        "id": "chunk-1", "document_id": "remote-design",
        "content": "design evidence", "similarity": 0.9,
    }
    client = _Client([[chunk]])
    backend = _backend(client, [_record()])

    evidence = backend.retrieve("kb", MATERIAL_QUERY, top_k=5, ctx=_ctx(),
                                filters={"source_names": ["design.pdf"], "balanced_route": True})

    # balanced_route zeros routed_source_groups; the route reason/confidence
    # still reflect the computed route, but the hard filter is dropped.
    assert evidence[0].metadata["query_route_source_groups"] == []
