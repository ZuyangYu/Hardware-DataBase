"""Attachment preflight returns candidates, never formal citations."""

from __future__ import annotations

from src.agents.tools import attachment_tools
from src.agents.tools.attachment_tools import make_attachment_search
from src.agents.tools.runtime import ToolRuntime
from src.attachments.models import AttachmentRef
from src.attachments.retrieval import RetrievalResult, RetrievedChunk


class _FakeRetriever:
    def __init__(self):
        self.queries: list[str] = []
        self.degraded_reasons: list[str] = []

    def search(self, query, *, asset_ids, limit=8, context_token_budget=None):
        self.queries.append(query)
        return RetrievalResult(query=query, chunks=[
            RetrievedChunk(
                asset_id="asset-1",
                part_id="part-1",
                ordinal=3,
                part_type="text",
                text_content="HSI 管脚 2 的电压为 3.3V。",
                locator={"heading_path": ["HSI", "管脚定义"], "paragraph": 3},
                metadata={},
                score=0.92,
                backend="lexical",
            )
        ])


def _ref() -> AttachmentRef:
    return AttachmentRef(
        attachment_id="att-1",
        asset_id="asset-1",
        session_id=1,
        filename="user-hsi.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        extension=".docx",
        size_bytes=10,
        sha256="a" * 64,
    )


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        kb_name="KB5", ctx=None, question="HSI 管脚 2 电压",
        source_scope="attachment_and_knowledge_base",
        attachment_refs=[_ref()], attachment_user_id=7, attachment_session_id=1,
        evidence_budget=5,
    )


def test_candidate_mode_returns_locator_without_allocating_a_citation(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr(attachment_tools, "_authorized_refs", lambda rt: list(rt.attachment_refs))
    retriever = _FakeRetriever()
    search = make_attachment_search(runtime, retriever, candidate_mode=True)

    preview = search.preview("管脚 2")

    assert "attachment_candidate" in preview
    assert 'attachment_id="att-1"' in preview
    assert "heading_path" in preview
    assert runtime.evidence == []
    assert runtime.evidence_index == {}
    assert "citation_number" not in preview


def test_explicit_search_keeps_the_formal_evidence_path(monkeypatch):
    runtime = _runtime()
    runtime.source_scope = "attachment_only"
    monkeypatch.setattr(attachment_tools, "_authorized_refs", lambda rt: list(rt.attachment_refs))
    search = make_attachment_search(runtime, _FakeRetriever())

    rendered = search("管脚 2")

    assert "<evidence " in rendered
    assert len(runtime.evidence) == 1
    assert runtime.evidence[0].metadata["source_type"] == "attachment"
    assert runtime.evidence[0].metadata["attachment_id"] == "att-1"
    assert runtime.evidence[0].metadata["content_hash"]
    assert runtime.evidence[0].metadata["document_version"] == "a" * 16
