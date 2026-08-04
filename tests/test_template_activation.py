from __future__ import annotations

from src.document_authoring.template_activation import decide_template_activation
from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)


def _analysis(
    *,
    unit: TemplateAnalysisUnit | None = None,
    suggestion: TemplateAnalysisSuggestion | None = None,
) -> TemplateAnalysis:
    target = unit or TemplateAnalysisUnit(
        unit_id="sheet:Review!B1",
        locator={"sheet_name": "Review", "cell": "B1"},
        label="Review!B1",
        writable=True,
        value_preview="{{project_summary}}",
        value_kind="text",
        style_fingerprint="style-1",
        structural_role_hint="placeholder",
    )
    proposal = suggestion or TemplateAnalysisSuggestion(
        semantic_unit_id="project_summary",
        label="Project Summary",
        target_unit_ids=[target.unit_id],
        retrieval_terms=["project summary"],
        confidence=0.95,
    )
    return TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="a" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=[target],
        suggestions=[proposal],
    )


def test_explicit_single_scalar_placeholder_is_auto_accepted():
    decision = decide_template_activation(_analysis())

    assert decision.status == "auto_accepted"
    assert decision.reason_codes == []
    assert decision.metrics.target_count == 1


def test_multitarget_scalar_requires_human_review():
    analysis = _analysis()
    second = analysis.units[0].model_copy(update={
        "unit_id": "sheet:Review!C1",
        "locator": {"sheet_name": "Review", "cell": "C1"},
    })
    analysis.units.append(second)
    analysis.suggestions[0].target_unit_ids.append(second.unit_id)

    decision = decide_template_activation(analysis)

    assert decision.status == "requires_human"
    assert "scalar_target_fanout" in decision.reason_codes


def test_layout_blank_requires_human_review():
    unit = TemplateAnalysisUnit(
        unit_id="sheet:Review!B1",
        locator={"sheet_name": "Review", "cell": "B1"},
        writable=True,
        value_kind="blank",
        style_fingerprint="style-1",
        structural_role_hint="layout_blank",
    )

    decision = decide_template_activation(_analysis(unit=unit))

    assert "layout_blank_target" in decision.reason_codes


def test_nonempty_fixed_label_requires_human_review():
    unit = TemplateAnalysisUnit(
        unit_id="sheet:Review!A1",
        locator={"sheet_name": "Review", "cell": "A1"},
        writable=True,
        value_preview="Project",
        value_kind="text",
        style_fingerprint="style-1",
        structural_role_hint="fixed_label",
    )

    decision = decide_template_activation(_analysis(unit=unit))

    assert "nonempty_target_not_placeholder" in decision.reason_codes


def test_repeating_table_requires_explicit_schema():
    suggestion = TemplateAnalysisSuggestion(
        semantic_unit_id="interfaces",
        label="Interfaces",
        target_unit_ids=["sheet:Review!B1"],
        confidence=0.99,
        value_shape="repeating_table",
    )

    decision = decide_template_activation(_analysis(suggestion=suggestion))

    assert "repeating_table_requires_schema" in decision.reason_codes


def test_low_confidence_requires_human_review():
    suggestion = TemplateAnalysisSuggestion(
        semantic_unit_id="project_summary",
        label="Project Summary",
        target_unit_ids=["sheet:Review!B1"],
        confidence=0.89,
    )

    decision = decide_template_activation(_analysis(suggestion=suggestion))

    assert "low_mapping_confidence" in decision.reason_codes


def test_coordinate_only_unit_requires_human_review():
    unit = TemplateAnalysisUnit(
        unit_id="sheet:Review!B1",
        locator={"sheet_name": "Review", "cell": "B1"},
        label="Review!B1",
        writable=True,
    )

    decision = decide_template_activation(_analysis(unit=unit))

    assert "missing_semantic_context" in decision.reason_codes


def test_duplicate_targets_are_reported_as_mapping_conflict():
    analysis = _analysis()
    analysis.suggestions.append(TemplateAnalysisSuggestion(
        semantic_unit_id="duplicate",
        label="Duplicate",
        target_unit_ids=[analysis.units[0].unit_id],
        confidence=0.99,
    ))

    decision = decide_template_activation(analysis)

    assert "mapping_conflict" in decision.reason_codes


def test_one_placeholder_cannot_hide_a_destructive_mixed_target_ratio():
    units = [
        TemplateAnalysisUnit(
            unit_id=f"sheet:Review!A{index}",
            locator={"sheet_name": "Review", "cell": f"A{index}"},
            writable=True,
            value_preview=(
                "{{safe_placeholder}}" if index == 1 else f"Fixed label {index}"
            ),
            value_kind="text",
            style_fingerprint="style-1",
            structural_role_hint="placeholder" if index == 1 else "fixed_label",
        )
        for index in range(1, 11)
    ]
    analysis = TemplateAnalysis(
        analysis_id="analysis-mixed",
        template_version_id="template-mixed",
        content_hash="a" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=units,
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id=f"field-{index}",
                label=f"Field {index}",
                target_unit_ids=[unit.unit_id],
                confidence=0.99,
            )
            for index, unit in enumerate(units[:5])
        ],
    )

    decision = decide_template_activation(analysis)

    assert "destructive_target_ratio" in decision.reason_codes


def test_explicit_placeholders_are_not_counted_as_destructive_targets():
    units = [
        TemplateAnalysisUnit(
            unit_id=f"sheet:Review!A{index}",
            locator={"sheet_name": "Review", "cell": f"A{index}"},
            writable=True,
            value_preview=f"{{{{field_{index}}}}}",
            value_kind="text",
            style_fingerprint="style-1",
            structural_role_hint="placeholder",
        )
        for index in range(1, 6)
    ]
    analysis = TemplateAnalysis(
        analysis_id="analysis-placeholders",
        template_version_id="template-placeholders",
        content_hash="a" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=units,
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id=f"field-{index}",
                label=f"Field {index}",
                target_unit_ids=[unit.unit_id],
                confidence=0.99,
            )
            for index, unit in enumerate(units)
        ],
    )

    decision = decide_template_activation(analysis)

    assert decision.status == "auto_accepted"
    assert "destructive_target_ratio" not in decision.reason_codes
