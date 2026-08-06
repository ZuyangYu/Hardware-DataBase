"""Deterministic safety policy for automatic template activation."""

from __future__ import annotations

from dataclasses import dataclass

from src.document_authoring.template_analysis import (
    TemplateActivationDecision,
    TemplateAnalysis,
    TemplateRiskMetrics,
)


@dataclass(frozen=True)
class TemplateActivationPolicy:
    min_mapping_confidence: float = 0.90
    max_target_ratio: float = 0.20
    max_nonempty_overwrite_ratio: float = 0.0


def decide_template_activation(
    analysis: TemplateAnalysis,
    policy: TemplateActivationPolicy | None = None,
) -> TemplateActivationDecision:
    """Classify a model proposal without granting the model policy authority."""
    effective = policy or TemplateActivationPolicy()
    unit_by_id = {unit.unit_id: unit for unit in analysis.units}
    target_ids = [
        unit_id
        for suggestion in analysis.suggestions
        for unit_id in suggestion.target_unit_ids
    ]
    target_units = [unit_by_id[unit_id] for unit_id in target_ids if unit_id in unit_by_id]
    nonempty_target_count = sum(unit.value_kind != "blank" for unit in target_units)
    total_unit_count = len(analysis.units)
    target_count = len(target_ids)
    metrics = TemplateRiskMetrics(
        total_unit_count=total_unit_count,
        target_count=target_count,
        nonempty_target_count=nonempty_target_count,
        target_ratio=target_count / total_unit_count if total_unit_count else 0.0,
        nonempty_overwrite_ratio=(
            nonempty_target_count / target_count if target_count else 0.0
        ),
        min_confidence=min(
            (suggestion.confidence for suggestion in analysis.suggestions),
            default=None,
        ),
    )
    reasons: list[str] = []

    def reject(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if analysis.status == "requires_human":
        reject("missing_semantic_context")
    if analysis.mapping_conflict_unit_ids:
        reject("mapping_conflict")

    seen_targets: set[str] = set()
    for suggestion in analysis.suggestions:
        if suggestion.value_shape == "scalar" and len(suggestion.target_unit_ids) != 1:
            reject("scalar_target_fanout")
        if suggestion.value_shape == "repeating_table":
            reject("repeating_table_requires_schema")
        if suggestion.confidence < effective.min_mapping_confidence:
            reject("low_mapping_confidence")
        for unit_id in suggestion.target_unit_ids:
            if unit_id in seen_targets:
                reject("mapping_conflict")
            seen_targets.add(unit_id)
            unit = unit_by_id.get(unit_id)
            if unit is None or not unit.writable:
                reject("mapping_conflict")
                continue
            if unit_id in analysis.locked_unit_ids:
                reject("mapping_conflict")
                continue
            if analysis.format == "docx":
                continue
            if unit.value_kind == "formula":
                reject("formula_target")
            if unit.structural_role_hint == "fixed_label":
                reject("fixed_label_target")
            if unit.structural_role_hint == "table_header":
                reject("table_header_target")
            if unit.structural_role_hint == "layout_blank":
                reject("layout_blank_target")
            if unit.structural_role_hint == "unknown":
                reject("missing_semantic_context")
            allowed_overwrite = (
                unit.structural_role_hint == "placeholder"
                or (
                    unit.structural_role_hint == "scalar_input"
                    and unit.candidate_for_auto_fill
                    and unit.value_kind == "blank"
                )
                or (
                    unit.structural_role_hint == "sample_value"
                    and suggestion.overwrite_basis == "sample_value"
                    and unit_id in analysis.approved_overwrite_unit_ids
                )
            )
            if not allowed_overwrite:
                reject("nonempty_target_not_placeholder")

    if analysis.format != "docx" and target_count:
        risky_targets = [
            unit
            for unit in target_units
            if not (
                unit.structural_role_hint == "placeholder"
                or (
                    unit.structural_role_hint == "scalar_input"
                    and unit.candidate_for_auto_fill
                    and unit.value_kind == "blank"
                )
            )
        ]
        risky_target_ratio = (
            len(risky_targets) / total_unit_count if total_unit_count else 0.0
        )
        risky_nonempty_ratio = (
            sum(unit.value_kind != "blank" for unit in risky_targets)
            / len(risky_targets)
            if risky_targets
            else 0.0
        )
        if risky_target_ratio > effective.max_target_ratio:
            reject("destructive_target_ratio")
        if risky_nonempty_ratio > effective.max_nonempty_overwrite_ratio:
            reject("destructive_target_ratio")

    return TemplateActivationDecision(
        status="requires_human" if reasons else "auto_accepted",
        reason_codes=reasons,
        suggestion_ids=[
            suggestion.semantic_unit_id for suggestion in analysis.suggestions
        ],
        metrics=metrics,
    )
