"""Research subagent (P3-002, flag gated, evaluation driven).

Single-delegation-level, server-scoped evidence collection for complex
multi-source questions.  The caller supplies the authorized evidence loader;
this module never widens knowledge-base/attachment access and never returns
unbounded internal reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import src.settings


def research_agent_enabled() -> bool:
    return bool(getattr(src.settings, "RESEARCH_AGENT_ENABLED", False))


@dataclass
class ResearchResult:
    findings: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    depth: int = 1

    def to_dict(self) -> dict:
        return {
            "findings": self.findings,
            "evidence_refs": self.evidence_refs,
            "conflicts": self.conflicts,
            "missing_information": self.missing_information,
        }


class ResearchAgent:
    def __init__(
        self,
        *,
        evidence_loader: Callable[[str], list[dict]] | None = None,
        max_depth: int = 1,
        enabled: bool | None = None,
    ) -> None:
        self.evidence_loader = evidence_loader
        self.max_depth = max(1, min(int(max_depth), 1))  # one delegation level only
        self.enabled = research_agent_enabled() if enabled is None else bool(enabled)

    def research(
        self,
        *,
        goal: str,
        scope: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        should_cancel=None,
    ) -> ResearchResult:
        if not self.enabled:
            raise RuntimeError("research agent is disabled (RESEARCH_AGENT_ENABLED=false)")
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("research goal must not be empty")
        if should_cancel is not None and should_cancel():
            return ResearchResult(missing_information=["research cancelled"])

        allowed_refs = {str(item) for item in (evidence_refs or [])}
        loaded: list[dict] = []
        if self.evidence_loader is not None:
            loaded = list(self.evidence_loader(goal) or [])
        findings: list[dict] = []
        refs: list[str] = []
        conflicts: list[str] = []
        missing: list[str] = []
        for item in loaded:
            if should_cancel is not None and should_cancel():
                break
            ref = str(item.get("id") or item.get("evidence_id") or "")
            if allowed_refs and ref not in allowed_refs:
                # Frozen scope: never widen access beyond the parent request.
                continue
            findings.append(
                {
                    "title": str(item.get("title") or item.get("source_name") or ""),
                    "summary": str(item.get("content") or "")[:400],
                    "source": str(item.get("source_name") or ""),
                }
            )
            if ref:
                refs.append(ref)
            if item.get("conflict") or item.get("conflicts_with"):
                conflicts.append(str(item.get("conflict") or item.get("conflicts_with")))
        if not findings:
            missing.append("no evidence matched the research scope")
        return ResearchResult(
            findings=findings,
            evidence_refs=sorted(set(refs)),
            conflicts=sorted(set(conflicts)),
            missing_information=missing,
            depth=self.max_depth,
        )
