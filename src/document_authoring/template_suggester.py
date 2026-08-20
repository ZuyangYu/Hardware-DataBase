"""Constrained LLM suggestions over an already-safe template inventory."""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from src.core.llm_client import LLMClient
from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)
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
# 大模板（数百可写单元）一次性交给 LLM 会让输出超过 max_tokens 被截断成非法 JSON，
# 进而整体回退到 requires_human。按固定批量分块调用、逐块解析合并，使每块输出
# 远小于 max_tokens，单块失败只丢该块而非整份分析。
_SUGGESTION_CHUNK_SIZE = 50
_FUNCTION_HEADER_RE = re.compile(
    r"(?:\bfunction\b|\bdescription\b|功能描述|功能说明|功能)",
    re.IGNORECASE,
)
_SYSTEM_PROMPT = """You analyze a safe template structural inventory and decide how each proposed semantic unit will be generated.
You receive one CHUNK of writable units. For each unit that is a field to be filled during document generation, judge its semantic meaning from its label, value preview, neighborhood, and style.
Return only a JSON array. Each item must contain exactly these keys:
semantic_unit_id, label, target_unit_ids, retrieval_terms, confidence, overwrite_basis.
For workbook (xlsx/xlsm) units, target_unit_ids must contain exactly one entry: the unit_id of that same unit, and each semantic_unit_id must be unique per unit.
For docx units, target_unit_ids may list multiple region unit_ids that share one semantic meaning.
retrieval_terms must be the most effective keywords to retrieve that unit's content from the knowledge base; they drive content acquisition during generation.
Only use target_unit_ids that appear in the provided inventory.
overwrite_basis must be "placeholder" when the target is a placeholder, "sample_value" only
when the target is an example value that may be replaced, or null otherwise.
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
        writable_units = [unit for unit in analysis.units if unit.writable]
        # Layout blanks are intentionally excluded from workbook prompts when
        # the analyzer has already identified safe fill candidates. This keeps
        # large sheets within the model budget and prevents the model from
        # inventing mappings for decorative cells.
        candidate_units = [
            unit for unit in writable_units
            if unit.candidate_for_auto_fill
            or unit.structural_role_hint == "placeholder"
            or (
                unit.structural_role_hint == "sample_value"
                and unit.candidate_for_auto_fill
            )
        ]
        if analysis.format != "docx" and candidate_units:
            writable_units = candidate_units
        restrict_workbook_targets = analysis.format != "docx" and bool(candidate_units)
        deterministic_table_suggestions = _deterministic_function_table_suggestions(analysis)
        if (
            deterministic_table_suggestions
            and analysis.format != "docx"
            and not any(unit.structural_role_hint == "placeholder" for unit in candidate_units)
        ):
            # A pure table-body template has a deterministic semantic binding;
            # avoid spending model time rediscovering a column role that the
            # analyzer already established. The selected model remains used by
            # retrieval/writing, where it adds value beyond cell coordinates.
            analysis.suggestions = deterministic_table_suggestions
            analysis.approved_overwrite_unit_ids = []
            return deterministic_table_suggestions
        if not writable_units:
            analysis.suggestions = []
            return []
        unit_by_id = {unit.unit_id: unit for unit in analysis.units}
        chunks = [
            writable_units[start:start + _SUGGESTION_CHUNK_SIZE]
            for start in range(0, len(writable_units), _SUGGESTION_CHUNK_SIZE)
        ]
        collected: list[TemplateAnalysisSuggestion] = []
        chunk_failures: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            try:
                response = self._client.chat(
                    [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": _chunk_payload(analysis, chunk)},
                    ],
                    usage_stage="template_analysis",
                    # Template mapping is a bounded, recoverable preflight. Do
                    # not inherit the long document-generation timeout here;
                    # a slow model must fall back to the deterministic table
                    # mapper instead of holding the upload request open.
                    timeout=60,
                )
                parsed = _parse_suggestions(json.loads(response))
                for suggestion in parsed:
                    valid_targets: list[str] = []
                    for unit_id in suggestion.target_unit_ids:
                        unit = unit_by_id.get(unit_id)
                        if unit is None:
                            # 幻觉目标（不在清单内）：丢弃该目标，不致整批失败。
                            continue
                        if not unit.writable:
                            # 非可写目标是策略违规，必须中止并保留审计草稿，
                            # 而非静默降级到 requires_human。
                            raise PermissionError(
                                f"suggestion targets non-writable analysis unit: {unit_id}"
                            )
                        if restrict_workbook_targets and not _safe_workbook_suggestion_target(
                            unit, suggestion.overwrite_basis
                        ):
                            # The model may see a nearby sample or layout cell,
                            # but only analyzer-confirmed candidates can enter
                            # a workbook binding.
                            continue
                        valid_targets.append(unit_id)
                    if not valid_targets:
                        continue
                    if len(valid_targets) != len(suggestion.target_unit_ids):
                        suggestion = suggestion.model_copy(update={"target_unit_ids": valid_targets})
                    collected.append(suggestion)
            except PermissionError as exc:
                # Suggesting a protected (non-writable) unit is a policy violation,
                # not a malformed response.  Abort so the sanitized draft is
                # preserved for audit instead of being silently deferred to review.
                raise TemplateSuggestionTechnicalFailure(
                    "suggestion targeted a non-writable analysis unit",
                    template_version_id=analysis.template_version_id,
                ) from exc
            except (Exception,) as exc:
                # 单块失败（超时/截断/非法 JSON）不应让整份大模板回退人工：
                # 记录后继续其余分块，只要有一块成功就能产出可用建议。
                logger.warning(
                    "Template suggestion chunk %d/%d failed, continuing: %s",
                    index, len(chunks), exc,
                )
                chunk_failures.append(f"chunk {index}: {exc}")
                continue
        suggestions = _resolve_duplicate_targets(collected)
        fallback_suggestions = deterministic_table_suggestions
        covered_targets = {
            target_id
            for suggestion in suggestions
            for target_id in suggestion.target_unit_ids
        }
        suggestions.extend(
            suggestion
            for suggestion in fallback_suggestions
            if not set(suggestion.target_unit_ids) & covered_targets
        )
        if analysis.format != "docx":
            # 工作簿标量映射在 confirm 阶段要求每条建议恰好 1 个 target
            # （_regions_and_bindings），把多目标建议拆成单目标以保证可激活。
            suggestions = _split_multi_target(suggestions)
        if not suggestions:
            logger.error(
                "Template suggestions require human review: %d chunk(s) failed, 0 suggestions: %s",
                len(chunk_failures), chunk_failures,
            )
            analysis.status = "requires_human"
            analysis.suggestions = []
            return []
        try:
            candidate = analysis.model_copy(update={"suggestions": suggestions})
            candidate.validate_suggestions()
        except PermissionError as exc:
            raise TemplateSuggestionTechnicalFailure(
                "suggestion targeted a non-writable analysis unit",
                template_version_id=analysis.template_version_id,
            ) from exc
        except (Exception,) as exc:
            logger.error("Template suggestions require human review (validation failed): %s", exc)
            analysis.status = "requires_human"
            analysis.suggestions = []
            return []
        analysis.suggestions = suggestions
        # Only server-classified sample values with the matching declared basis
        # may receive an automatic overwrite approval.  The LLM can propose a
        # basis but cannot grant itself authority over a cell.
        analysis.approved_overwrite_unit_ids = sorted(
            {
                unit_id
                for suggestion in suggestions
                for unit_id in suggestion.target_unit_ids
                if (
                    unit_by_id[unit_id].structural_role_hint == "sample_value"
                    and suggestion.overwrite_basis == "sample_value"
                )
            }
        )
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


def _safe_workbook_suggestion_target(
    unit: TemplateAnalysisUnit,
    overwrite_basis: str | None,
) -> bool:
    if unit.structural_role_hint in {"placeholder"}:
        return True
    if unit.structural_role_hint == "scalar_input":
        return unit.value_kind == "blank" and unit.candidate_for_auto_fill
    if unit.structural_role_hint == "sample_value":
        return unit.candidate_for_auto_fill and overwrite_basis == "sample_value"
    return False


def _deterministic_function_table_suggestions(
    analysis: TemplateAnalysis,
) -> list[TemplateAnalysisSuggestion]:
    """Recover safe function-description bindings when the LLM is unavailable.

    This is deliberately narrow: it only handles blank scalar-input cells that
    the analyzer classified below a header containing Function/Description or
    its Chinese equivalents. It never targets nonempty sample values or layout
    blanks, and it derives retrieval terms only from the bounded neighborhood.
    """
    if analysis.format == "docx":
        return []
    function_headers: dict[tuple[str, int], str] = {}
    for header in analysis.units:
        if (
            header.structural_role_hint != "table_header"
            or not header.value_preview
            or not _FUNCTION_HEADER_RE.search(header.value_preview)
        ):
            continue
        sheet_name = str(header.locator.get("sheet_name") or "")
        column = _cell_column_index(str(header.locator.get("cell") or ""))
        if sheet_name and column is not None:
            function_headers[(sheet_name, column)] = header.value_preview
    suggestions: list[TemplateAnalysisSuggestion] = []
    for unit in analysis.units:
        if (
            not unit.writable
            or unit.structural_role_hint != "scalar_input"
            or unit.value_kind != "blank"
            or not unit.candidate_for_auto_fill
        ):
            continue
        neighborhood_values = [
            neighbor.value_preview
            for neighbor in unit.neighborhood
            if neighbor.value_preview
        ]
        headers = [value for value in neighborhood_values if _FUNCTION_HEADER_RE.search(value)]
        sheet_name = str(unit.locator.get("sheet_name") or "")
        column = _cell_column_index(str(unit.locator.get("cell") or ""))
        if not headers and sheet_name and column is not None:
            header = function_headers.get((sheet_name, column))
            if header:
                headers = [header]
        if not headers:
            continue
        terms: list[str] = []
        for value in [*headers, *neighborhood_values]:
            if value and value not in terms:
                terms.append(value)
        suggestions.append(TemplateAnalysisSuggestion(
            semantic_unit_id=f"field:{unit.unit_id}",
            label=unit.label,
            target_unit_ids=[unit.unit_id],
            retrieval_terms=terms[:8],
            confidence=0.90,
        ))
    return suggestions


def _cell_column_index(cell: str) -> int | None:
    match = re.match(r"^([A-Z]+)[1-9][0-9]*$", cell.upper())
    if not match:
        return None
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def _parse_suggestions(value: object) -> list[TemplateAnalysisSuggestion]:
    if not isinstance(value, list):
        raise ValueError("LLM suggestion response must be a JSON array")
    suggestions: list[TemplateAnalysisSuggestion] = []
    for index, item in enumerate(value):
        try:
            suggestions.append(_parse_suggestion(item))
        except (ValueError, TypeError) as exc:
            # 容错：单条建议字段不符只跳过该条，不让一条畸形输出拖垮整块。
            logger.warning("Skipping malformed LLM suggestion item %d: %s", index, exc)
    return suggestions


def _coerce_string_list(value: object) -> list[str]:
    """Heal common LLM format drift on list-of-strings fields.

    A bare string is treated as a one-element list and non-string members are
    dropped, so a stray number/object in an otherwise-valid suggestion does not
    invalidate the whole item.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _parse_suggestion(value: object) -> TemplateAnalysisSuggestion:
    if not isinstance(value, dict):
        raise ValueError("LLM suggestion item must be an object")
    missing = _SUGGESTION_FIELDS - set(value)
    if missing:
        raise ValueError(f"LLM suggestion item missing fields: {sorted(missing)}")
    # 多出的字段直接忽略，避免 LLM 偶发附加键导致整批解析失败。
    semantic_unit_id = value["semantic_unit_id"]
    label = value["label"]
    target_unit_ids = value["target_unit_ids"]
    retrieval_terms = value["retrieval_terms"]
    confidence = value["confidence"]
    overwrite_basis = value.get("overwrite_basis")
    if not isinstance(semantic_unit_id, str) or not semantic_unit_id:
        raise ValueError("semantic_unit_id must be a non-empty string")
    if not isinstance(label, str) or not label:
        raise ValueError("label must be a non-empty string")
    if not isinstance(target_unit_ids, list) or not all(isinstance(item, str) and item for item in target_unit_ids):
        raise ValueError("target_unit_ids must be a list of non-empty strings")
    # retrieval_terms 是常见格式漂移点（模型有时输出单个字符串而非列表）：
    # 归一化修复而不是丢弃整条建议，避免大模板因格式漂移损失字段。
    retrieval_terms = _coerce_string_list(value["retrieval_terms"])
    if isinstance(confidence, str):
        try:
            confidence = float(confidence)
        except ValueError:
            raise ValueError("confidence must be a number")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise ValueError("confidence must be a number")
    if overwrite_basis not in {None, "placeholder", "sample_value"}:
        raise ValueError("overwrite_basis must be placeholder, sample_value, or null")
    return TemplateAnalysisSuggestion(
        semantic_unit_id=semantic_unit_id,
        label=label,
        target_unit_ids=target_unit_ids,
        retrieval_terms=retrieval_terms,
        confidence=float(confidence),
        overwrite_basis=overwrite_basis,
    )


def _unit_inventory(unit: TemplateAnalysisUnit) -> dict[str, object]:
    """Project one unit down to the binary-free structural fields the LLM needs."""
    return {
        "unit_id": unit.unit_id,
        "label": unit.label,
        "value_preview": unit.value_preview,
        "value_kind": unit.value_kind,
        "structural_role_hint": unit.structural_role_hint,
        "neighborhood": [
            {"value_preview": neighbor.value_preview} for neighbor in unit.neighborhood
        ],
    }


def _chunk_payload(analysis: TemplateAnalysis, chunk: list[TemplateAnalysisUnit]) -> str:
    """Serialize one chunk of writable units as a safe, binary-free inventory.

    Includes template_version_id/content_hash so the model can anchor the chunk
    without ever seeing OOXML bytes or filesystem paths.
    """
    payload = {
        "template_version_id": analysis.template_version_id,
        "content_hash": analysis.content_hash,
        "format": analysis.format,
        "units": [_unit_inventory(unit) for unit in chunk],
    }
    return json.dumps(payload, ensure_ascii=False)


def _split_multi_target(
    suggestions: list[TemplateAnalysisSuggestion],
) -> list[TemplateAnalysisSuggestion]:
    """Split scalar suggestions that won multiple targets into one-per-target.

    Workbook confirm (_regions_and_bindings) requires each scalar suggestion to
    own exactly one target cell.  When the LLM groups several cells under one
    semantic id, split them into single-target suggestions with unique ids so
    activation can still proceed instead of failing on scalar_target_fanout.
    """
    split: list[TemplateAnalysisSuggestion] = []
    for suggestion in suggestions:
        if len(suggestion.target_unit_ids) <= 1:
            split.append(suggestion)
            continue
        for unit_id in suggestion.target_unit_ids:
            split.append(suggestion.model_copy(update={
                "semantic_unit_id": f"{suggestion.semantic_unit_id}::{unit_id}",
                "target_unit_ids": [unit_id],
            }))
    return split
