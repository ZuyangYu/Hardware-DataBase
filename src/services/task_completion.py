"""Scope-aware completion gate for TaskPlan.

The gate is deterministic.  It evaluates the current DeliveryScope and the
server-owned coverage ledger; it never asks the model whether the task is
done, and it never treats a frozen report as proof that every requested item
was delivered.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.services.task_planning import RecoveryAction, TaskPlan
from src.services.task_progress import TaskProgress


CompletionStatus = Literal["continue", "complete", "complete_with_warnings", "partial", "blocked"]


class CompletionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CompletionStatus
    missing_deliverables: tuple[str, ...] = ()
    missing_item_ids: tuple[str, ...] = ()
    reason: str = ""
    safe_to_answer: bool = False
    report_complete: bool = False
    answer_complete: bool = False
    next_action: RecoveryAction | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)


def _chapter_complete(progress: TaskProgress) -> tuple[bool, tuple[str, ...], str]:
    chapters = progress.coverage.chapters
    if not chapters:
        return False, (), "no_chapter_coverage"
    missing = tuple(
        key for key, chapter in chapters.items() if chapter.state not in {"satisfied"}
    )
    return not missing, missing, "" if not missing else "chapter_summary_incomplete"


def _deliverable_satisfied(
    plan: TaskPlan,
    progress: TaskProgress,
    *,
    artifact_id: str,
    answer_text: str,
) -> tuple[bool, tuple[str, ...], str, tuple[str, ...]]:
    missing_deliverables: list[str] = []
    missing_items: list[str] = []
    reasons: list[str] = []
    for deliverable in plan.deliverables:
        if not deliverable.required:
            continue
        rule = deliverable.coverage_rule
        state = progress.deliverable_states.get(deliverable.deliverable_id)
        if state is not None and state.state == "satisfied":
            continue
        if rule.rule_kind == "one_per_chapter":
            ok, missing, reason = _chapter_complete(progress)
            if not ok:
                missing_deliverables.append(deliverable.deliverable_id)
                missing_items.extend(missing)
                reasons.append(reason)
            continue
        if rule.rule_kind == "all_scope_items":
            if not progress.coverage.required_item_ids:
                if progress.coverage.report_id:
                    # A frozen report with no changed items in this scope is a
                    # valid "no differences" result, not an empty delivery.
                    continue
                missing_deliverables.append(deliverable.deliverable_id)
                reasons.append("empty_required_scope")
                continue
            if progress.coverage.missing_item_ids:
                missing_deliverables.append(deliverable.deliverable_id)
                missing_items.extend(progress.coverage.missing_item_ids)
                reasons.append("scope_items_missing")
            continue
        if rule.rule_kind == "artifact_complete":
            if not artifact_id:
                missing_deliverables.append(deliverable.deliverable_id)
                reasons.append("artifact_missing")
            continue
        if rule.rule_kind == "citation_coverage":
            if rule.require_citation:
                required_items = set(progress.coverage.required_item_ids)
                cited_items = set(progress.coverage.cited_item_ids)
                missing_citations = required_items - cited_items
                if not cited_items:
                    missing_deliverables.append(deliverable.deliverable_id)
                    reasons.append("citations_missing")
                elif missing_citations:
                    missing_deliverables.append(deliverable.deliverable_id)
                    missing_items.extend(sorted(missing_citations))
                    reasons.append("scope_citations_missing")
            continue
        if rule.rule_kind == "answer_non_empty":
            if not str(answer_text or "").strip() and not (
                progress.answer_manifest and progress.answer_manifest.covered_item_ids
            ):
                missing_deliverables.append(deliverable.deliverable_id)
                reasons.append("answer_empty")
            continue
        if rule.rule_kind == "top_n_items":
            if progress.coverage.rendered_item_ids:
                continue
            missing_deliverables.append(deliverable.deliverable_id)
            reasons.append("top_n_missing")
    return (
        not missing_deliverables,
        tuple(dict.fromkeys(missing_items)),
        ",".join(dict.fromkeys(reasons)),
        tuple(dict.fromkeys(missing_deliverables)),
    )


class CompletionGate:
    """Evaluate one plan against one progress snapshot."""

    def evaluate(
        self,
        plan: TaskPlan,
        progress: TaskProgress,
        *,
        answer_text: str = "",
        artifact_id: str = "",
        warnings: tuple[str, ...] = (),
        next_action: RecoveryAction | None = None,
    ) -> CompletionDecision:
        if progress.status == "cancelled":
            return CompletionDecision(
                status="blocked",
                reason="cancelled",
                safe_to_answer=True,
                coverage=progress.coverage.snapshot(),
            )
        if plan.compile_errors:
            return CompletionDecision(
                status="blocked",
                reason=";".join(plan.compile_errors),
                safe_to_answer=True,
                coverage=progress.coverage.snapshot(),
            )
        satisfied, missing_items, reason, missing_deliverables = _deliverable_satisfied(
            plan, progress, artifact_id=artifact_id, answer_text=answer_text,
        )
        coverage = progress.coverage.snapshot()
        effective_warnings = tuple(dict.fromkeys((*warnings, *progress.coverage.warnings)))
        if satisfied:
            status: CompletionStatus = "complete_with_warnings" if effective_warnings else "complete"
            return CompletionDecision(
                status=status,
                reason="required_deliverables_satisfied",
                safe_to_answer=True,
                report_complete=bool(progress.coverage.report_id or artifact_id),
                answer_complete=True,
                coverage=coverage,
            )
        if next_action is not None and next_action.action != "stop":
            return CompletionDecision(
                status="continue",
                missing_deliverables=missing_deliverables,
                missing_item_ids=missing_items,
                reason=reason or "recovery_available",
                safe_to_answer=False,
                report_complete=bool(progress.coverage.report_id or artifact_id),
                next_action=next_action,
                coverage=coverage,
            )
        any_delivered = bool(
            progress.coverage.rendered_item_ids
            or progress.coverage.cited_item_ids
            or any(
                state.state not in {"pending"}
                for state in progress.deliverable_states.values()
            )
        )
        status = "partial" if any_delivered else "blocked"
        return CompletionDecision(
            status=status,
            missing_deliverables=missing_deliverables,
            missing_item_ids=missing_items,
            reason=reason or "required_deliverables_missing",
            safe_to_answer=True,
            report_complete=bool(progress.coverage.report_id or artifact_id),
            answer_complete=False,
            coverage=coverage,
        )


__all__ = ["CompletionDecision", "CompletionGate", "CompletionStatus"]
