"""Bind the evidence policy and source-aware budget for one turn.

The model owns intent and tool choice.  This module only applies the
deterministic admission policy to whatever tools the model actually calls.
"""

from __future__ import annotations

from typing import Any

import src.settings as settings
from src.services.evidence_budget import EvidenceBudgetCoordinator
from src.services.evidence_policy import policy_for_purpose


def configure_turn_evidence(
    *,
    rt: Any,
    conversation_plan: Any,
    intent_plan: Any,
    effective_scope: str,
    has_attachments: bool,
    has_kb: bool,
) -> Any:
    """Apply a bounded evidence policy; never pre-select a comparison."""

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

    if (
        bool(getattr(settings, "EVIDENCE_SOURCE_AWARE_ENABLED", False))
        and effective_scope == "attachment_and_knowledge_base"
    ):
        coordinator = EvidenceBudgetCoordinator.from_settings(
            item_limit=int(getattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 24) or 24),
            required_sources=("attachment", "knowledge_base"),
        )
        if rt.turn_evidence_plan is not None:
            coordinator.bind_requirements(rt.turn_evidence_plan.required)
            rt.turn_evidence_plan.source_budget_provider = coordinator.snapshot
        rt.evidence_budget_coordinator = coordinator

    return policy


__all__ = ["configure_turn_evidence"]
