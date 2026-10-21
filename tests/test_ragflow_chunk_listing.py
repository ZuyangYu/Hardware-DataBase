from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from src.pipelines.document_rag.ragflow_backend import RAGFlowAPIError, RAGFlowBackend, RAGFlowClient
from src.pipelines.document_rag.schemas import ScopedRetrievalError


def _client(handler):
    client = RAGFlowClient(base_url="http://ragflow.test", api_key="test", timeout=2)
    client._sync_client.close()
    client._sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_list_chunks_reads_all_pages_and_deduplicates_chunk_ids():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            payload = {"chunks": [{"id": "a"}, {"id": "b"}], "total": 3}
        else:
            payload = {"chunks": [{"id": "b"}, {"id": "c"}], "total": 3}
        return httpx.Response(200, json={"code": 0, "data": payload})

    client = _client(handler)
    try:
        chunks = client.list_chunks("dataset", "document")
    finally:
        client.close()

    assert [chunk["id"] for chunk in chunks] == ["a", "b", "c"]
    assert [params["page"] for params in calls] == ["1", "2"]
    assert all(params["page_size"] == "30" for params in calls)
    assert client.request_count == 2


def test_list_chunks_propagates_later_page_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json={"code": 0, "data": {"chunks": [{"id": "a"}], "total": 2}})
        return httpx.Response(503, json={"code": 100, "message": "temporarily unavailable"})

    client = _client(handler)
    try:
        with pytest.raises((httpx.HTTPStatusError, RAGFlowAPIError, ScopedRetrievalError)):
            client.list_chunks("dataset", "document")
    finally:
        client.close()


def test_list_chunks_reads_target_from_third_and_fourth_pages():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        calls.append(page)
        chunks = [[{"id": "a"}, {"id": "b"}], [{"id": "c"}, {"id": "d"}],
                  [{"id": "e"}, {"id": "target"}], [{"id": "tail"}]][page - 1]
        return httpx.Response(200, json={"code": 0, "data": {"chunks": chunks, "total": 7}})

    client = _client(handler)
    try:
        chunks = client.list_chunks("dataset", "document", page_size=2)
    finally:
        client.close()

    assert [chunk["id"] for chunk in chunks] == ["a", "b", "c", "d", "e", "target", "tail"]
    assert calls == [1, 2, 3, 4]


def test_list_chunks_stops_on_short_tail_without_total():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        chunks = [{"id": "a"}, {"id": "b"}] if page == 1 else [{"id": "c"}]
        return httpx.Response(200, json={"code": 0, "data": {"chunks": chunks}})

    client = _client(handler)
    try:
        assert [chunk["id"] for chunk in client.list_chunks("dataset", "document", page_size=2)] == ["a", "b", "c"]
    finally:
        client.close()


def test_list_chunks_rejects_repeated_page_instead_of_returning_partial_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"chunks": [{"id": "a"}], "total": 5}})

    client = _client(handler)
    try:
        with pytest.raises(ScopedRetrievalError) as exc:
            client.list_chunks("dataset", "document")
    finally:
        client.close()
    assert exc.value.error_code == "citation_readback_incomplete"


def test_list_chunks_page_limit_fails_instead_of_returning_prefix():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"code": 0, "data": {"chunks": [{"id": f"c{page}"}], "total": 100}})

    client = _client(handler)
    try:
        with pytest.raises(ScopedRetrievalError) as exc:
            client.list_chunks("dataset", "document", page_size=1, max_pages=2)
    finally:
        client.close()
    assert exc.value.error_code == "citation_readback_incomplete"


def test_list_chunks_byte_limit_fails_instead_of_returning_partial_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"chunks": [{"id": "a", "content": "x" * 100}], "total": 1}})

    client = _client(handler)
    try:
        with pytest.raises(ScopedRetrievalError) as exc:
            client.list_chunks("dataset", "document", max_bytes=10)
    finally:
        client.close()
    assert exc.value.error_code == "citation_readback_incomplete"


def test_list_chunks_reports_deadline_and_cancellation():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"chunks": [{"id": "a"}], "total": 1}})

    client = _client(handler)
    try:
        with pytest.raises(ScopedRetrievalError) as expired:
            client.list_chunks("dataset", "document", deadline=time.monotonic() - 1)
        with pytest.raises(ScopedRetrievalError) as cancelled:
            client.list_chunks("dataset", "document", should_cancel=lambda: True)
    finally:
        client.close()
    assert expired.value.status == "timeout"
    assert cancelled.value.status == "cancelled"


def _parse_backend(record, client, monkeypatch):
    backend = object.__new__(RAGFlowBackend)
    backend._check_kb_access = lambda *_args, **_kwargs: None
    backend._get_record_for_document_id = lambda *_args, **_kwargs: record
    backend._client = lambda: client
    monkeypatch.setattr("src.pipelines.document_rag.ragflow_backend._check_record_department", lambda *_args: None)
    return backend


def test_get_parse_result_only_returns_none_when_record_is_missing(monkeypatch):
    client = SimpleNamespace(list_chunks=lambda *_a, **_k: [])
    backend = _parse_backend(None, client, monkeypatch)
    assert backend.get_parse_result("A", "ragflow:1") is None


def test_get_parse_result_rejects_unsupported_processor_instead_of_missing(monkeypatch):
    record = SimpleNamespace(processor_kind="spreadsheet_table")
    backend = _parse_backend(record, SimpleNamespace(), monkeypatch)
    with pytest.raises(ScopedRetrievalError) as exc:
        backend.get_parse_result("A", "ragflow:1")
    assert exc.value.status == "filter_unsupported"
    assert exc.value.error_code == "citation_processor_unsupported"


def test_get_parse_result_propagates_incomplete_listing(monkeypatch):
    record = SimpleNamespace(processor_kind="ragflow", dataset_id="d", document_id="x")
    client = SimpleNamespace(list_chunks=lambda *_a, **_k: (_ for _ in ()).throw(
        ScopedRetrievalError("source_unavailable", "citation_readback_incomplete")))
    backend = _parse_backend(record, client, monkeypatch)
    with pytest.raises(ScopedRetrievalError) as exc:
        backend.get_parse_result("A", "ragflow:1")
    assert exc.value.error_code == "citation_readback_incomplete"


def test_get_parse_result_maps_transport_failure_to_unavailable(monkeypatch):
    record = SimpleNamespace(processor_kind="ragflow", dataset_id="d", document_id="x")
    client = SimpleNamespace(list_chunks=lambda *_a, **_k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    backend = _parse_backend(record, client, monkeypatch)
    with pytest.raises(ScopedRetrievalError) as exc:
        backend.get_parse_result("A", "ragflow:1")
    assert exc.value.status == "source_unavailable"
    assert exc.value.error_code == "citation_source_unavailable"



