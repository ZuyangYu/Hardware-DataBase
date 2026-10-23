"""Source identity is strict: attachment text cannot satisfy a KB obligation."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.schemas import Evidence
from src.services.turn_evidence_plan import (
    ADMITTED,
    TurnEvidencePlan,
)


def _kb_evidence(**metadata):
    return Evidence(
        id="kb-1",
        content="HSI pin 2 is 5V",
        source_name="HSI.docx",
        content_kind="document_text",
        processor_kind="ragflow",
        metadata={
            "kb_id": 5,
            "local_record_id": 101,
            "target_ids": ["t1"],
            "query_id": "q1",
            **metadata,
        },
        locator={"chunk_id": "chunk-1"},
    )


def _attachment_evidence(**metadata):
    return Evidence(
        id="att-1",
        content="HSI pin 2 is 3.3V",
        source_name="user-hsi.docx",
        content_kind="text",
        processor_kind="local_attachment:lexical",
        metadata={
            "source_type": "attachment",
            "attachment_id": "att-1",
            "asset_id": "asset-1",
            **metadata,
        },
        locator={"heading_path": ["HSI"], "paragraph": 2},
    )


def _plan():
    target = SimpleNamespace(
        target_id="t1", kb_id=5, kb_name="KB5", record_id=101,
        question="pin 2 voltage", query_id="q1",
        processor_kind="ragflow",
        locators=(SimpleNamespace(sheet_name=None, row_index=None, cell_ref=None, pin_value=None),),
    )
    return TurnEvidencePlan.from_targets([target], selected_kb_ids=[5])


def test_attachment_never_matches_a_knowledge_base_target_obligation():
    plan = _plan()
    assert plan.evaluate(_attachment_evidence()) == ()
    snapshot = plan.coverage_snapshot()
    assert snapshot["false_match_rejected"] >= 1
    assert snapshot["states"]["pending"] == 1


def test_legacy_chat_attachment_is_normalized_but_still_cannot_match_kb():
    plan = _plan()
    item = _attachment_evidence(source_type="chat_attachment")
    assert plan.evaluate(item) == ()


def test_missing_identity_is_not_a_wildcard_for_a_restricted_obligation():
    plan = _plan()
    item = Evidence(
        id="unknown", content="text", source_name="anonymous",
        content_kind="document_text", processor_kind="ragflow", metadata={},
    )
    assert plan.evaluate(item) == ()


def test_knowledge_evidence_does_not_satisfy_attachment_source_obligation():
    plan = TurnEvidencePlan.from_comparison_sources(
        turn_id="turn-1", attachment_ids=["att-1"], kb_ids=[5], selected_kb_ids=[5],
    )
    matched = plan.evaluate(_kb_evidence(target_ids=["source:knowledge_base:5"]))
    assert matched == ("source:knowledge_base:5",)
    snapshot = plan.coverage_snapshot()
    assert snapshot["comparison_complete"] is False


def test_comparison_report_with_both_sides_can_satisfy_both_source_obligations():
    plan = TurnEvidencePlan.from_comparison_sources(
        turn_id="turn-1", attachment_ids=["att-1"], kb_ids=[5], selected_kb_ids=[5],
    )
    report_evidence = Evidence(
        id="comparison:cmp-1:item-1",
        content="changed pin voltage",
        source_name="HSI ↔ KB HSI",
        content_kind="comparison_modified",
        processor_kind="document_comparison",
        metadata={
            "source_type": "knowledge_base",
            "evidence_role": "comparison",
            "comparison_sources": ["attachment", "knowledge_base"],
            "comparison_report_id": "cmp-1",
        },
        locator={"comparison_report_id": "cmp-1"},
    )
    matched = plan.evaluate(report_evidence)
    assert set(matched) == {"source:attachment:att-1", "source:knowledge_base:5"}
    plan.mark_admitted(matched, report_evidence.id)
    snapshot = plan.coverage_snapshot()
    assert snapshot["states"][ADMITTED] == 2
    assert snapshot["comparison_complete"] is True


def test_attachment_source_obligation_requires_a_real_attachment_identity():
    plan = TurnEvidencePlan.from_comparison_sources(
        turn_id="turn-1", attachment_ids=["att-1"], kb_ids=[5], selected_kb_ids=[5],
    )
    wrong = _attachment_evidence(attachment_id="att-other", asset_id="asset-other")
    assert plan.evaluate(wrong) == ()
    assert plan.evaluate(_attachment_evidence()) == ("source:attachment:att-1",)


def test_comparison_sides_are_one_obligation_each_not_every_candidate():
    plan = TurnEvidencePlan.from_comparison_sides(
        turn_id="turn-1",
        attachment_ids=["att-1", "att-2"],
        kb_ids=[5, 6],
        selected_kb_ids=[5, 6],
    )
    snapshot = plan.coverage_snapshot()
    assert snapshot["required"] == 2
    assert snapshot["by_source"]["attachment"]["pending"] == 1
    assert snapshot["by_source"]["knowledge_base"]["pending"] == 1
    report_evidence = Evidence(
        id="comparison:cmp-1:item-1",
        content="frozen diff",
        source_name="a ↔ b",
        content_kind="comparison_modified",
        processor_kind="document_comparison",
        metadata={
            "source_type": "comparison",
            "evidence_role": "comparison",
            "comparison_sources": ["attachment", "knowledge_base"],
            "comparison_report_id": "cmp-1",
        },
    )
    matched = plan.evaluate(report_evidence)
    assert set(matched) == {
        "source:attachment:any", "source:knowledge_base:any",
    }
    plan.mark_admitted(matched, report_evidence.id)
    assert plan.coverage_snapshot()["comparison_complete"] is True


def test_coverage_snapshot_exposes_source_budget_provider():
    plan = _plan()
    plan.source_budget_provider = lambda: {
        "slices": {"attachment": {"status": "source_budget_exhausted"}},
    }
    snapshot = plan.coverage_snapshot()
    assert snapshot["source_budget_status"]["slices"]["attachment"]["status"] == "source_budget_exhausted"
    assert snapshot["blocked_by_source"] == {}
