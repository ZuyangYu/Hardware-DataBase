from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import src.document_authoring as document_authoring
from src.pipelines.document_rag.schemas import RequestContext


@dataclass
class _Chunk:
    asset_id: str
    part_id: str
    ordinal: int
    part_type: str
    text_content: str
    locator: dict
    metadata: dict
    score: float = 0.9
    backend: str = "fts5"


class _AttachmentRetrieval:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, query, *, asset_ids, limit, context_token_budget=None):
        self.calls.append({
            "query": query,
            "asset_ids": list(asset_ids),
            "limit": limit,
        })
        return SimpleNamespace(
            chunks=[_Chunk(
                asset_id="asset-1",
                part_id="part-1",
                ordinal=2,
                part_type="text",
                text_content="U1-PA0 connects to CAN_RX",
                locator={"page": 3},
                metadata={},
            )],
            degraded_reasons=[],
        )


class _KbBackend:
    def __init__(self):
        self.calls: list[dict] = []

    def retrieve(self, kb_name, query, *, top_k, ctx, filters):
        self.calls.append({
            "kb_name": kb_name,
            "query": query,
            "top_k": top_k,
            "filters": dict(filters or {}),
        })
        return [SimpleNamespace(
            id="chunk-1",
            content="CAN_RX is defined by the hardware specification",
            source_name="spec.pdf",
            score=0.8,
            metadata={"page": 4},
            backend="ragflow",
            retriever="standard",
        )]


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        metadata={"resource_department_id": "3", "kb_id": 7},
        kb_permissions={"3:shared": "read"},
    )


def test_attachment_provider_retrieves_only_frozen_attachment_refs():
    assert hasattr(document_authoring, "AttachmentEvidenceProvider")
    retrieval = _AttachmentRetrieval()
    provider = document_authoring.AttachmentEvidenceProvider(
        retrieval=retrieval,
        refs_snapshot=[{
            "attachment_id": "att-1",
            "asset_id": "asset-1",
            "session_id": 42,
            "filename": "board.edf",
            "media_type": "application/x-edif",
            "extension": ".edf",
            "size_bytes": 100,
            "sha256": "source-hash",
            "usage_hint": "data",
            "parse_status": "ready",
        }],
    )

    evidence = provider.retrieve(
        query="U1-PA0",
        source_scope="attachment_only",
        attachment_ids=["att-1", "att-foreign"],
        kb_name="",
        ctx=_ctx(),
    )

    assert retrieval.calls == [{"query": "U1-PA0", "asset_ids": ["asset-1"], "limit": 8}]
    assert len(evidence) == 1
    assert evidence[0].metadata["source_type"] == "chat_attachment"
    assert evidence[0].metadata["attachment_id"] == "att-1"
    assert evidence[0].locator == {"page": 3}


def test_kb_provider_requires_read_scope_and_marks_knowledge_base_provenance():
    assert hasattr(document_authoring, "KnowledgeBaseEvidenceProvider")
    backend = _KbBackend()
    provider = document_authoring.KnowledgeBaseEvidenceProvider(backend, top_k=5)

    evidence = provider.retrieve(
        query="CAN_RX",
        source_scope="knowledge_base_only",
        attachment_ids=[],
        kb_name="shared",
        ctx=_ctx(),
    )

    assert len(evidence) == 1
    assert evidence[0].metadata["source_type"] == "knowledge_base"
    assert evidence[0].metadata["knowledge_base_name"] == "shared"
    assert backend.calls[0]["top_k"] == 5

    try:
        provider.retrieve(
            query="CAN_RX",
            source_scope="knowledge_base_only",
            attachment_ids=[],
            kb_name="other",
            ctx=_ctx(),
        )
    except PermissionError:
        pass
    else:  # pragma: no cover - assertion keeps the contract explicit
        raise AssertionError("unscoped KB retrieval must be rejected")


def test_composite_provider_honors_combined_scope_without_calling_disallowed_sources():
    assert hasattr(document_authoring, "CompositeDocumentEvidenceProvider")
    attachment = document_authoring.AttachmentEvidenceProvider(
        retrieval=_AttachmentRetrieval(),
        refs_snapshot=[{
            "attachment_id": "att-1",
            "asset_id": "asset-1",
            "session_id": 42,
            "filename": "board.edf",
            "media_type": "application/x-edif",
            "extension": ".edf",
            "size_bytes": 100,
            "sha256": "source-hash",
            "parse_status": "ready",
        }],
    )
    kb_backend = _KbBackend()
    kb = document_authoring.KnowledgeBaseEvidenceProvider(kb_backend)
    provider = document_authoring.CompositeDocumentEvidenceProvider([kb, attachment])

    combined = provider.retrieve(
        query="CAN_RX",
        source_scope="attachment_and_knowledge_base",
        attachment_ids=["att-1"],
        kb_name="shared",
        ctx=_ctx(),
    )
    assert [item.metadata["source_type"] for item in combined] == [
        "knowledge_base", "chat_attachment"
    ]

    kb_backend.calls.clear()
    attachment.retrieval.calls.clear()
    attachment_only = provider.retrieve(
        query="CAN_RX",
        source_scope="attachment_only",
        attachment_ids=["att-1"],
        kb_name="shared",
        ctx=_ctx(),
    )
    assert not kb_backend.calls
    assert not any(item.metadata.get("source_type") == "knowledge_base" for item in attachment_only)
    assert attachment.retrieval.calls
