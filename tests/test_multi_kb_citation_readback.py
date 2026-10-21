"""Citation readback resolves stable source IDs and current ACLs."""

from types import SimpleNamespace
from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_user, get_auth_service, get_pipeline
from src.api.routes.files import router
from src.pipelines.document_store import PipelineDocumentStore
from tests import test_knowledge_scope as scope_fixtures
from tests import test_multi_kb_structured_retrieval as structured_fixtures

auth_scope = scope_fixtures.auth_scope
indexes = structured_fixtures.indexes


@pytest.fixture
def citations(tmp_path, auth_scope):
    auth, _, actor, _, ids = auth_scope
    documents = PipelineDocumentStore(db_path=str(tmp_path / "citation.db"))
    records = {}
    for kb in ("A", "B"):
        documents.upsert_document(kb_name=kb, document_name="same.pdf", original_file_name="same.pdf",
            dataset_kind="design", dataset_id="remote", document_id=f"remote-{kb}",
            department_id=str(actor.department_id), uploaded_by=actor.username,
            source_group="design", kb_id=ids[kb], status="parsed", content_hash=f"hash-{kb}")
        records[kb] = documents.list_documents(kb, actor.department_id)[0]

    class Pipeline:
        document_store = documents
        backend = SimpleNamespace(store=documents)

        def get_parse_result(self, kb_name, document_id, ctx=None, *, deadline=None):
            assert document_id == f"ragflow:{records[kb_name].id}"
            assert ctx.metadata["kb_id"] == ids[kb_name]
            return SimpleNamespace(chunks=[
                SimpleNamespace(content=f"{kb_name} wrong chunk", metadata={"ragflow_chunk_id": "other"}),
                SimpleNamespace(content=f"{kb_name} selected chunk", metadata={"ragflow_chunk_id": "shared-chunk"}),
            ])

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_user] = lambda: actor
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_pipeline] = Pipeline
    with TestClient(app) as client:
        yield client, auth_scope, records


def payload(record):
    return {"kb_id": record.kb_id, "local_record_id": record.id,
            "source_content_hash": record.content_hash, "locator": {"chunk_id": "shared-chunk"},
            "index_revision": 0, "processing_artifact_id": "", "processor_version": "",
            "evidence_text_hash": sha256(f"{record.kb_name} selected chunk".encode()).hexdigest()}


def test_same_file_and_chunk_names_read_actual_selected_kb(citations):
    client, _, records = citations
    result = client.post("/api/v1/citations/resolve", json=payload(records["B"]))
    assert result.status_code == 200
    assert result.json()["content"] == "B selected chunk"
    assert result.json()["kb_id"] == records["B"].kb_id
    assert result.json()["local_record_id"] == records["B"].id


def test_citation_readback_uses_backend_store_when_pipeline_has_no_legacy_store_attribute(citations):
    client, _, records = citations
    pipeline = client.app.dependency_overrides[get_pipeline]
    delattr(pipeline, "document_store")

    result = client.post("/api/v1/citations/resolve", json=payload(records["B"]))

    assert result.status_code == 200, result.text
    assert result.json()["content"] == "B selected chunk"


def test_citation_record_cannot_be_substituted_from_another_selected_kb(citations):
    client, _, records = citations
    result = client.post("/api/v1/citations/resolve", json={**payload(records["B"]), "local_record_id": records["A"].id})
    assert result.status_code == 404
    assert "selected chunk" not in result.text


def test_citation_readback_rechecks_revocation_without_returning_cached_text(citations):
    client, auth_scope, records = citations
    assert client.post("/api/v1/citations/resolve", json=payload(records["B"])).status_code == 200
    auth, _, _, owner, _ = auth_scope
    auth.set_knowledge_base_sharing("B", owner, False)
    result = client.post("/api/v1/citations/resolve", json=payload(records["B"]))
    assert result.status_code == 403
    assert "selected chunk" not in result.text


@pytest.mark.parametrize("changes, status", [
    ({"source_content_hash": "stale"}, 409),
    ({"locator": {"chunk_id": "missing"}}, 404),
    ({"kb_id": 999999}, 403),
])
def test_citation_missing_or_changed_source_never_opens_first_chunk(citations, changes, status):
    client, _, records = citations
    result = client.post("/api/v1/citations/resolve", json={**payload(records["B"]), **changes})
    assert result.status_code == status
    assert "selected chunk" not in result.text


@pytest.mark.parametrize("field, stale_value", [
    ("index_revision", 1),
    ("processing_artifact_id", "old-parse"),
    ("processor_version", "old-parser"),
    ("evidence_text_hash", sha256(b"old chunk under the same locator").hexdigest()),
])
def test_citation_rejects_stale_processing_identity_with_unchanged_source_bytes(citations, field, stale_value):
    client, _, records = citations
    result = client.post("/api/v1/citations/resolve", json={**payload(records["B"]), field: stale_value})
    assert result.status_code == 409
    assert result.json()["detail"] == "citation_source_changed"
    assert "selected chunk" not in result.text


@pytest.mark.parametrize("missing", [
    {"index_revision"}, {"processing_artifact_id"}, {"processor_version"}, {"evidence_text_hash"},
    {"index_revision", "processing_artifact_id", "processor_version", "evidence_text_hash"},
])
def test_legacy_citations_without_processing_identity_require_new_retrieval(citations, missing):
    client, _, records = citations
    result = client.post("/api/v1/citations/resolve", json={
        key: value for key, value in payload(records["B"]).items() if key not in missing
    })
    assert result.status_code == 409
    assert result.json()["detail"] == "citation_source_changed"
    assert "selected chunk" not in result.text


def test_citation_rejects_generation_changed_during_content_read(citations, monkeypatch):
    client, _, records = citations
    pipeline = client.app.dependency_overrides[get_pipeline]
    read_record = pipeline.document_store.get_document_by_id_scoped
    read_content = pipeline.get_parse_result
    generation = {"index_revision": 3, "processing_artifact_id": "parse-3", "processor_version": "parser-2"}

    def stamped_record(*args, **kwargs):
        record = read_record(*args, **kwargs)
        if record is not None:
            for key, value in generation.items():
                setattr(record, key, value)
        return record

    def change_generation(self, *args, **kwargs):
        result = read_content(self, *args, **kwargs)
        generation["index_revision"] = 4
        return result

    monkeypatch.setattr(pipeline.document_store, "get_document_by_id_scoped", stamped_record)
    expected = {**payload(records["B"]), **generation}
    assert client.post("/api/v1/citations/resolve", json=expected).status_code == 200
    monkeypatch.setattr(pipeline, "get_parse_result", change_generation)
    result = client.post("/api/v1/citations/resolve", json=expected)
    assert result.status_code == 409
    assert result.json()["detail"] == "citation_source_changed"
    assert "selected chunk" not in result.text


def test_citation_rejects_reused_chunk_locator_with_changed_text(citations, monkeypatch):
    client, _, records = citations
    pipeline = client.app.dependency_overrides[get_pipeline]

    def replaced_chunk(*_args, **_kwargs):
        return SimpleNamespace(chunks=[SimpleNamespace(
            content="new parse content", metadata={"ragflow_chunk_id": "shared-chunk"},
        )])

    monkeypatch.setattr(pipeline, "get_parse_result", replaced_chunk)
    result = client.post("/api/v1/citations/resolve", json=payload(records["B"]))
    assert result.status_code == 409
    assert result.json()["detail"] == "citation_source_changed"
    assert "new parse content" not in result.text


def test_spreadsheet_readback_uses_exact_sheet_row_and_rejects_other_record(indexes):
    from src.services.citation_readback import read_citation_content

    resolver, snapshot, _, spreadsheets, add = indexes
    record = add("A", "5V")
    add("A", "99V", "unselected.xlsx")
    pipeline = SimpleNamespace(spreadsheet_service=spreadsheets)
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])
    content = read_citation_content(pipeline, record, ctx, {"sheet_name": "Power", "row_index": 2})
    assert content == "Component: U1; Voltage: 5V"
    assert read_citation_content(pipeline, record, ctx, {"sheet_name": "other", "row_index": 2}) is None
    assert read_citation_content(pipeline, record, ctx, {"sheet_name": "Power", "row_index": 3}) is None


@pytest.mark.parametrize("status, error_code, http_status, detail", [
    ("access_denied", None, 403, "citation_access_denied"),
    ("source_changed", None, 409, "citation_source_changed"),
    ("filter_unsupported", "citation_processor_unsupported", 422, "citation_processor_unsupported"),
    ("timeout", "citation_readback_timeout", 504, "citation_readback_timeout"),
    ("source_unavailable", "citation_readback_incomplete", 502, "citation_readback_incomplete"),
    ("source_unavailable", "citation_source_unavailable", 503, "citation_source_unavailable"),
    ("cancelled", "cancelled", 503, "citation_source_unavailable"),
])
def test_citation_read_failures_map_to_explicit_http_contract(citations, monkeypatch, status, error_code, http_status, detail):
    from src.pipelines.document_rag.schemas import ScopedRetrievalError

    client, _, records = citations
    pipeline = client.app.dependency_overrides[get_pipeline]

    def fail(*_args, **_kwargs):
        raise ScopedRetrievalError(status, error_code)

    monkeypatch.setattr(pipeline, "get_parse_result", fail)
    result = client.post("/api/v1/citations/resolve", json=payload(records["B"]))
    assert result.status_code == http_status, result.text
    assert result.json()["detail"] == detail
    assert "selected chunk" not in result.text


def test_preview_read_failures_are_not_reported_as_missing_chunks(citations, monkeypatch):
    from src.pipelines.document_rag.schemas import ScopedRetrievalError

    client, _, records = citations
    pipeline = client.app.dependency_overrides[get_pipeline]
    record = records["A"]

    def fail(*_args, **_kwargs):
        raise ScopedRetrievalError("source_unavailable", "citation_readback_incomplete")

    monkeypatch.setattr(pipeline, "get_parse_result", fail)
    monkeypatch.setattr("src.api.routes.files.build_context_for_user", lambda *_a, **_k: _preview_ctx(record))
    result = client.get(f"/api/v1/kbs/A/files/ragflow:{record.id}/chunks")
    assert result.status_code == 502
    assert result.json()["detail"] == "citation_readback_incomplete"


def test_preview_unsupported_processor_is_not_reported_as_missing(citations, monkeypatch):
    from src.pipelines.document_rag.schemas import ScopedRetrievalError

    client, _, records = citations
    pipeline = client.app.dependency_overrides[get_pipeline]
    record = records["A"]

    def fail(*_args, **_kwargs):
        raise ScopedRetrievalError("filter_unsupported", "citation_processor_unsupported")

    monkeypatch.setattr(pipeline, "get_parse_result", fail)
    monkeypatch.setattr("src.api.routes.files.build_context_for_user", lambda *_a, **_k: _preview_ctx(record))
    result = client.get(f"/api/v1/kbs/A/files/ragflow:{record.id}/chunks")
    assert result.status_code == 422
    assert result.json()["detail"] == "citation_processor_unsupported"


def _preview_ctx(record):
    from src.pipelines.document_rag.schemas import RequestContext

    department_id = str(record.department_id)
    return RequestContext(
        user_id="reader", roles=[], allowed_kbs=["A"],
        kb_permissions={f"{department_id}:A": "admin"},
        metadata={"resource_department_id": department_id, "department_id": department_id,
                  "kb_id": record.kb_id},
    )
