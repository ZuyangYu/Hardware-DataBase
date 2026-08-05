"""Constrained LLM suggestions over an already-safe template inventory."""

from __future__ import annotations

import json
import logging
from typing import Protocol

from src.core.llm_client import LLMClient
from src.document_authoring.template_analysis import TemplateAnalysis, TemplateAnalysisSuggestion
from src.document_authoring.template_progress import TemplateProgressCallback


logger = logging.getLogger(__name__)


class TemplateSuggestionTechnicalFailure(Exception):
    """The automatic template-upload suggestion path failed for a technical reason.

    Raised by the service so the sanitized draft can be preserved for audit
    instead of being deleted or silently accepted.
    """

    def __init__(self, message: str, *, template_version_id: str | None = None):
        super().__init__(message)
        self.template_version_id = template_version_id

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

    def suggest(
        self,
        analysis: TemplateAnalysis,
        *,
        progress_callback: TemplateProgressCallback | None = None,
    ) -> list[TemplateAnalysisSuggestion]:
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
            suggestions = _resolve_duplicate_targets(suggestions)
            candidate = analysis.model_copy(update={"suggestions": suggestions})
            candidate.validate_suggestions()
        except PermissionError as exc:
            # Suggesting a protected (non-writable) unit is a policy violation,
            # not a malformed response.  Abort so the sanitized draft is
            # preserved for audit instead of being silently deferred to review.
            raise TemplateSuggestionTechnicalFailure(
                "suggestion targeted a non-writable analysis unit",
                template_version_id=analysis.template_version_id,
            ) from exc
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


def _resolve_duplicate_targets(
    suggestions: list[TemplateAnalysisSuggestion],
) -> list[TemplateAnalysisSuggestion]:
    """Keep at most one suggestion per target unit, preferring higher confidence.

    The LLM may echo the same writable unit in several candidates; binding them
    all would produce contradictory writes.  A single winner per unit wins, and
    each kept suggestion is trimmed to the units it actually won so the result
    stays unique under ``validate_suggestions``.
    """
    best_by_unit: dict[str, TemplateAnalysisSuggestion] = {}
    for suggestion in suggestions:
        for unit_id in suggestion.target_unit_ids:
            current = best_by_unit.get(unit_id)
            if current is None or suggestion.confidence > current.confidence:
                best_by_unit[unit_id] = suggestion
    kept: list[TemplateAnalysisSuggestion] = []
    for suggestion in suggestions:
        won = [unit_id for unit_id in suggestion.target_unit_ids if best_by_unit[unit_id] is suggestion]
        if not won:
            continue
        if len(won) != len(suggestion.target_unit_ids):
            suggestion = suggestion.model_copy(update={"target_unit_ids": won})
        kept.append(suggestion)
    return kept


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
