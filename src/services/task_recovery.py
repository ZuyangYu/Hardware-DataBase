"""Bounded recovery selection for the task completion gate."""

from __future__ import annotations

from src.services.task_planning import RecoveryAction, TaskPlan
from src.services.task_progress import TaskProgress
from src.services.task_completion import CompletionDecision


def _allowed(plan: TaskPlan, action: str) -> bool:
    return any(rule.action == action and rule.max_attempts > 0 for rule in plan.recovery_rules)


def select_recovery(
    plan: TaskPlan,
    progress: TaskProgress,
    decision: CompletionDecision,
    *,
    report_available: bool = False,
    recovery_steps_remaining: int = 0,
    tool_calls_remaining: int = 0,
    budget_exhausted: bool = False,
) -> RecoveryAction | None:
    """Choose one deterministic next action, or None when the turn must stop."""

    if decision.status not in {"continue", "partial"}:
        return None
    if recovery_steps_remaining <= 0 or tool_calls_remaining <= 0 or budget_exhausted:
        return None
    missing = set(decision.missing_deliverables)
    scope = plan.delivery_scope
    if "compare.chapter_summary" in missing or "compare.index" in missing:
        if _allowed(plan, "compose_chapter_summary"):
            return RecoveryAction(
                action="compose_chapter_summary",
                params={"scope_key": scope.scope_key},
                reason="chapter_summary_missing",
                estimated_cost={"tool_calls": 0, "recovery_steps": 1},
            )
    if "compare.artifact" in missing:
        if _allowed(plan, "promote_from_report") and report_available:
            return RecoveryAction(
                action="promote_from_report",
                params={"scope_key": scope.scope_key},
                reason="artifact_missing",
                estimated_cost={"tool_calls": 0, "recovery_steps": 1},
            )
    if missing & {"compare.item_body", "compare.inline_diff"}:
        if _allowed(plan, "comparison_expand") and report_available:
            return RecoveryAction(
                action="comparison_expand",
                params={
                    "section_keys": list(scope.section_keys),
                    "chapter_keys": list(scope.chapter_keys),
                    "item_ids": list(scope.item_ids),
                    "table_keys": list(scope.table_keys),
                    "cursor": progress.coverage.next_cursor,
                },
                reason="comparison_items_missing",
                estimated_cost={"tool_calls": 1, "recovery_steps": 1},
            )
    if report_available and _allowed(plan, "load_frozen_report"):
        return RecoveryAction(
            action="load_frozen_report",
            params={"report_id": progress.coverage.report_id},
            reason="reload_frozen_report",
            estimated_cost={"tool_calls": 1, "recovery_steps": 1},
        )
    return None


__all__ = ["select_recovery"]
