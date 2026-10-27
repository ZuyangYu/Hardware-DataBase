"""Source identity is strict: attachment text cannot satisfy a KB obligation."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.schemas import Evidence
from src.services.turn_evidence_plan import TurnEvidencePlan


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


def test_coverage_snapshot_exposes_source_budget_provider():
    plan = _plan()
    plan.source_budget_provider = lambda: {
        "slices": {"attachment": {"status": "source_budget_exhausted"}},
    }
    snapshot = plan.coverage_snapshot()
    assert snapshot["source_budget_status"]["slices"]["attachment"]["status"] == "source_budget_exhausted"
    assert snapshot["blocked_by_source"] == {}
