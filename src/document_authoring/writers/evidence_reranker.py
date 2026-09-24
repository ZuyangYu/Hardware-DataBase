"""LLM-as-judge evidence reranker for the authoring harness.

Reorders already-validated evidence by relevance to a requirement before it
reaches the writer, closing P6 (no rerank). Mirrors ``QueryRewriter``: it uses
the shared chat-model runtime, is gated by ``HarnessPolicy.allowed_tools``
(``rerank_evidence``) at injection time, and degrades to a verbatim
pass-through on any LLM/parse failure so old policies or unavailable LLMs never
regress.
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
from src.document_authoring.writers.managed import _strip_code_fences


logger = logging.getLogger(__name__)


class EvidenceRankingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indices: list[int] = Field(default_factory=list)


class EvidenceReranker:
    """Rerank validated evidence by requirement relevance using a chat model."""

    provider_id = "evidence_reranker"

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

    def rerank(
        self,
        requirement: InformationRequirement,
        evidence: list[dict[str, Any]],
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return ``evidence`` reordered by relevance; never drops evidence.

        ``top_k`` truncates the reordered result when given. Any LLM or parsing
        failure returns the evidence in its original order (pass-through).
        """
        # Nothing to rank (and no LLM call to spend) for trivial inputs.
        if len(evidence) <= 1:
            return list(evidence)
        try:
            result = invoke_structured(
                self._get_model(),
                EvidenceRankingPayload,
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(requirement, evidence)},
                ],
                operation="evidence_rerank",
                profile="authoring_auxiliary",
                text_fallback=_parse_text_ranking,
            )
            ordered = _apply_ranking_indices(result.value.indices, evidence)
        except Exception as exc:
            logger.warning(
                "EvidenceReranker failed for %s: %s",
                requirement.requirement_id, exc,
            )
            return list(evidence)
        if top_k is not None:
            return ordered[:top_k]
        return ordered


_SYSTEM_PROMPT = """You rerank evidence chunks by relevance to a hardware document requirement.

Return ONLY a JSON array of 0-based evidence indices, ordered most-relevant
first. Include every index exactly once. No explanation, no markdown fences."""


def _build_prompt(requirement: InformationRequirement, evidence: list[dict[str, Any]]) -> str:
    lines = [f"subject: {requirement.subject or ''}"]
    if requirement.predicate:
        lines.append(f"predicate: {requirement.predicate}")
    if requirement.required_capabilities:
        lines.append(f"capabilities: {', '.join(requirement.required_capabilities)}")
    lines.append("")
    lines.append("evidence (index | id | content):")
    for idx, ev in enumerate(evidence):
        content = str(ev.get("content") or "")[:500]
        lines.append(f"[{idx}] {ev.get('id')}: {content}")
    lines.append("")
    lines.append("Return a JSON array of the indices, most relevant first.")
    return "\n".join(lines)


def _as_index(item: Any, count: int) -> int | None:
    """Coerce a parsed JSON value to a valid 0-based index, else None."""
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        idx = item
    elif isinstance(item, float) and item.is_integer():
        idx = int(item)
    else:
        return None
    return idx if 0 <= idx < count else None


def _apply_ranking(response: str, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder ``evidence`` by the LLM's index array; append unreferenced items."""
    cleaned = _strip_code_fences(response).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("rerank response is not a list")
    return _apply_ranking_indices(parsed, evidence)


def _parse_text_ranking(response: str) -> dict[str, list[int]]:
    cleaned = _strip_code_fences(str(response or "")).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("rerank response is not a list")
    return {"indices": parsed}


def _apply_ranking_indices(parsed: list[Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply validated or compatibility indices while preserving all evidence."""
    ordered_idx: list[int] = []
    seen: set[int] = set()
    for item in parsed:
        idx = _as_index(item, len(evidence))
        if idx is None or idx in seen:
            continue
        seen.add(idx)
        ordered_idx.append(idx)
    # Referenced evidence in LLM order, then any unreferenced evidence in
    # original order so nothing is ever silently dropped.
    return [evidence[i] for i in ordered_idx] + [
        evidence[i] for i in range(len(evidence)) if i not in seen
    ]
