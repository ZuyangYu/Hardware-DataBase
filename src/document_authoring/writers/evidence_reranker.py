"""LLM-as-judge evidence reranker for the authoring harness.

Reorders already-validated evidence by relevance to a requirement before it
reaches the writer, closing P6 (no rerank). Mirrors ``QueryRewriter``: it reuses
the shared ``LLMClient``, is gated by ``HarnessPolicy.allowed_tools``
(``rerank_evidence``) at injection time, and degrades to a verbatim pass-through
on any LLM/parse failure so old policies or unavailable LLMs never regress.
"""

from __future__ import annotations

import json
import logging

from typing import Any

from src.agents.claim_evidence import InformationRequirement
from src.core.llm_client import LLMClient
from src.document_authoring.writers.managed import _strip_code_fences


logger = logging.getLogger(__name__)


class EvidenceReranker:
    """Rerank validated evidence by requirement relevance using the LLM."""

    provider_id = "evidence_reranker"

    def __init__(self, client: LLMClient | None = None):
        self._client = client or LLMClient()

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
            response = self._client.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(requirement, evidence)},
                ],
                usage_stage="evidence_rerank",
                timeout=20,
                rate_limit_max_retries=0,
            )
            ordered = _apply_ranking(response, evidence)
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
