from __future__ import annotations

from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.pipelines.document_store import PipelineDocumentRecord


class _Store:
    def __init__(self, records: list[PipelineDocumentRecord]):
        self.records = records

    def list_documents(self, kb_name: str, department_id: str | None = None):
        return [
            record
            for record in self.records
            if record.kb_name == kb_name and (department_id is None or record.department_id == department_id)
        ]


class _Client:
    def __init__(self, responses: list[list[dict]]):
        self.responses = list(responses)
        self.calls: list[tuple[str, list[str], int, dict | None]] = []

    def retrieve(self, question: str, dataset_ids: list[str], top_k: int, metadata_condition=None):
        self.calls.append((question, dataset_ids, top_k, metadata_condition))
        return self.responses.pop(0)


def _record() -> PipelineDocumentRecord:
    return PipelineDocumentRecord(
        id=1,
        kb_name="kb",
        document_name="design.pdf",
        original_file_name="design.pdf",
        dataset_kind="design",
        dataset_id="dataset-design",
        document_id="remote-design",
        source_group="design",
        department_id="dept_a",
        uploaded_by="tester",
        status="parsed",
        processor_kind="ragflow",
    )


def _backend(client: _Client) -> RAGFlowBackend:
    backend = object.__new__(RAGFlowBackend)
    backend.name = "ragflow"
    backend.client = client
    backend.store = _Store([_record()])
    backend._dataset_ids = {"governance": "dataset-g", "design": "dataset-d"}
    backend._ensure_physical_datasets = lambda: None
    backend._check_kb_access = lambda kb_name, ctx, required="read": None
    return backend


def test_retrieve_retries_without_server_metadata_when_chunks_omit_metadata():
    chunk = {
        "id": "chunk-1",
        "document_id": "remote-design",
        "content": "retrieved design evidence",
        "similarity": 0.9,
    }
    client = _Client([[], [chunk]])
    backend = _backend(client)

    evidence = backend.retrieve(
        "kb",
        "design question",
        top_k=5,
        ctx=RequestContext(metadata={"department_id": "dept_a"}),
        filters={"source_name": "design.pdf"},
    )

    assert [item.id for item in evidence] == ["chunk-1"]
    assert client.calls[0][3] is not None
    assert client.calls[1][3] is None
    assert evidence[0].metadata["ragflow_source_name_fallback"] is True


def test_retrieve_deduplicates_physical_dataset_ids():
    chunk = {
        "id": "chunk-1",
        "document_id": "remote-design",
        "content": "retrieved design evidence",
        "similarity": 0.9,
    }
    client = _Client([[chunk]])
    backend = _backend(client)
    backend._dataset_ids = {"governance": "dataset-shared", "design": "dataset-shared"}

    backend.retrieve(
        "kb",
        "design question",
        top_k=5,
        ctx=RequestContext(metadata={"department_id": "dept_a"}),
    )

    assert client.calls[0][1] == ["dataset-shared"]


def test_retrieve_honors_explicit_source_name_over_conflicting_source_group_route():
    chunk = {
        "id": "chunk-1",
        "document_id": "remote-design",
        "content": "retrieved design evidence",
        "similarity": 0.9,
    }
    client = _Client([[], [chunk]])
    backend = _backend(client)

    evidence = backend.retrieve(
        "kb",
        "\u6587\u6863 \u624b\u518c design question",
        top_k=5,
        ctx=RequestContext(metadata={"department_id": "dept_a"}),
        filters={"source_name": "design.pdf"},
    )

    assert [item.id for item in evidence] == ["chunk-1"]
    assert evidence[0].metadata["ragflow_metadata_condition_fallback"] is True
