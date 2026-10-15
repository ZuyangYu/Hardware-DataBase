"""Provenance labels and citation admission stay stable after fusion."""

from __future__ import annotations

from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolRuntime, format_evidence_for_llm
from tests.test_multi_kb_store_contract import snapshot


def item(kb_id, ordinal, score=0.9):
    return Evidence(id=f"full-identity-{kb_id}-{ordinal}", content="raw source text", source_name="spec.pdf",
        processor_kind="ragflow", content_kind="document_text", score=score, locator={"page": 4},
        metadata={"kb_id": kb_id, "fusion_rank": ordinal, "source_content_hash": "a" * 64,
                  "knowledge_source": {"kb_id": kb_id, "kb_name": f"Project {kb_id}", "role": "primary" if kb_id == 101 else "reference"}})


def test_llm_citation_labels_include_actual_kb_file_and_version_fingerprint():
    a, b = item(101, 1), item(205, 2)
    a.metadata["citation_number"], b.metadata["citation_number"] = 1, 2
    rendered = format_evidence_for_llm([a, b])
    assert "[1] 来源：Project 101 · spec.pdf · aaaaaaaaaaaa" in rendered
    assert "[2] 来源：Project 205 · spec.pdf · aaaaaaaaaaaa" in rendered
    assert "page=4" in rendered


def test_fusion_order_and_source_coverage_override_incomparable_raw_scores():
    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=snapshot(1), evidence_budget=2)
    a, b, extra = item(101, 1, 0.6), item(205, 2, 0.5), item(101, 3, 0.99)
    assert rt.add_evidence([a, b, extra]) == [1, 2, None]
    assert rt.add_evidence([b, a]) == [2, 1]
    rt.finalize_evidence()
    assert [e.id for e in rt.evidence] == [a.id, b.id]


def test_multiscope_late_evidence_cannot_reserve_or_consume_citations():
    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=snapshot(1), evidence_budget=2)
    epoch = rt.begin_tool_call()
    rt.end_tool_call(epoch)
    assert rt.add_evidence([item(101, 1), item(205, 2)], epoch=epoch) == [None, None]
    assert rt.evidence_index == {}
    assert rt.add_evidence([item(101, 1), item(205, 2)]) == [1, 2]
