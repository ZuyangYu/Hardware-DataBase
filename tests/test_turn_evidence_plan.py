"""Target/query coverage is execution state, not proof of answer completeness."""

from __future__ import annotations

from types import SimpleNamespace

import src.settings as settings
from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolRuntime
from src.services.retrieval_targets import TargetLocator
from src.services.turn_evidence_plan import (
    ADMITTED, CANDIDATE, MISSING, PENDING, TurnEvidencePlan,
)


def target(kb_id=5, record_id=101, locators=None, target_id="t1"):
    return SimpleNamespace(
        target_id=target_id, kb_id=kb_id, kb_name=f"KB{kb_id}", record_id=record_id,
        question="pin 2 voltage", query_id="q1",
        locators=tuple(locators or (TargetLocator(target_id, sheet_name="Pin Definition", pin_value="2"),)),
    )


def evidence(record_id=101, kb_id=5, target_ids=("t1",), query_id="q1", content="pin 2: 5V", **metadata):
    return Evidence(id=f"e-{record_id}", content=content, source_name="f", content_kind="spreadsheet_table",
                    processor_kind="spreadsheet_table", score=0.9,
                    locator={"record_id": record_id, "sheet_name": "Pin Definition", "row_index": 29},
                    metadata={"kb_id": kb_id, "local_record_id": record_id, "target_ids": list(target_ids),
                              "query_id": query_id, "values": {"Pin Number": "2", "Voltage": "5V"},
                              "knowledge_source": {"kb_id": kb_id, "kb_name": f"KB{kb_id}",
                                                   "department_id": "47", "role": "primary"},
                              **metadata})


def test_plan_starts_pending_and_candidate_does_not_claim_coverage():
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5, 6, 7])
    assert plan.coverage_snapshot()["states"][PENDING] == 1
    matched = plan.evaluate(evidence())
    assert matched == ("t1:q1:1",)
    plan.mark_candidate(matched, "e-101")
    snapshot = plan.coverage_snapshot()
    assert snapshot["states"][CANDIDATE] == 1
    assert snapshot["complete"] is False
    plan.mark_admitted(matched, "e-101")
    assert plan.coverage_snapshot()["complete"] is True


def test_plan_requires_exact_record_and_locator_and_one_evidence_can_cover_two_targets():
    plan = TurnEvidencePlan.from_targets([
        target(record_id=101),
        target(record_id=102, target_id="t2"),
    ])
    assert plan.evaluate(evidence(record_id=999)) == ()
    shared = evidence()
    shared.metadata["target_ids"] = ["t1", "t2"]
    # The record id still differs; target ids alone cannot satisfy identity.
    assert plan.evaluate(shared) == ("t1:q1:1",)


def test_missing_and_blocked_are_distinct_and_preserve_admitted_evidence():
    plan = TurnEvidencePlan.from_targets([target()])
    plan.mark_admitted(("t1:q1:1",), "e-101")
    plan.mark_blocked("budget_exhausted")
    assert plan.coverage_snapshot()["states"][ADMITTED] == 1
    plan.mark_missing("q1")
    assert plan.coverage_snapshot()["states"][ADMITTED] == 1


def test_untargeted_selected_kbs_do_not_become_required_or_missing():
    plan = TurnEvidencePlan.from_targets([target(kb_id=5)], selected_kb_ids=[5, 6, 7])
    snapshot = plan.coverage_snapshot()
    assert snapshot["targeted_kb_ids"] == [5]
    assert snapshot["not_targeted_kb_ids"] == [6, 7]
    assert snapshot["states"][MISSING] == 0


def _runtime(plan):
    return ToolRuntime(
        kb_name="KB5", ctx=None, knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        question="pin 2 voltage", evidence_budget=10, turn_evidence_plan=plan,
    )


def test_runtime_admits_pending_target_before_higher_ranked_background(monkeypatch):
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = _runtime(plan)
    rt.evidence_budget = 1
    background = evidence(record_id=777, target_ids=(), query_id="q0")
    background.metadata["fusion_rank"] = 1
    target_item = evidence()
    target_item.metadata["fusion_rank"] = 9

    numbers = rt.add_evidence([background, target_item])

    assert numbers == [None, 1]
    assert [item.id for item in rt.evidence] == ["e-101"]
    assert plan.coverage_snapshot()["states"][ADMITTED] == 1


def test_runtime_does_not_reserve_room_for_selected_but_untargeted_kbs():
    plan = TurnEvidencePlan.from_targets([target(kb_id=5)], selected_kb_ids=[5, 6, 7])
    rt = ToolRuntime(
        kb_name="KB5", ctx=None,
        knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5), SimpleNamespace(kb_id=6), SimpleNamespace(kb_id=7)]),
        question="pin 2 voltage", evidence_budget=1, turn_evidence_plan=plan,
    )
    numbers = rt.add_evidence([evidence()])
    assert numbers == [1]
    assert plan.coverage_snapshot()["not_targeted_kb_ids"] == [6, 7]


def test_runtime_byte_limit_blocks_admission_without_losing_candidate_state(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", 1)
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = _runtime(plan)
    numbers = rt.add_evidence([evidence()])
    assert numbers == [None]
    assert rt.evidence == []
    assert plan.coverage_snapshot()["states"][CANDIDATE] == 1


def test_admit_candidate_batch_includes_earlier_pooled_evidence_and_new_rows():
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = _runtime(plan)
    first = evidence()
    assert rt.add_evidence([first]) == [1]
    later = Evidence(id="e-later", content="pin 2 note", source_name="f", content_kind="spreadsheet_table",
                     processor_kind="spreadsheet_table", score=0.8,
                     locator={"record_id": 101, "sheet_name": "Pin Definition", "row_index": 29},
                     metadata={"kb_id": 5, "local_record_id": 101, "target_ids": ["t1"],
                               "query_id": "q1", "values": {"Pin Number": "2"}})
    result = rt.admit_candidate_batch([first, later], source_statuses=[{"kb_id": 5, "status": "success_with_hits"}])
    assert result.numbers == [1, 2]
    assert [item.id for item in result.evidence] == ["e-101", "e-later"]
    assert result.target_coverage["states"][ADMITTED] == 1
