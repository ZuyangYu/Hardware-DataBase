"""Non-mutating mapping consent preparation and explicit review application."""

from __future__ import annotations

from typing import Any
from collections import defaultdict

from src.document_authoring.table_contracts import repair_repeating_table_targets
from src.document_authoring.template_activation import decide_template_activation
from src.document_authoring.template_analysis import TemplateAnalysisSuggestion, TemplateMappingCorrection

_CONSENT = "允许替换模板示例数据"


def prepare_mapping_review(service: Any, ctx: Any, template_id: str) -> dict[str, Any] | None:
    """Build a safe, durable review payload without writing or activating."""
    stored = service.store.get_template_analysis(template_id)
    if stored is None:
        stored = service.store.get_template_analysis_by_id(template_id)
    if stored is None:
        raise KeyError(f"template analysis not found: {template_id}")
    analysis = service.get_template_analysis_for_review(ctx, analysis_id=stored.analysis_id)
    if analysis.status == "failed" or not analysis.suggestions:
        return None
    suggestions = [
        repair_repeating_table_targets(analysis, suggestion)
        for suggestion in analysis.suggestions
    ]
    target_ids = [unit_id for suggestion in suggestions for unit_id in suggestion.target_unit_ids]
    units = {unit.unit_id: unit for unit in analysis.units}
    overwrite_ids = [
        unit_id for unit_id in target_ids
        if units[unit_id].value_kind not in {"blank", "formula"}
        and units[unit_id].writable
    ]
    candidate = analysis.model_copy(update={
        "status": "ready_for_confirmation",
        "suggestions": suggestions,
        "human_confirmed_target_unit_ids": sorted(set(target_ids)),
        "approved_overwrite_unit_ids": sorted(set(overwrite_ids)),
    })
    decision = decide_template_activation(candidate)
    if decision.status != "auto_accepted":
        return None
    template = service.store.get_template(analysis.template_version_id)
    if template is None:
        raise KeyError(f"template version not found: {analysis.template_version_id}")
    ranges = _target_ranges(suggestions, units)
    examples = [
        {"unit_id": unit_id, "value": units[unit_id].value_preview}
        for unit_id in overwrite_ids[:3]
    ]
    return {
        "analysis_id": analysis.analysis_id,
        "template_version_id": analysis.template_version_id,
        "content_hash": analysis.content_hash,
        "suggestions": [item.model_dump(mode="json") for item in suggestions],
        "target_unit_ids": sorted(set(target_ids)),
        "overwrite_unit_ids": sorted(set(overwrite_ids)),
        "counts": {
            "targets": len(set(target_ids)),
            "sample_overwrites": len(set(overwrite_ids)),
        },
        "decision": decision.model_dump(mode="json"),
        "summary": (
            f"将填充 {len(set(target_ids))} 个模板位置；"
            f"其中 {len(set(overwrite_ids))} 个已有示例数据需要明确允许替换。"
        ),
        "scope": {"ranges": ranges, "existing_examples": examples},
    }


def apply_mapping_review(service: Any, ctx: Any, review: dict[str, Any]) -> Any:
    """Apply only a hash-bound review after the exact user consent phrase."""
    if review.get("consent") != _CONSENT:
        raise PermissionError("explicit template example replacement consent is required")
    analysis = service.get_template_analysis_for_review(ctx, analysis_id=str(review.get("analysis_id", "")))
    template = service.store.get_template(analysis.template_version_id)
    if template is None:
        raise KeyError(f"template version not found: {analysis.template_version_id}")
    if (
        analysis.template_version_id != review.get("template_version_id")
        or analysis.content_hash != review.get("content_hash")
    ):
        raise ValueError("stale template mapping review")
    suggestions = [
        TemplateAnalysisSuggestion.model_validate(payload)
        for payload in review.get("suggestions", [])
    ]
    target_ids = {unit_id for suggestion in suggestions for unit_id in suggestion.target_unit_ids}
    if target_ids != set(review.get("target_unit_ids", [])):
        raise ValueError("template mapping review targets do not match its payload")
    overwrite_ids = set(review.get("overwrite_unit_ids", []))
    if not overwrite_ids <= target_ids:
        raise ValueError("template mapping review overwrite IDs are not targets")
    corrected = service.correct_template_analysis(
        ctx,
        correction=TemplateMappingCorrection(
            analysis_id=analysis.analysis_id,
            expected_content_hash=analysis.content_hash,
            suggestions=suggestions,
            locked_unit_ids=list(analysis.locked_unit_ids),
            approved_overwrite_unit_ids=sorted(overwrite_ids),
            actor_id=ctx.user_id,
            comment="用户明确允许替换模板示例数据",
        ),
    )
    if corrected.template_version_id != analysis.template_version_id:
        raise ValueError("template mapping correction changed template version")
    return service.confirm_template_analysis(
        ctx,
        analysis_id=corrected.analysis_id,
        display_name=template.template_id,
    )


def _target_ranges(suggestions: list[Any], units: dict[str, Any]) -> list[str]:
    grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
    from src.document_authoring.template_analysis import workbook_cell_coordinates
    for suggestion in suggestions:
        for unit_id in suggestion.target_unit_ids:
            unit = units.get(unit_id)
            if not unit:
                continue
            sheet = unit.locator.get("sheet_name")
            cell = unit.locator.get("cell")
            if not sheet or not cell:
                continue
            try:
                grouped[str(sheet)].append(workbook_cell_coordinates(str(cell)))
            except ValueError:
                continue
    result = []
    for sheet, coordinates in grouped.items():
        columns = [column for column, _row in coordinates]
        rows = [row for _column, row in coordinates]
        def col_name(number: int) -> str:
            value = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                value = chr(65 + remainder) + value
            return value
        result.append(f"{sheet}!{col_name(min(columns))}{min(rows)}:{col_name(max(columns))}{max(rows)}")
    return result
