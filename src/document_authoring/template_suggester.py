"""Constrained LLM suggestions over an already-safe template inventory."""

from __future__ import annotations

import json
import logging
from typing import Protocol

from src.core.llm_client import LLMClient
from src.document_authoring.template_analysis import TemplateAnalysis, TemplateAnalysisSuggestion


logger = logging.getLogger(__name__)

_SUGGESTION_FIELDS = {
    "semantic_unit_id", "label", "target_unit_ids", "retrieval_terms", "confidence",
}
_SYSTEM_PROMPT = """You map a safe template structural inventory to proposed semantic units.
Return only a JSON array. Each item must contain exactly these keys:
semantic_unit_id, label, target_unit_ids, retrieval_terms, confidence.
Only use target_unit_ids that appear in the provided inventory and are writable.
Do not return Markdown, explanations, locations, or new locators."""


class TemplateSuggestionProvider(Protocol):
    def suggest(self, analysis: TemplateAnalysis) -> list[TemplateAnalysisSuggestion]: ...


class LLMTemplateSuggestionProvider:
    """Suggest bindings without providing the model any OOXML bytes or paths."""

    def __init__(self, client: LLMClient):
        self._client = client

    def suggest(self, analysis: TemplateAnalysis) -> list[TemplateAnalysisSuggestion]:
        if analysis.status != "ready_for_confirmation":
            return []
        try:
            response = self._client.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": analysis.model_dump_json()},
                ],
                usage_stage="template_analysis",
            )
            raw_suggestions = json.loads(response)
            suggestions = _parse_suggestions(raw_suggestions)
            candidate = analysis.model_copy(update={"suggestions": suggestions})
            candidate.validate_suggestions()
        except (Exception,) as exc:
            # The analysis model has a status contract but no free-form error
            # payload.  Emit the actionable reason to the application's
            # operator log and force the persisted analysis through review.
            logger.error("Template suggestions require human review: %s", exc)
            analysis.status = "requires_human"
            analysis.suggestions = []
            return []
        analysis.suggestions = suggestions
        return suggestions


def _parse_suggestions(value: object) -> list[TemplateAnalysisSuggestion]:
    if not isinstance(value, list):
        raise ValueError("LLM suggestion response must be a JSON array")
    return [_parse_suggestion(item) for item in value]


def _parse_suggestion(value: object) -> TemplateAnalysisSuggestion:
    if not isinstance(value, dict) or set(value) != _SUGGESTION_FIELDS:
        raise ValueError("LLM suggestion item must have exactly the required fields")
    semantic_unit_id = value["semantic_unit_id"]
    label = value["label"]
    target_unit_ids = value["target_unit_ids"]
    retrieval_terms = value["retrieval_terms"]
    confidence = value["confidence"]
    if not isinstance(semantic_unit_id, str) or not semantic_unit_id:
        raise ValueError("semantic_unit_id must be a non-empty string")
    if not isinstance(label, str) or not label:
        raise ValueError("label must be a non-empty string")
    if not isinstance(target_unit_ids, list) or not all(isinstance(item, str) and item for item in target_unit_ids):
        raise ValueError("target_unit_ids must be a list of non-empty strings")
    if not isinstance(retrieval_terms, list) or not all(isinstance(item, str) for item in retrieval_terms):
        raise ValueError("retrieval_terms must be a list of strings")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise ValueError("confidence must be a number")
    return TemplateAnalysisSuggestion(
        semantic_unit_id=semantic_unit_id,
        label=label,
        target_unit_ids=target_unit_ids,
        retrieval_terms=retrieval_terms,
        confidence=float(confidence),
    )
