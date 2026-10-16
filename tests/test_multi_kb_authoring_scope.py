"""R3 T8/T9: authoring adapters retain the frozen multi-source boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from src.agents.schemas import Evidence
from src.agents.claim_evidence import InformationRequirement
from src.core.app_pipeline import AppPipeline
from src.pipelines.document_rag.schemas import RequestContext
from src.document_authoring.evidence import KnowledgeBaseEvidenceProvider
from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from src.services.knowledge_scope_models import AuthorizedKnowledgeSource
from tests.test_multi_kb_store_contract import snapshot

pytest_plugins = ("tests.test_knowledge_scope",)


def test_generation_session_keeps_selection_across_requests_and_rechecks_acl(auth_scope, tmp_path, monkeypatch):
    from src.document_authoring.generation_sessions import GenerationSessionStore
    from src.services.knowledge_scope import KnowledgeScopeResolver
    from src.services.knowledge_scope_models import KnowledgeSelectionInput
    import pytest

    auth, _admin, actor, owner, ids = auth_scope
    resolver = KnowledgeScopeResolver(auth)
    selected = resolver.resolve(actor, "A", KnowledgeSelectionInput.model_validate({
        "purpose": "compose", "sources": [{"kb_id": ids["A"], "role": "primary"}, {"kb_id": ids["B"], "role": "reference"}],
    }))
    store = GenerationSessionStore(str(tmp_path / "sessions.db"))
    assert "knowledge_scope_snapshot" in __import__("inspect").signature(store.create_session).parameters
    saved = store.create_session(tenant_id="default", user_id=actor.username, knowledge_base_name="A", template_version_id=None,
                                 contract_version="output_spec_v1", knowledge_scope_snapshot=selected.model_dump(mode="json"))
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(store=SimpleNamespace(generation_sessions=store))
    monkeypatch.setattr("src.core.app_pipeline.AuthService", lambda: auth)
    ctx = RequestContext(user_id=actor.username, tenant_id="default", metadata={"document_template_kb_name": "A"})
    pipeline.get_document_generation_session(ctx, saved.session_id)
    assert ctx.metadata["knowledge_scope_snapshot"]["selection_hash"] == selected.selection_hash
    auth.set_knowledge_base_sharing("B", owner, False)
    with pytest.raises(PermissionError):
        pipeline.get_document_generation_session(ctx, saved.session_id)
def test_source_snapshot_can_persist_multi_source_roles_and_versions():
    result = KnowledgeBaseSourceSnapshot.create(
        tenant_id="default",
        knowledge_base_name="KB101",
        source_names=["spec.pdf", "standard.pdf"],
        created_by="1",
        source_snapshot_kind="multi_knowledge_v1",
        sources=[
            {"kb_id": 101, "kb_name": "KB101", "role": "primary", "source_name": "spec.pdf", "local_record_id": 7, "source_content_hash": "a" * 64, "index_revision": 2},
            {"kb_id": 205, "kb_name": "KB205", "role": "standard", "source_name": "standard.pdf", "local_record_id": 9, "source_content_hash": "b" * 64, "index_revision": 3},
        ],
    )
    assert result.source_snapshot_kind == "multi_knowledge_v1"
    assert {item["kb_id"] for item in result.sources} == {101, 205}
    assert len(result.content_hash) == 64


def test_authoring_dedup_and_field_cap_keep_independent_source_occurrences():
    from src.document_authoring.retriever_registry import dedup_by_content
    from src.document_authoring.harness.graph import _select_field_evidence

    rows = [Evidence(id=f"e-{kb}-{rank}", content="输入电压5V" if rank == 1 else "共用文本",
                     source_name="same.pdf", content_kind="document_text", processor_kind="ragflow", score=10-rank,
                     metadata={"kb_id": kb, "local_record_id": rank, "knowledge_source": {"kb_id": kb, "role": "primary" if kb == 1 else "reference"}})
            for kb, rank in [(1, 1), (1, 2), (1, 3), (2, 1)]]
    assert len(dedup_by_content(rows)) == 4
    selected, _ = _select_field_evidence([row.model_dump() for row in rows], 2, preserve_rerank_order=True)
    assert {row["metadata"]["kb_id"] for row in selected} == {1, 2}


def test_authoring_kb_provider_delegates_to_scoped_multi_service():
    scope = snapshot(1)
    calls = []

    class Service:
        def search(self, **kwargs):
            calls.append(kwargs)
            return type("Outcome", (), {
                "evidence": [
                    Evidence(
                        id="kb205:chunk",
                        content="reference evidence",
                        source_name="standard.pdf",
                        content_kind="document_text",
                        processor_kind="ragflow",
                        metadata={
                            "kb_id": 205,
                            "knowledge_source": scope.sources[1].model_dump(mode="json"),
                        },
                    )
                ]
            })()

    provider = KnowledgeBaseEvidenceProvider(
        object(),
        top_k=4,
        multi_kb_service=Service(),
        knowledge_scope=scope,
        retrieval_budget=object(),
    )
    rows = provider.retrieve(
        query="voltage",
        source_scope="knowledge_base_only",
        attachment_ids=[],
        kb_name="KB101",
        ctx=type("Context", (), {"has_kb_permission": lambda *_: True})(),
    )
    assert calls and calls[0]["snapshot"] is scope
    assert rows[0].metadata["knowledge_base_name"] == "KB205"


def test_authoring_kb_provider_fuses_scoped_candidates_before_returning_rows():
    from hashlib import sha256
    from src.pipelines.document_rag.schemas import Evidence as ScopedEvidence

    scope = snapshot(1)

    def item(source, content, rank):
        return ScopedEvidence(
            id=f"chunk-{source.kb_id}-{rank}", content=content,
            source_name=f"{source.kb_name}.pdf", score=1.0 / rank,
            metadata={
                "tenant_key": scope.tenant_key,
                "department_id": source.department_id,
                "kb_id": source.kb_id,
                "knowledge_source": source.model_dump(mode="json"),
                "processor_kind": "ragflow", "local_record_id": source.kb_id,
                "source_content_hash": f"{source.kb_id:064x}",
                "processing_artifact_id": "artifact-1", "index_revision": 1,
                "remote_document_id": f"remote-{source.kb_id}",
                "chunk_or_structured_locator": f"chunk-{source.kb_id}-{rank}",
                "evidence_text_hash": sha256(content.encode()).hexdigest(),
            },
        )

    class Service:
        def search(self, **_kwargs):
            a, b = scope.sources
            evidence = [item(a, "A primary", 1), item(a, "A primary", 1), item(b, "B reference", 1)]
            outcomes = [
                SimpleNamespace(kb_id=a.kb_id, query="voltage", retriever_kind="document", status="success_with_hits", evidence=evidence[:2], error_code=None, diagnostics={}),
                SimpleNamespace(kb_id=b.kb_id, query="voltage", retriever_kind="document", status="success_with_hits", evidence=evidence[2:], error_code=None, diagnostics={}),
            ]
            return SimpleNamespace(evidence=evidence, source_outcomes=outcomes, selection_hash=scope.selection_hash)

    provider = KnowledgeBaseEvidenceProvider(
        object(), top_k=4, multi_kb_service=Service(), knowledge_scope=scope,
        retrieval_budget=object(),
    )
    rows = provider.retrieve(
        query="voltage", source_scope="knowledge_base_only", attachment_ids=[],
        kb_name="KB101", ctx=type("Context", (), {"has_kb_permission": lambda *_: True})(),
    )
    assert {row.metadata["kb_id"] for row in rows} == {101, 205}
    assert len(rows) == 2


def test_multi_kb_authoring_does_not_mount_anchor_only_structured_retrievers(monkeypatch):
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.spreadsheet_service = Mock()
    pipeline.circuit_service = Mock()
    pipeline.document_generation = Mock()
    pipeline.document_generation.build_knowledge_base_retrieval_outcome.return_value = SimpleNamespace(
        status="success_empty"
    )
    ctx = RequestContext(
        user_id="alice",
        metadata={
            "department_id": "7",
            "knowledge_scope_snapshot": {"version": 1, "sources": [{"kb_id": 101}]},
        },
        kb_permissions={"7:KB101": "read"},
    )
    monkeypatch.setattr(
        "src.core.app_pipeline.KnowledgeBaseEvidenceProvider.retrieve",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "src.core.app_pipeline.SpreadsheetSemanticTool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("multi-KB authoring must not use anchor-only spreadsheet retrieval")
        ),
    )
    monkeypatch.setattr(
        "src.core.app_pipeline.CircuitQueryTool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("multi-KB authoring must not use anchor-only circuit retrieval")
        ),
    )

    retrieve = pipeline._knowledge_base_retriever(ctx, "KB101", ["spec.pdf"])
    result = retrieve(
        InformationRequirement(
            requirement_id="r1",
            semantic_unit_id="u1",
            claim_type="attribute",
            subject="voltage",
            required_capabilities=["tabular_lookup", "relationship_lookup"],
        ),
        0,
    )

    assert result.status == "success_empty"


def test_frozen_source_entry_keeps_record_identity_and_processing_stamps():
    from src.document_authoring.knowledge_sources import build_frozen_source_entry

    source = AuthorizedKnowledgeSource(
        kb_id=205,
        kb_name="KB205",
        department_id="7",
        role="standard",
    )
    document = SimpleNamespace(
        id="pipeline:9",
        name="standard.xlsx",
        processor_kind="spreadsheet_table",
        metadata={
            "store_id": 9,
            "dataset_id": "dataset-205",
            "table_document_id": "table-9",
            "content_hash": "b" * 64,
            "source_version_id": "source-v3",
            "revision": "r7",
            "approval_status": "approved",
            "processing_artifact_id": "artifact-2",
            "processor_version": "xlsx-parser-4",
            "index_revision": 12,
            "source_group": "requirements",
            "locator": {"sheet_name": "BOM", "row_index": 8},
        },
    )

    entry = build_frozen_source_entry(
        tenant_key="default",
        source=source,
        document=document,
    )

    assert entry == {
        "tenant_key": "default",
        "department_id": "7",
        "kb_id": 205,
        "kb_name_snapshot": "KB205",
        "role": "standard",
        "local_record_id": 9,
        "processor_kind": "spreadsheet_table",
        "source_group": "requirements",
        "source_name": "standard.xlsx",
        "source_content_hash": "b" * 64,
        "source_version_id": "source-v3",
        "revision": "r7",
        "approval_status": "approved",
        "processing_artifact_id": "artifact-2",
        "processor_version": "xlsx-parser-4",
        "index_revision": 12,
        "dataset_id": "dataset-205",
        "remote_document_id": "table-9",
        "locator": {"sheet_name": "BOM", "row_index": 8},
    }
