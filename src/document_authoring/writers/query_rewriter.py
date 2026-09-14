"""LLM-backed query rewriter for harness retrieval.

Produces one rewritten query string per requirement to improve recall when
schema terminology differs from document terminology. Any LLM or parsing
failure degrades to ``None`` so the caller falls back to the original query.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from src.agents.claim_evidence import InformationRequirement
from src.core.chat_model_runtime import ChatModelLike, invoke_structured
from src.core.model_factory import create_chat_model
from src.document_authoring.writers.managed import _strip_code_fences


logger = logging.getLogger(__name__)


class QueryRewritePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewrite: str = Field(min_length=1)


class QueryRewriter:
    """Rewrite a retrieval query using the shared chat-model runtime."""

    provider_id = "query_rewriter"

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

    def rewrite(self, requirement: InformationRequirement) -> str | None:
        prompt = _build_prompt(requirement)
        try:
            result = invoke_structured(
                self._get_model(),
                QueryRewritePayload,
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                operation="query_rewrite",
                profile="authoring_auxiliary",
                text_fallback=lambda text: {"rewrite": _parse_rewrite(text) or ""},
            )
        except Exception as exc:
            logger.warning(
                "QueryRewriter LLM call failed for %s: %s",
                requirement.requirement_id, exc,
            )
            return None
        return result.value.rewrite.strip() or None


_SYSTEM_PROMPT = """You rewrite a retrieval query to improve recall in a hardware knowledge base.

Return ONE rewritten query string that covers synonyms, entity aliases, and
alternative phrasings of the requirement. Output ONLY the query text, or a
JSON object {"rewrite": "<query>"}. No explanation, no markdown fences."""


def _build_prompt(requirement: InformationRequirement) -> str:
    parts = [f"subject: {requirement.subject or ''}"]
    if requirement.predicate:
        parts.append(f"predicate: {requirement.predicate}")
    if requirement.required_capabilities:
        parts.append(f"capabilities: {', '.join(requirement.required_capabilities)}")
    parts.append("Return one rewritten query string.")
    return "\n".join(parts)


def _parse_rewrite(response: str) -> str | None:
    if not response:
        return None
    cleaned = _strip_code_fences(response).strip()
    if not cleaned:
        return None
    # Try JSON first: {"rewrite": "..."} or a bare JSON string.
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned or None
    if isinstance(parsed, dict):
        value = str(parsed.get("rewrite") or "").strip()
        return value or None
    if isinstance(parsed, str):
        return parsed.strip() or None
    return None
