"""Bounded recovery selection."""

from __future__ import annotations

from src.services.task_completion import CompletionDecision
from src.services.task_planning import TaskPlanCompiler
from src.services.task_progress import TaskProgress
from src.services.task_recovery import select_recovery


def _plan(query):
    return TaskPlanCompiler().compile(
        query=query,
        evidence_purpose="compare",
        source_scope_fingerprint="scope",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )


def _progress(plan):
    progress = TaskProgress.new(plan.plan_id)
    progress.ensure_deliverables(tuple(item.deliverable_id for item in plan.deliverables))
    return progress


def test_chapter_summary_selects_server_composer():
    plan = _plan("按章节说差异")
    progress = _progress(plan)
    decision = CompletionDecision(
        status="partial",
        missing_deliverables=("compare.chapter_summary",),
        safe_to_answer=True,
    )
    action = select_recovery(
        plan, progress, decision,
        report_available=True, recovery_steps_remaining=2, tool_calls_remaining=2,
    )
    assert action is not None
    assert action.action == "compose_chapter_summary"


def test_chapter_detail_selects_expand_with_scope():
    plan = _plan("第 4 章具体差异")
    progress = _progress(plan)
    decision = CompletionDecision(
        status="partial",
        missing_deliverables=("compare.item_body", "compare.inline_diff"),
        safe_to_answer=True,
    )
    action = select_recovery(
        plan, progress, decision,
        report_available=True, recovery_steps_remaining=2, tool_calls_remaining=2,
    )
    assert action is not None
    assert action.action == "comparison_expand"
    assert action.params["chapter_keys"] == ["num:4"]


def test_exhausted_budget_stops_recovery():
    plan = _plan("第 4 章具体差异")
    progress = _progress(plan)
    decision = CompletionDecision(
        status="partial",
        missing_deliverables=("compare.item_body",),
        safe_to_answer=True,
    )
    assert select_recovery(
        plan, progress, decision,
        report_available=True, recovery_steps_remaining=0, tool_calls_remaining=2,
    ) is None
    assert select_recovery(
        plan, progress, decision,
        report_available=True, recovery_steps_remaining=2, tool_calls_remaining=0,
    ) is None
