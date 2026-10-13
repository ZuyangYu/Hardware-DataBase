"""RAGFlow document_ids 预过滤（老版本兼容）的单元测试。

背景：RAGFlow <0.21 会静默忽略检索的 metadata_condition。检索链路改为把本地
台账翻译成 document_ids 预过滤下发（0.19 起支持），这里覆盖作用域构造、
漂移降级、开关与上限，以及客户端请求体。
"""
from __future__ import annotations

import json
from unittest import mock

import httpx

import src.settings as settings
from src.pipelines.document_rag.ragflow_backend import (
    RAGFlowAPIError,
    RAGFlowBackend,
    RAGFlowClient,
)
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
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def retrieve(
        self,
        question: str,
        dataset_ids: list[str],
        top_k: int,
        metadata_condition=None,
        document_ids=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "question": question,
                "dataset_ids": dataset_ids,
                "top_k": top_k,
                "metadata_condition": metadata_condition,
                "document_ids": document_ids,
            }
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _record(
    record_id: int = 1,
    *,
    document_id: str = "remote-design",
    name: str = "design.pdf",
    source_group: str = "design",
    dataset_id: str = "dataset-design",
) -> PipelineDocumentRecord:
    return PipelineDocumentRecord(
        id=record_id,
        kb_name="kb",
        document_name=name,
        original_file_name=name,
        dataset_kind="design",
        dataset_id=dataset_id,
        document_id=document_id,
        source_group=source_group,
        department_id="dept_a",
        uploaded_by="tester",
        status="parsed",
        processor_kind="ragflow",
    )


def _backend(client: _Client, records: list[PipelineDocumentRecord]) -> RAGFlowBackend:
    backend = object.__new__(RAGFlowBackend)
    backend.name = "ragflow"
    backend._client_instance = client
    backend.store = _Store(records)
    backend._dataset_ids = {"governance": "dataset-g", "design": "dataset-design"}
    backend._ensure_physical_datasets = lambda: None
    backend._check_kb_access = lambda kb_name, ctx, required="read": None
    return backend


def _chunk(document_id: str = "remote-design") -> dict:
    return {
        "id": "chunk-1",
        "document_id": document_id,
        "content": "retrieved design evidence",
        "similarity": 0.9,
    }


def test_retrieve_scopes_remote_search_to_local_document_ids():
    client = _Client([[_chunk()]])
    backend = _backend(client, [_record()])

    evidence = backend.retrieve(
        "kb",
        "design question",
        top_k=5,
        ctx=RequestContext(metadata={"department_id": "dept_a"}),
    )

    assert [item.id for item in evidence] == ["chunk-1"]
    assert client.calls[0]["document_ids"] == ["remote-design"]


def test_retrieve_scope_ids_follow_source_name_filter():
    records = [
        _record(1, document_id="remote-design", name="design.pdf"),
        _record(2, document_id="remote-notes", name="notes.pdf"),
    ]
    client = _Client([[_chunk("remote-notes")]])
    backend = _backend(client, records)

    backend.retrieve(
        "kb",
        "design question",
        top_k=5,
        ctx=RequestContext(metadata={"department_id": "dept_a"}),
        filters={"source_name": "notes.pdf"},
    )

    assert client.calls[0]["document_ids"] == ["remote-notes"]


def test_retrieve_retries_without_scope_ids_when_remote_rejects_them():
    rejection = RAGFlowAPIError(
        {"code": 102, "message": "The datasets don't own the document remote-design"}
    )
    client = _Client([rejection, [_chunk()]])
    backend = _backend(client, [_record()])

    evidence = backend.retrieve(
        "kb",
        "design question",
        top_k=5,
        ctx=RequestContext(metadata={"department_id": "dept_a"}),
    )

    assert [item.id for item in evidence] == ["chunk-1"]
    assert client.calls[0]["document_ids"] == ["remote-design"]
    assert client.calls[1]["document_ids"] is None


def test_retrieve_scope_disabled_by_switch():
    client = _Client([[_chunk()]])
    backend = _backend(client, [_record()])

    with mock.patch.object(settings, "RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED", False):
        backend.retrieve(
            "kb",
            "design question",
            top_k=5,
            ctx=RequestContext(metadata={"department_id": "dept_a"}),
        )

    assert client.calls[0]["document_ids"] is None


def test_retrieve_scope_falls_back_when_over_cap():
    records = [
        _record(1, document_id="remote-1", name="a.pdf"),
        _record(2, document_id="remote-2", name="b.pdf"),
    ]
    client = _Client([[_chunk("remote-1")]])
    backend = _backend(client, records)

    with mock.patch.object(settings, "RAGFLOW_RETRIEVAL_DOC_IDS_MAX", 1):
        backend.retrieve(
            "kb",
            "design question",
            top_k=5,
            ctx=RequestContext(metadata={"department_id": "dept_a"}),
        )

    assert client.calls[0]["document_ids"] is None


def _client_with_handler(handler) -> RAGFlowClient:
    client = RAGFlowClient(base_url="http://ragflow.test", api_key="test-key", timeout=5)
    client._sync_client.close()
    client._sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_client_retrieve_includes_document_ids_in_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "data": {"chunks": [_chunk()]}})

    client = _client_with_handler(handler)
    chunks = client.retrieve(
        "q",
        dataset_ids=["dataset-design"],
        top_k=3,
        document_ids=["remote-design"],
    )

    assert [item["id"] for item in chunks] == ["chunk-1"]
    assert captured["payload"]["document_ids"] == ["remote-design"]
    assert captured["payload"]["dataset_ids"] == ["dataset-design"]
