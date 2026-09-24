"""Reviewer subagent (P3-001, flag gated, evaluation driven).

Responsibilities:
- deterministic citation/number/required-fact checks first;
- optional model pass for semantic issues (unsupported claims, conflicts);
- never modifies artifacts and never bypasses a release gate.

Disabled unless ``REVIEWER_AGENT_ENABLED`` is true; results are advisory and
must be persisted by the caller, not written into authoritative Memory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import src.settings

REVIEWER_VERSION = "reviewer-v1"
_CITATION_RE = re.compile(r"\[(\d+)\]")
_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def reviewer_agent_enabled() -> bool:
    return bool(getattr(src.settings, "REVIEWER_AGENT_ENABLED", False))


@dataclass
class ReviewIssue:
    code: str
    severity: str  # info | warn | fail
    message: str
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    status: str  # pass | warn | fail
    issues: list[ReviewIssue] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    evidence_conflicts: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    reviewer_version: str = REVIEWER_VERSION

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "evidence_refs": issue.evidence_refs,
                }
                for issue in self.issues
            ],
            "unsupported_claims": self.unsupported_claims,
            "evidence_conflicts": self.evidence_conflicts,
            "recommendations": self.recommendations,
            "reviewer_version": self.reviewer_version,
        }


class ReviewerAgent:
    def __init__(self, model=None, *, enabled: bool | None = None) -> None:
        self.model = model
        self.enabled = reviewer_agent_enabled() if enabled is None else bool(enabled)

    def review(
        self,
        *,
        document_text: str,
        evidence: list[dict],
        required_facts: list[str] | None = None,
        forbidden_claims: list[str] | None = None,
        should_cancel=None,
    ) -> ReviewResult:
        if not self.enabled:
            raise RuntimeError("reviewer agent is disabled (REVIEWER_AGENT_ENABLED=false)")

        def cancelled() -> bool:
            return bool(should_cancel is not None and should_cancel())

        text = str(document_text or "")
        issues: list[ReviewIssue] = []
        recommendations: list[str] = []

        citation_count = len(evidence)
        unknown = sorted(
            {int(match.group(1)) for match in _CITATION_RE.finditer(text) if int(match.group(1)) > citation_count}
        )
        if unknown:
            issues.append(
                ReviewIssue(
                    code="unknown_citation",
                    severity="fail",
                    message=f"citations reference evidence that does not exist: {unknown}",
                )
            )
        if not evidence and text.strip():
            issues.append(
                ReviewIssue(
                    code="no_evidence",
                    severity="fail",
                    message="document contains claims but no evidence was supplied",
                )
            )

        lowered = text.lower()
        for fact in required_facts or []:
            if str(fact).strip() and str(fact).strip().lower() not in lowered:
                issues.append(
                    ReviewIssue(
                        code="missing_required_fact",
                        severity="fail",
                        message=f"required fact not found: {fact}",
                    )
                )
        for claim in forbidden_claims or []:
            if str(claim).strip() and str(claim).strip().lower() in lowered:
                issues.append(
                    ReviewIssue(
                        code="forbidden_claim_present",
                        severity="fail",
                        message=f"forbidden claim present: {claim}",
                    )
                )

        unsupported: list[str] = []
        conflicts: list[str] = []
        if self.model is not None and not cancelled():
            semantic = self._semantic_review(text=text, evidence=evidence)
            unsupported = semantic.get("unsupported_claims", [])
            conflicts = semantic.get("evidence_conflicts", [])
            for claim in unsupported:
                issues.append(
                    ReviewIssue(code="unsupported_claim", severity="warn", message=claim)
                )
            if not semantic.get("available", True):
                recommendations.append("semantic review unavailable; re-run before release")

        status = "pass"
        if any(issue.severity == "fail" for issue in issues):
            status = "fail"
        elif any(issue.severity == "warn" for issue in issues) or recommendations:
            status = "warn"
        return ReviewResult(
            status=status,
            issues=issues,
            unsupported_claims=list(unsupported),
            evidence_conflicts=list(conflicts),
            recommendations=recommendations,
        )

    def _semantic_review(self, *, text: str, evidence: list[dict]) -> dict:
        """Optional bounded model pass; failures degrade to ``available=false``."""
        try:
            from src.core.chat_model_runtime import invoke_text

            prompt = (
                "你是硬件设计文档审查员。仅返回 JSON："
                '{"unsupported_claims": [], "evidence_conflicts": []}。'
                "unsupported_claims 列出文档中没有证据支持的结论；"
                "evidence_conflicts 列出证据之间互相矛盾的结论。\n\n"
                f"文档:\n{text[:6000]}\n\n证据:\n"
                + json.dumps(
                    [{"id": item.get("id"), "content": str(item.get("content") or "")[:400]} for item in evidence[:20]],
                    ensure_ascii=False,
                )
            )
            result = invoke_text(
                self.model,
                [{"role": "user", "content": prompt}],
                operation="reviewer_review",
                profile="reviewer",
            )
            payload = json.loads(result.text)
            return {
                "available": True,
                "unsupported_claims": [str(item) for item in payload.get("unsupported_claims", [])],
                "evidence_conflicts": [str(item) for item in payload.get("evidence_conflicts", [])],
            }
        except Exception:
            return {"available": False}


def review_document(
    *,
    document_text: str,
    evidence: list[dict],
    model=None,
    required_facts: list[str] | None = None,
    forbidden_claims: list[str] | None = None,
    should_cancel=None,
) -> ReviewResult | None:
    """Convenience entry point; returns None when the feature is disabled."""
    if not reviewer_agent_enabled():
        return None
    return ReviewerAgent(model).review(
        document_text=document_text,
        evidence=evidence,
        required_facts=required_facts,
        forbidden_claims=forbidden_claims,
        should_cancel=should_cancel,
    )
