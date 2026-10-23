"""Source-aware budget slices never borrow promised reserves."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import src.settings as settings
from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolRuntime
from src.services.evidence_budget import (
    GLOBAL_BUDGET_EXHAUSTED,
    ITEM_LIMIT_EXHAUSTED,
    SOURCE_BUDGET_EXHAUSTED,
    EvidenceBudgetCoordinator,
)


def coordinator(*, global_bytes=6000, attachment=3000, kb=6000,
                attachment_reserve=2000, kb_reserve=1000,
                system=0, answer=0, item_limit=0):
    return EvidenceBudgetCoordinator(
        global_bytes=global_bytes,
        source_limits={"attachment": attachment, "knowledge_base_document": kb},
        source_reserves={
            "attachment": attachment_reserve,
            "knowledge_base_document": kb_reserve,
        },
        system_reserve_bytes=system,
        answer_reserve_bytes=answer,
        item_limit_per_source=item_limit,
    )


def test_kb_cannot_spend_the_attachment_required_reserve():
    budget = coordinator()
    # KB fills more than its reserve; the attachment reserve must stay intact.
    assert budget.try_admit("knowledge_base_document", 4000).admitted
    budget.note_admitted("knowledge_base_document", 4000)
    assert budget.try_admit("attachment", 2500).reason == GLOBAL_BUDGET_EXHAUSTED
    assert budget.try_admit("attachment", 1500).admitted
    budget.note_admitted("attachment", 1500)
    snapshot = budget.snapshot()
    assert snapshot["global_used_bytes"] == 5500
    assert snapshot["global_remaining_bytes"] == 500


def test_attachment_budget_exhaustion_does_not_block_kb_slice():
    budget = coordinator(
        global_bytes=8000, attachment=1000, kb=4000, attachment_reserve=200,
    )
    assert budget.try_admit("attachment", 900).admitted
    budget.note_admitted("attachment", 900)
    blocked = budget.try_admit("attachment", 200)
    assert blocked.reason == SOURCE_BUDGET_EXHAUSTED
    decision = budget.try_admit("knowledge_base_document", 3000)
    assert decision.admitted
    budget.note_admitted("knowledge_base_document", 3000)
    snapshot = budget.snapshot()
    assert snapshot["slices"]["attachment"]["used_bytes"] == 900
    assert snapshot["slices"]["knowledge_base_document"]["used_bytes"] == 3000


def test_kb_exhaustion_does_not_evict_admitted_attachment():
    budget = coordinator(global_bytes=5000, attachment=2000, kb=1000)
    assert budget.try_admit("attachment", 1800).admitted
    budget.note_admitted("attachment", 1800)
    assert budget.try_admit("knowledge_base_document", 1200).reason == SOURCE_BUDGET_EXHAUSTED
    snapshot = budget.snapshot()
    assert snapshot["slices"]["attachment"]["used_bytes"] == 1800
    assert snapshot["global_used_bytes"] == 1800


def test_item_limit_is_reported_separately():
    budget = coordinator(global_bytes=10000, attachment=10000, kb=10000, item_limit=1)
    assert budget.try_admit("attachment", 10).admitted
    budget.note_admitted("attachment", 10)
    assert budget.try_admit("attachment", 10).reason == ITEM_LIMIT_EXHAUSTED


def test_concurrent_admission_never_exceeds_the_global_cap():
    budget = coordinator(
        global_bytes=5000, attachment=5000, kb=5000,
        attachment_reserve=0, kb_reserve=0,
    )
    accepted: list[str] = []
    lock = threading.Lock()

    def admit(source: str) -> None:
        for _ in range(50):
            decision = budget.admit(source, 100)
            if decision.admitted:
                with lock:
                    accepted.append(source)

    threads = [threading.Thread(target=admit, args=(source,)) for source in (
        "attachment", "knowledge_base_document",
    ) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    snapshot = budget.snapshot()
    assert snapshot["global_used_bytes"] <= 5000
    assert len(accepted) == 50


def test_comparison_reserve_cannot_be_consumed_by_ordinary_retrieval():
    budget = EvidenceBudgetCoordinator(
        global_bytes=10000,
        source_limits={"attachment": 8000, "knowledge_base_document": 8000},
        source_reserves={"attachment": 0, "knowledge_base_document": 0},
        comparison_reserve_bytes=3000,
        comparison_limit_bytes=3000,
    )
    assert budget.try_admit("knowledge_base_document", 7000).admitted
    budget.note_admitted("knowledge_base_document", 7000)
    # Ordinary evidence must leave the comparison reserve untouched.
    assert budget.try_admit("attachment", 2000).reason == GLOBAL_BUDGET_EXHAUSTED
    decision = budget.try_admit("comparison", 2500)
    assert decision.admitted
    budget.note_admitted("comparison", 2500)
    snapshot = budget.snapshot()
    assert snapshot["slices"]["comparison"]["used_bytes"] == 2500
    assert snapshot["global_used_bytes"] == 9500


def _attachment_evidence(content="attachment text", **metadata):
    return Evidence(
        id=f"att-{hash(content)}",
        content=content,
        source_name="附件.docx",
        content_kind="text",
        processor_kind="local_attachment:lexical",
        metadata={"source_type": "attachment", "attachment_id": "att-1", **metadata},
    )


def _kb_evidence(content="kb text"):
    return Evidence(
        id=f"kb-{hash(content)}",
        content=content,
        source_name="KB 文档",
        content_kind="document_text",
        processor_kind="ragflow",
        metadata={"source_type": "knowledge_base_document", "kb_id": 5, "local_record_id": 9},
    )


def test_runtime_rejects_attachment_overrun_but_still_admits_kb(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 20)
    monkeypatch.setattr(settings, "MULTI_KB_CONTEXT_MAX_TOKENS", 10**6)
    monkeypatch.setattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", 10**6)
    bt = EvidenceBudgetCoordinator(
        global_bytes=20000,
        source_limits={"attachment": 1600, "knowledge_base_document": 5000},
        source_reserves={"attachment": 300, "knowledge_base_document": 300},
    )
    rt = ToolRuntime(
        kb_name="KB5", ctx=None,
        knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        source_scope="attachment_and_knowledge_base",
        question="q", evidence_budget=50,
    )
    rt.evidence_budget_coordinator = bt
    attachment = _attachment_evidence("A" * 1000)
    kb = _kb_evidence("K" * 200)

    assert rt.add_evidence([attachment]) == [1]
    second = _attachment_evidence("B" * 1000)
    second.id = "att-second"
    assert rt.add_evidence([second]) == [None]
    assert rt.last_rejection_reasons[SOURCE_BUDGET_EXHAUSTED] == 1
    assert rt.add_evidence([kb]) == [2]
    assert rt.source_budget_snapshot()["slices"]["knowledge_base_document"]["used_bytes"] > 0


def test_comparison_report_is_admitted_after_ordinary_retrieval(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 24)
    monkeypatch.setattr(settings, "MULTI_KB_CONTEXT_MAX_TOKENS", 8000)
    monkeypatch.setattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", 8000)
    from src.services.turn_evidence_plan import TurnEvidencePlan

    plan = TurnEvidencePlan.from_comparison_sides(
        turn_id="t", attachment_ids=["att-1"], kb_ids=[5], selected_kb_ids=[5],
    )
    bt = EvidenceBudgetCoordinator(
        global_bytes=22000,
        source_limits={"attachment": 12000, "knowledge_base_document": 16000},
        source_reserves={"attachment": 4000, "knowledge_base_document": 4000},
        comparison_reserve_bytes=8000,
        comparison_limit_bytes=12000,
    )
    rt = ToolRuntime(
        kb_name="KB5", ctx=None,
        knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        source_scope="attachment_and_knowledge_base",
        question="compare", evidence_budget=80, turn_evidence_plan=plan,
    )
    rt.evidence_budget_coordinator = bt
    plan.source_budget_provider = bt.snapshot

    ordinary = []
    for index in range(14):
        item = _kb_evidence("K" * 900)
        item.id = f"kb-{index}"
        ordinary.append(item)
    admitted = [number for number in rt.add_evidence(ordinary) if number is not None]
    # The comparison plan keeps at most two rows per side obligation, which
    # leaves the dedicated comparison reserve untouched.
    assert len(admitted) >= 2
    assert bt.snapshot()["global_used_bytes"] <= 14000

    comparison = Evidence(
        id="comparison:cmp-1:item-1",
        content="比较项：两侧文件内容哈希相同，整篇未发现差异。",
        source_name="附件 ↔ 知识库",
        content_kind="comparison_unchanged",
        processor_kind="document_comparison",
        metadata={
            "source_type": "comparison",
            "evidence_role": "comparison",
            "comparison_sources": ["attachment", "knowledge_base"],
            "comparison_report_id": "cmp-1",
        },
    )
    numbers = rt.add_evidence([comparison])
    assert numbers != [None]
    assert bt.snapshot()["slices"]["comparison"]["used_bytes"] > 0
    assert plan.coverage_snapshot()["comparison_complete"] is True


def test_source_aware_budget_supersedes_legacy_shared_byte_counter(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 24)
    monkeypatch.setattr(settings, "MULTI_KB_CONTEXT_MAX_TOKENS", 8000)
    monkeypatch.setattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", 8000)
    bt = EvidenceBudgetCoordinator(
        global_bytes=30000,
        source_limits={"attachment": 12000, "knowledge_base_document": 16000},
        source_reserves={"attachment": 4000, "knowledge_base_document": 4000},
    )
    rt = ToolRuntime(
        kb_name="KB5", ctx=None,
        knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        source_scope="attachment_and_knowledge_base",
        question="compare", evidence_budget=50,
    )
    rt.evidence_budget_coordinator = bt

    attachments = []
    for index in range(8):
        item = _attachment_evidence("A" * 900)
        item.id = f"att-{index}"
        attachments.append(item)
    assert all(number is not None for number in rt.add_evidence(attachments))
    # The legacy 8000-byte shared counter would have blocked this KB row; the
    # coordinator still has the KB slice and its reserve available.
    assert rt.add_evidence([_kb_evidence("K" * 300)]) == [9]
