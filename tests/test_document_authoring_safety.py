from __future__ import annotations

import pytest

from src.document_authoring.models import (
    DocumentUnitDraft,
    TemplateUnitBinding,
    TemplateVersion,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)


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
        validation_status="supported",
    )

    with pytest.raises(ValueError, match="scalar.*one target"):
        DocumentGenerationService._semantic_fills(
            _template(),
            [draft],
            {draft.unit_id: "ready_to_render"},
            {"summary": binding},
        )
