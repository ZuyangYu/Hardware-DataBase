"""Deterministic conflict handling; models may propose claims but never decide policy."""

from __future__ import annotations

from .models import ConflictCase, ConflictDecision


class ConflictPolicy:
    def __init__(self, *, version: str = "conflict-policy-v1"):
        self.version = version

    def evaluate(self, case: ConflictCase, *, purpose: str) -> ConflictDecision:
        purpose = str(purpose or "answer").strip().casefold()
        if purpose == "compare":
            return ConflictDecision(
                conflict_id=case.conflict_id,
                action="report_difference",
                requires_human=False,
                blocking=False,
                rationale="peer comparison preserves each source declaration",
                policy_version=self.version,
            )
        approved_primary = [
            claim
            for claim in case.claims
            if claim.source_role == "primary"
            and claim.approval_status in {"approved", "effective", "released"}
        ]
        if len(approved_primary) == 1:
            return ConflictDecision(
                conflict_id=case.conflict_id,
                action="retain_primary",
                requires_human=False,
                blocking=False,
                selected_claim_id=approved_primary[0].claim_id,
                rationale="approved primary source remains the target fact; references are retained as alternatives",
                policy_version=self.version,
            )
        return ConflictDecision(
            conflict_id=case.conflict_id,
            action="choose_claim",
            requires_human=True,
            blocking=True,
            rationale="no single approved primary fact determines the target value",
            policy_version=self.version,
        )


__all__ = ["ConflictPolicy"]
