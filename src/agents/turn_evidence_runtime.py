"""Bind the deterministic evidence policy, comparison obligations and budget.

This module owns the turn-level decision so ``runner.py`` stays an
orchestration shell and the budget/policy code remains unit-testable.
"""

from __future__ import annotations

from typing import Any, Iterable

import src.settings as settings
from src.services.evidence_budget import EvidenceBudgetCoordinator
from src.services.evidence_policy import policy_for_purpose
from src.services.turn_evidence_plan import TurnEvidencePlan


def configure_turn_evidence(
    *,
    rt: Any,
    conversation_plan: Any,
    intent_plan: Any,
    thread_id: str,
    knowledge_scope: Any,
    ctx: Any,
    attachment_refs: Iterable[Any],
    effective_scope: str,
    has_attachments: bool,
    has_kb: bool,
) -> Any:
    """Resolve purpose, comparison obligations and the source-aware budget."""

    evidence_purpose = str(
        getattr(conversation_plan, "evidence_purpose", "")
        or getattr(intent_plan, "intent", "qa")
    )
    policy = policy_for_purpose(
        evidence_purpose,
        source_scope=effective_scope,
        has_attachments=has_attachments,
        has_kb=has_kb,
    )
    rt.evidence_policy = policy
    if rt.turn_evidence_plan is not None:
        rt.turn_evidence_plan.purpose = policy.purpose

    selected_kb_ids = (
        [source.kb_id for source in knowledge_scope.sources]
        if knowledge_scope is not None else []
    )
    if not selected_kb_ids:
        metadata = dict(getattr(ctx, "metadata", {}) or {})
        if metadata.get("kb_id"):
            selected_kb_ids = [int(metadata["kb_id"])]
    attachment_ids = [
        str(getattr(ref, "attachment_id", "") or "") for ref in attachment_refs
    ]

    if (
        policy.purpose == "compare"
        and effective_scope == "attachment_and_knowledge_base"
    ):
        attachment_reserve = int(
            getattr(settings, "EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES", 4000) or 0
        )
        kb_reserve = int(
            getattr(settings, "EVIDENCE_KNOWLEDGE_BASE_REQUIRED_RESERVE_BYTES", 4000) or 0
        )
        # Whole-document comparison has exactly two obligations: one
        # attachment side and one KB side.  Replacing any target-level plan
        # prevents unrelated per-record obligations from making a completed
        # comparison look incomplete.
        rt.turn_evidence_plan = TurnEvidencePlan.from_comparison_sides(
            turn_id=str(thread_id or ""),
            attachment_ids=attachment_ids,
            kb_ids=selected_kb_ids,
            selected_kb_ids=selected_kb_ids,
            attachment_reserve_bytes=attachment_reserve,
            kb_reserve_bytes=kb_reserve,
        )

    if (
        bool(getattr(settings, "EVIDENCE_SOURCE_AWARE_ENABLED", False))
        and effective_scope == "attachment_and_knowledge_base"
    ):
        coordinator = EvidenceBudgetCoordinator.from_settings(
            item_limit=int(getattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 24) or 24),
            required_sources=("attachment", "knowledge_base"),
            include_comparison_reserve=policy.purpose == "compare",
        )
        if rt.turn_evidence_plan is not None:
            coordinator.bind_requirements(rt.turn_evidence_plan.required)
            rt.turn_evidence_plan.source_budget_provider = coordinator.snapshot
        rt.evidence_budget_coordinator = coordinator

    from src.services import task_settings

    if task_settings.TASK_PLANNING_ENABLED and policy.purpose in {
        "qa", "summarize", "extract", "compare",
    }:
        from src.services.task_runtime import create_compare_task

        create_compare_task(
            rt,
            query=str(getattr(rt, "question", "") or ""),
            turn_id=str(thread_id or ""),
            evidence_purpose=policy.purpose,
            source_scope_fingerprint=str(
                getattr(knowledge_scope, "selection_hash", "") or effective_scope
            ),
            has_attachments=bool(attachment_refs),
            has_kb=knowledge_scope is not None,
            comparison_available=bool(attachment_refs) and (
                knowledge_scope is not None or len(attachment_refs) >= 2
            ),
        )
    return policy


__all__ = ["configure_turn_evidence"]
