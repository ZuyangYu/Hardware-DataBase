"""Candidate ranking and runtime admission cover the R3 failure modes."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.tools.runtime import ToolRuntime
from src.services.evidence_fusion import rank_evidence_candidates
from src.services.knowledge_scope_models import KnowledgeSourceRetrievalOutcome
from src.services.turn_evidence_plan import ADMITTED, TurnEvidencePlan
from tests.test_multi_kb_evidence_fusion import evidence as document_evidence
from tests.test_turn_evidence_plan import evidence, target


def test_ranking_keeps_target_candidate_before_runtime_budgeting():
    items = [document_evidence(chunk=f"chunk-{index}", content=f"candidate {index}", record_id=900 + index)
             for index in range(13)]
    batch = rank_evidence_candidates(
        [KnowledgeSourceRetrievalOutcome(101, "q", "document", "success_with_hits", items)],
        purpose="answer",
    )
    assert len(batch.candidates) == 13
    assert len({candidate.identity for candidate in batch.candidates}) == 13


def test_selected_but_untargeted_kbs_do_not_consume_admission_room():
    plan = TurnEvidencePlan.from_targets([target(kb_id=5)], selected_kb_ids=[5, 6, 7])
    rt = ToolRuntime(
        kb_name="KB5", ctx=None,
        knowledge_scope=SimpleNamespace(sources=[
            SimpleNamespace(kb_id=5), SimpleNamespace(kb_id=6), SimpleNamespace(kb_id=7),
        ]),
        question="pin 2", evidence_budget=1, turn_evidence_plan=plan,
    )
    assert rt.add_evidence([evidence()]) == [1]


def test_earlier_pooled_candidate_is_admitted_without_replacing_its_number():
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = ToolRuntime(
        kb_name="KB5", ctx=None, knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        question="pin 2", evidence_budget=5, turn_evidence_plan=plan,
    )
    first = evidence()
    assert rt.add_evidence([first]) == [1]
    second = evidence(content="pin 2 second note")
    second.id = "e-later"
    result = rt.admit_candidate_batch([first, second])
    assert result.numbers == [1, 2]


def test_cancelled_call_epoch_cannot_revive_a_candidate():
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = ToolRuntime(
        kb_name="KB5", ctx=None, knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        question="pin 2", evidence_budget=5, turn_evidence_plan=plan,
    )
    epoch = rt.begin_tool_call()
    rt.end_tool_call(epoch)
    assert rt.add_evidence([evidence()], epoch=epoch) == [None]
    assert rt.evidence == []


def test_bounded_candidate_pool_promotes_target_candidate_on_later_call():
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = ToolRuntime(
        kb_name="KB5", ctx=None, knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        question="pin 2", evidence_budget=1, turn_evidence_plan=plan,
    )
    first = evidence()
    assert rt.add_evidence([first]) == [1]
    second = evidence(content="pin 2 later candidate")
    second.id = "e-later"
    assert rt.add_evidence([second]) == [None]
    assert "e-later" in rt.candidate_pool

    rt.evidence_budget = 2
    promoted = rt.promote_pooled_candidates()
    assert [item.id for item in promoted] == ["e-later"]
    assert promoted[0].metadata["citation_number"] == 2
    assert plan.coverage_snapshot()["states"][ADMITTED] == 1


def test_candidate_pool_is_bounded_by_source_candidate_config(monkeypatch):
    import src.settings as settings

    monkeypatch.setattr(settings, "MULTI_KB_MAX_SOURCES", 1)
    monkeypatch.setattr(settings, "MULTI_KB_PER_SOURCE_CANDIDATES", 2)
    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = ToolRuntime(
        kb_name="KB5", ctx=None, knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        question="pin 2", evidence_budget=1, turn_evidence_plan=plan,
    )
    items = []
    for index in range(5):
        candidate = evidence(content=f"pin 2 candidate {index}")
        candidate.id = f"e{index}"
        items.append(candidate)
    rt.add_evidence(items)
    assert len(rt.candidate_pool) == 2


def test_admission_rejection_reasons_split_call_and_byte_limits(monkeypatch):
    import src.settings as settings

    plan = TurnEvidencePlan.from_targets([target()], selected_kb_ids=[5])
    rt = ToolRuntime(
        kb_name="KB5", ctx=None, knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        question="pin 2", evidence_budget=1, turn_evidence_plan=plan,
    )
    first = evidence()
    second = evidence(content="pin 2 second")
    second.id = "e2"
    rt.add_evidence([first, second])
    assert rt.last_rejection_reasons["call_quota"] == 1

    monkeypatch.setattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", 1)
    rt2 = ToolRuntime(
        kb_name="KB5", ctx=None, knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        question="pin 2", evidence_budget=5, turn_evidence_plan=plan,
    )
    rt2.add_evidence([evidence()])
    assert rt2.last_rejection_reasons["byte_limit"] == 1
