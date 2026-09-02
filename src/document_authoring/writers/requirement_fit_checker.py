"""LLM-as-judge requirement-fit checker for the authoring harness.

Decides whether a draft actually answers its requirement (P10). Mirrors
``EvidenceReranker``: it uses the shared chat-model runtime, is gated by
``HarnessPolicy.allowed_tools`` (``requirement_fit_check``) at injection time,
and degrades to a pass verdict on any LLM/parse failure so old policies or
unavailable LLMs never spuriously mark a draft ``requires_human``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agents.claim_evidence import InformationRequirement
from src.core.chat_model_runtime import ChatModelLike, invoke_structured
from src.core.model_factory import create_chat_model
from src.document_authoring.models import DocumentUnitDraft
from src.document_authoring.writers.managed import _strip_code_fences


logger = logging.getLogger(__name__)

_PASS: dict[str, Any] = {"fit": True, "reason": "fit check unavailable"}


class RequirementFitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fit: bool
    reason: str = Field(default="", max_length=500)


class RequirementFitChecker:
    """Judge whether a draft answers its requirement using a chat model."""

    provider_id = "requirement_fit_checker"

    def __init__(
        self,
        model: ChatModelLike | None = None,
        *,
        model_factory: Callable[[], ChatModelLike] | None = None,
    ):
        self._model = model
        self._model_factory = model_factory

    def _get_model(self) -> ChatModelLike:
        if self._model is None:
            self._model = (
                self._model_factory()
                if self._model_factory is not None
                else create_chat_model(profile="authoring_auxiliary")
            )
        return self._model

    def check(self, draft: DocumentUnitDraft, requirement: InformationRequirement) -> dict[str, Any]:
        """Return ``{'fit': bool, 'reason': str}``; degrades to pass on failure."""
        try:
            result = invoke_structured(
                self._get_model(),
                RequirementFitPayload,
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(draft, requirement)},
                ],
                operation="requirement_fit_check",
                profile="authoring_auxiliary",
                text_fallback=_parse_text_verdict,
            )
            return {
                "fit": result.value.fit,
                "reason": result.value.reason.strip() or ("fit" if result.value.fit else "unfit"),
            }
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


def _parse_text_verdict(response: str) -> dict[str, Any]:
    parsed = _parse_verdict(response)
    if parsed.get("reason") == _PASS["reason"] and parsed.get("fit") is True:
        raise ValueError("fit response is not a valid structured verdict")
    return parsed
