"""R3 T4: preserve scoped outcomes and prohibit frozen broad fallback."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

import src.settings as settings
from src.pipelines.document_rag import schemas
from src.pipelines.document_rag.ragflow_backend import RAGFlowAPIError
from tests.test_ragflow_document_scope import _Client, _backend, _chunk, _record


def record():
    return replace(_record(), kb_id=101, content_hash=hashlib.sha256(b"file bytes").hexdigest())


def context():
    return schemas.RequestContext(user_id="tester", tenant_id="default", kb_permissions={"dept_a:kb": "read"}, metadata={"kb_id": 101, "department_id": "dept_a"})


def contract(source=None, *, mode="chat", ids=None):
    stamps = {}
    if source is not None:
        ids = (source.id,)
        stamps[source.id] = schemas.DocumentSourceStamp(
            kb_id=101, local_record_id=source.id, source_content_hash=source.content_hash,
            source_version_id=source.source_version_id, revision=source.revision,
            processing_artifact_id=getattr(source, "processing_artifact_id", None),
            processor_version=getattr(source, "processor_version", None),
            remote_document_id=source.document_id, index_revision=0,
        )
    return schemas.DocumentRetrievalContract(mode=mode, allowed_record_ids=ids, expected_source_stamps=stamps)


def test_chat_evidence_overwrites_remote_identity_with_verified_local_identity():
    source = record()
    raw = {**_chunk(), "metadata": {"kb_id": 999, "source_content_hash": "forged", "local_record_id": 999}}
    backend = _backend(_Client([[raw]]), [source])
    outcome = backend.retrieve_with_scope("kb", "design question", contract=contract(), top_k=8, ctx=context())
    assert outcome.status == "success_with_hits"
    evidence = outcome.evidence[0]
    assert evidence.metadata["kb_id"] == 101
    assert evidence.metadata["local_record_id"] == source.id
    assert evidence.metadata["source_content_hash"] == hashlib.sha256(b"file bytes").hexdigest()
    assert evidence.metadata["evidence_text_hash"] == hashlib.sha256(b"retrieved design evidence").hexdigest()
    assert evidence.metadata["remote_document_id"] == source.document_id
    assert evidence.metadata["chunk_or_structured_locator"] == "chunk-1"


@pytest.mark.parametrize("ids,status", [(None, "success_empty"), ((), "success_empty"), ((999,), "source_unavailable")])
def test_valid_empty_scope_is_distinct_from_missing_explicit_records(ids, status):
    client = _Client([[], []])
    backend = _backend(client, [record()])
    result = backend.retrieve_with_scope("kb", "design question", contract=contract(ids=ids), top_k=8, ctx=context())
    assert result.status == status
    assert not result.evidence
    if ids is not None:
        assert client.calls == []


def test_frozen_document_scope_rejection_is_one_call_without_broad_retry():
    source = record()
    rejection = RAGFlowAPIError({"code": 102, "message": "document_ids not supported"})
    client = _Client([rejection, [_chunk()]])
    result = _backend(client, [source]).retrieve_with_scope("kb", "design question", contract=contract(source, mode="frozen"), top_k=8, ctx=context())
    assert result.status == "filter_unsupported"
    assert not result.evidence
    assert len(client.calls) == 1
    assert client.calls[0]["document_ids"] == [source.document_id]


def test_chat_rejection_falls_back_with_local_record_whitelist():
    source = record()
    rejection = RAGFlowAPIError({"code": 102, "message": "document_ids not supported"})
    client = _Client([rejection, [_chunk(), _chunk("other-kb-secret")]])
    result = _backend(client, [source]).retrieve_with_scope("kb", "design question", contract=contract(), top_k=8, ctx=context())
    assert result.status == "success_with_hits"
    assert len(result.evidence) == 1
    assert len(client.calls) == 2
    assert client.calls[1]["document_ids"] is None
    assert result.diagnostics["remote_scope_fallback"] is True
    assert result.diagnostics["discarded_chunks"] == 1


def test_changed_source_stamp_is_not_empty_and_never_queries_backend():
    source = record()
    client = _Client([[_chunk()]])
    frozen = contract(source, mode="frozen")
    changed = replace(source, content_hash=hashlib.sha256(b"new file bytes").hexdigest())
    result = _backend(client, [changed]).retrieve_with_scope("kb", "question", contract=frozen, top_k=8, ctx=context())
    assert result.status == "source_changed"
    assert not client.calls


def test_source_is_rechecked_after_remote_call():
    source = record()

    class Client(_Client):
        def retrieve(self, *args, **kwargs):
            result = super().retrieve(*args, **kwargs)
            backend.store.records = [replace(source, content_hash=hashlib.sha256(b"replacement").hexdigest())]
            return result

    backend = _backend(Client([[_chunk()]]), [source])
    result = backend.retrieve_with_scope("kb", "question", contract=contract(source, mode="frozen"), top_k=8, ctx=context())
    assert result.status == "source_changed"
    assert result.evidence == []


def test_denied_scope_returns_access_denied_instead_of_empty():
    backend = _backend(_Client([]), [record()])

    def deny(*_args):
        raise PermissionError("denied")

    backend._check_kb_access = deny
    result = backend.retrieve_with_scope("kb", "question", contract=contract(), top_k=8, ctx=context())
    assert result.status == "access_denied"


def test_frozen_scope_with_unexpected_chunks_is_unsupported():
    source = record()
    result = _backend(_Client([[_chunk(), _chunk("unselected")]]), [source]).retrieve_with_scope("kb", "question", contract=contract(source, mode="frozen"), top_k=8, ctx=context())
    assert result.status == "filter_unsupported"
    assert not result.evidence


def test_frozen_scope_requires_complete_stamps_even_if_global_prefilter_is_off(monkeypatch):
    source = record()
    monkeypatch.setattr(settings, "RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED", False)
    client = _Client([[_chunk()]])
    result = _backend(client, [source]).retrieve_with_scope("kb", "question", contract=contract(source, mode="frozen"), top_k=8, ctx=context())
    assert result.status == "success_with_hits"
    assert client.calls[0]["document_ids"] == [source.document_id]
    incomplete = schemas.DocumentRetrievalContract(mode="frozen", allowed_record_ids=(source.id,), expected_source_stamps={})
    invalid = _backend(_Client([]), [source]).retrieve_with_scope("kb", "question", contract=incomplete, top_k=8, ctx=context())
    assert invalid.status == "source_unavailable"


def test_frozen_dataset_mapping_drift_is_unavailable_instead_of_empty():
    source = replace(record(), dataset_id="no-longer-mapped")
    client = _Client([])
    result = _backend(client, [source]).retrieve_with_scope("kb", "question", contract=contract(source, mode="frozen"), top_k=8, ctx=context())
    assert result.status == "source_unavailable"
    assert client.calls == []


def test_actual_http_fallbacks_share_budget_and_cap_request_timeout(monkeypatch):
    import json
    import httpx
    from src.pipelines.document_rag.ragflow_backend import RAGFlowClient
    from src.services.multi_kb_retrieval import RetrievalBudget

    monkeypatch.setattr(settings, "RESOURCE_GOVERNOR_ENABLED", False)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"code": 102, "message": "The datasets don't own the document remote-design"})
        return httpx.Response(200, json={"code": 0, "data": {"chunks": []}})

    client = RAGFlowClient(base_url="http://unit-test", api_key="unit-test", timeout=300)
    client._sync_client.close()
    client._sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        budget = RetrievalBudget(max_calls=2, timeout_seconds=1)
        outcome = _backend(client, [record()]).retrieve_with_scope("kb", "design question", contract=contract(), top_k=8, ctx=context(),
            before_request=budget.before_attempt, remaining_seconds=budget.remaining_seconds)
        assert outcome.status == "budget_exhausted"
        assert budget.calls_used == len(requests) == 2
        assert "document_ids" in json.loads(requests[0].content)
        assert "document_ids" not in json.loads(requests[1].content)
        assert 0 < requests[0].extensions["timeout"]["read"] <= 1
    finally:
        client.close()
