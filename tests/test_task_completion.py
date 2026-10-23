"""Scope-aware completion gate and coverage ledger."""

from __future__ import annotations

from src.services.task_budget import TurnBudget
from src.services.task_completion import CompletionGate
from src.services.task_planning import TaskPlanCompiler
from src.services.task_progress import CoverageLedger, TaskProgress


def _plan(query="按章节说差异", *, scope="chapter_summary"):
    return TaskPlanCompiler().compile(
        query=query,
        evidence_purpose="compare",
        source_scope_fingerprint="scope-a",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )


def _progress(plan):
    progress = TaskProgress.new(plan.plan_id)
    progress.ensure_deliverables(tuple(item.deliverable_id for item in plan.deliverables))
    return progress


def test_chapter_summary_complete_when_all_chapters_covered():
    plan = _plan()
    progress = _progress(plan)
    progress.coverage.set_chapters({
        "num:1": ("i1", "i2"),
        "num:2": ("i3",),
    })
    progress.coverage.mark_rendered(("i1", "i2", "i3"))
    for chapter in progress.coverage.chapters.values():
        chapter.state = "satisfied"
        chapter.covered_count = chapter.required_count
    decision = CompletionGate().evaluate(plan, progress, answer_text="章节概览")
    assert decision.status == "complete"
    assert decision.safe_to_answer is True


def test_chapter_detail_is_partial_when_items_missing():
    plan = TaskPlanCompiler().compile(
        query="第 4 章具体差异",
        evidence_purpose="compare",
        source_scope_fingerprint="scope-a",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    progress = _progress(plan)
    progress.coverage.required_item_ids = ("i1", "i2", "i3")
    progress.coverage.mark_rendered(("i1",))
    decision = CompletionGate().evaluate(plan, progress, answer_text="第 4 章部分差异")
    assert decision.status == "partial"
    assert "i2" in decision.missing_item_ids
    assert decision.safe_to_answer is True


def test_optional_deliverable_missing_does_not_downgrade():
    plan = _plan()
    progress = _progress(plan)
    progress.coverage.set_chapters({"num:1": ("i1",)})
    progress.coverage.mark_rendered(("i1",))
    progress.coverage.chapters["num:1"].state = "satisfied"
    progress.coverage.chapters["num:1"].covered_count = 1
    decision = CompletionGate().evaluate(plan, progress, answer_text="章节概览")
    assert decision.status == "complete"


def test_warnings_produce_complete_with_warnings():
    plan = _plan()
    progress = _progress(plan)
    progress.coverage.set_chapters({"num:1": ("i1",)})
    progress.coverage.mark_rendered(("i1",))
    progress.coverage.chapters["num:1"].state = "satisfied"
    progress.coverage.chapters["num:1"].covered_count = 1
    decision = CompletionGate().evaluate(
        plan, progress, answer_text="章节概览", warnings=("unparsed_embedded_object",),
    )
    assert decision.status == "complete_with_warnings"


def test_compile_error_is_blocked():
    plan = TaskPlanCompiler().compile(
        query="对比两份文件",
        evidence_purpose="compare",
        has_attachments=True,
        has_kb=True,
        comparison_available=False,
    )
    progress = _progress(plan)
    decision = CompletionGate().evaluate(plan, progress, answer_text="")
    assert decision.status == "blocked"
    assert "comparison_capability_unavailable" in decision.reason


def test_turn_budget_is_independent_and_bounded():
    first = TurnBudget()
    assert first.consume("tool_call") is True
    assert first.usage.tool_calls == 1
    second = TurnBudget()
    assert second.usage.tool_calls == 0
    for _ in range(first.profile.max_tool_calls):
        first.consume("tool_call")
    assert first.can_consume("tool_call") is False


def test_coverage_missing_recomputed():
    ledger = CoverageLedger(required_item_ids=("a", "b"))
    ledger.mark_rendered(("a",))
    assert ledger.missing_item_ids == ("b",)
    ledger.mark_rendered(("b",))
    assert ledger.missing_item_ids == ()
