"""LLM-as-judge requirement-fit checker for the authoring harness.

Decides whether a draft actually answers its requirement (P10). Mirrors
``EvidenceReranker``: reuses the shared ``LLMClient``, is gated by
``HarnessPolicy.allowed_tools`` (``requirement_fit_check``) at injection time,
and degrades to a pass verdict on any LLM/parse failure so old policies or
unavailable LLMs never spuriously mark a draft ``requires_human``.
"""

from __future__ import annotations

import json
import logging

from typing import Any

from src.agents.claim_evidence import InformationRequirement
from src.core.llm_client import LLMClient
from src.document_authoring.models import DocumentUnitDraft
from src.document_authoring.writers.managed import _strip_code_fences


logger = logging.getLogger(__name__)

_PASS: dict[str, Any] = {"fit": True, "reason": "fit check unavailable"}


class RequirementFitChecker:
    """Judge whether a draft answers its requirement using the LLM."""

    provider_id = "requirement_fit_checker"

    def __init__(self, client: LLMClient | None = None):
        self._client = client or LLMClient()

    def check(self, draft: DocumentUnitDraft, requirement: InformationRequirement) -> dict[str, Any]:
        """Return ``{'fit': bool, 'reason': str}``; degrades to pass on failure."""
        try:
            response = self._client.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(draft, requirement)},
                ],
                usage_stage="requirement_fit_check",
                timeout=20,
            )
            return _parse_verdict(response)
        except Exception as exc:
            logger.warning(
                "RequirementFitChecker failed for %s: %s",
                requirement.requirement_id, exc,
            )
            return dict(_PASS)


_SYSTEM_PROMPT = """You judge whether a drafted document field answers its requirement.

Return ONLY a JSON object: {"fit": <true|false>, "reason": "<short explanation>"}
fit=true means the draft substantively answers the requirement; false means it
does not (e.g. off-topic, missing the asked value). No markdown fences, no prose."""


def _build_prompt(draft: DocumentUnitDraft, requirement: InformationRequirement) -> str:
    lines = [f"requirement subject: {requirement.subject or ''}"]
    if requirement.predicate:
        lines.append(f"requirement predicate: {requirement.predicate}")
    lines.append("")
    lines.append("draft content:")
    lines.append(str(draft.content or "")[:1000])
    return "\n".join(lines)


def _parse_verdict(response: str) -> dict[str, Any]:
    cleaned = _strip_code_fences(response).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        return dict(_PASS)
    fit = parsed.get("fit")
    if not isinstance(fit, bool):
        return dict(_PASS)
    reason = str(parsed.get("reason") or "").strip()
    return {"fit": fit, "reason": reason or ("fit" if fit else "unfit")}
