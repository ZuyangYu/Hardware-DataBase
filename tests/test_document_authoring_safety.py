from __future__ import annotations

import pytest

from src.document_authoring.models import (
    DocumentUnitDraft,
    TemplateUnitBinding,
    TemplateVersion,
    TypedFieldValue,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)
from src.document_authoring.validator import DocumentValidator


def _template() -> TemplateVersion:
    return TemplateVersion(
        template_version_id="template-1",
        template_id="template",
        format="xlsx",
        content_hash="a" * 64,
        template_schema_id="schema-1",
        template_schema_version="1",
        renderer_policy_id="renderer-1",
    )


def _unit(cell: str, placeholder: str) -> TemplateAnalysisUnit:
    return TemplateAnalysisUnit(
        unit_id=f"sheet:Review!{cell}",
        locator={"sheet_name": "Review", "cell": cell},
        label=f"Review!{cell}",
        writable=True,
        value_preview=placeholder,
        value_kind="text",
        value_hash=f"hash-{cell}",
        style_fingerprint="style-1",
        structural_role_hint="placeholder",
    )


def test_region_binding_rejects_a_scalar_with_multiple_workbook_targets():
    units = [_unit("A1", "{{first}}"), _unit("B1", "{{second}}")]
    analysis = TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="a" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=units,
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="summary",
            label="Summary",
            target_unit_ids=[unit.unit_id for unit in units],
            confidence=0.99,
        )],
    )

    with pytest.raises(ValueError, match="scalar.*one target"):
        DocumentGenerationService._regions_and_bindings(_template(), analysis)


def test_region_binding_freezes_placeholder_baseline_and_overwrite_permission():
    unit = _unit("A1", "{{summary}}")
    analysis = TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="a" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=[unit],
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="summary",
            label="Summary",
            target_unit_ids=[unit.unit_id],
            confidence=0.99,
        )],
    )

    regions, bindings = DocumentGenerationService._regions_and_bindings(
        _template(),
        analysis,
    )

    assert len(regions) == len(bindings) == 1
    assert regions[0].expected_value_hash == "hash-A1"
    assert regions[0].allow_nonempty_overwrite is True


def test_fill_plan_rejects_an_existing_multitarget_workbook_binding():
    binding = TemplateUnitBinding(
        binding_id="binding-1",
        template_schema_id="schema-1",
        template_schema_version="1",
        semantic_unit_type="field",
        semantic_unit_id="summary",
        target_region_ids=["region-a", "region-b"],
    )
    draft = DocumentUnitDraft(
        unit_id="field:summary",
        run_id="run-1",
        generated_by="managed_writer",
        proposed_value="One scalar",
        typed_value=TypedFieldValue(
            kind="scalar",
            normalized_values=["One scalar"],
            display_value="One scalar",
            evidence_ids=["e1"],
        ),
        validation_status="supported",
    )

    with pytest.raises(ValueError, match="scalar.*one target"):
        DocumentGenerationService._semantic_fills(
            _template(),
            [draft],
            {draft.unit_id: "ready_to_render"},
            {"summary": binding},
        )


def test_fill_plan_ignores_supported_draft_without_typed_value():
    binding = TemplateUnitBinding(
        binding_id="binding-1",
        template_schema_id="schema-1",
        template_schema_version="1",
        semantic_unit_type="field",
        semantic_unit_id="summary",
        target_region_ids=["region-a"],
    )
    draft = DocumentUnitDraft(
        unit_id="field:summary",
        run_id="run-1",
        generated_by="managed_writer",
        proposed_value="Unsafe full evidence text",
        validation_status="supported",
    )

    fills = DocumentGenerationService._semantic_fills(
        _template(),
        [draft],
        {draft.unit_id: "ready_to_render"},
        {"summary": binding},
    )

    assert fills.fills == []


def test_typed_scalar_with_conflicting_values_is_unsupported():
    draft = DocumentUnitDraft(
        unit_id="field:current",
        run_id="run-1",
        generated_by="managed_writer",
        proposed_value="12 A / 15 A",
        evidence_ids=["e1", "e2"],
        typed_value=TypedFieldValue(
            kind="scalar",
            normalized_values=["12 A", "15 A"],
            display_value="12 A / 15 A",
            evidence_ids=["e1", "e2"],
        ),
    )

    validated = DocumentValidator().validate_typed_field_draft(
        draft,
        {"e1": {"id": "e1", "content": "12 A"}, "e2": {"id": "e2", "content": "15 A"}},
        expected_value_type="scalar",
    )

    assert validated.validation_status == "unsupported"
    assert any("unique normalized value" in note for note in validated.validation_notes)


def test_typed_enumeration_is_deduplicated_and_low_confidence_evidence_is_rejected():
    draft = DocumentUnitDraft(
        unit_id="field:modes",
        run_id="run-1",
        generated_by="managed_writer",
        evidence_ids=["e1"],
        typed_value=TypedFieldValue(
            kind="enumeration",
            normalized_values=["Normal", "normal", "Sleep"],
            display_value="Normal, Sleep",
            evidence_ids=["e1"],
        ),
    )

    accepted = DocumentValidator().validate_typed_field_draft(
        draft,
        {"e1": {"id": "e1", "content": "Normal, Sleep", "metadata": {}}},
        expected_value_type="enumeration",
    )
    rejected = DocumentValidator().validate_typed_field_draft(
        draft,
        {"e1": {"id": "e1", "content": "Normal, Sleep", "metadata": {"low_confidence": True}}},
        expected_value_type="enumeration",
    )

    assert accepted.validation_status == "supported"
    assert accepted.typed_value.normalized_values == ["Normal", "Sleep"]
    assert rejected.validation_status == "unsupported"
    assert "non-auto-fill evidence" in rejected.validation_notes[0]


def test_fill_plan_uses_typed_display_value_instead_of_draft_prose():
    binding = TemplateUnitBinding(
        binding_id="binding-1",
        template_schema_id="schema-1",
        template_schema_version="1",
        semantic_unit_type="field",
        semantic_unit_id="summary",
        target_region_ids=["region-a"],
    )
    draft = DocumentUnitDraft(
        unit_id="field:summary",
        run_id="run-1",
        generated_by="managed_writer",
        proposed_value="The source says that the value is 10 A.",
        typed_value=TypedFieldValue(
            kind="scalar",
            normalized_values=["10 A"],
            display_value="10 A",
            evidence_ids=["e1"],
        ),
        validation_status="supported",
    )

    fills = DocumentGenerationService._semantic_fills(
        _template(),
        [draft],
        {draft.unit_id: "ready_to_render"},
        {"summary": binding},
    )

    assert fills.fills[0].value == "10 A"
