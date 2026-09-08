from __future__ import annotations

from src.document_authoring.generation_sessions import GenerationBrief
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    TemplateUnitBinding,
    WorkbookTableColumnSchema,
    WorkbookTableSchema,
)
from src.document_authoring.planning.legacy import legacy_brief_to_output_spec
from src.document_authoring.planning.models import OutputSpec
from src.document_authoring.planning.registry import build_builtin_registries
from src.document_authoring.planning.service import DocumentPlanningService, LegacyTemplatePlanningAdapter
from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)


def _schema() -> DocumentSchema:
    return DocumentSchema(
        document_schema_id="schema-1",
        version="1",
        document_type="icd",
        status="approved",
        fields=[
            DocumentFieldSchema(
                field_id="product",
                label="Product",
                required=True,
                retrieval_policy_id="retrieval-product",
                verification_policy_id="verify-product",
                required_capabilities=["entity_lookup"],
            ),
            DocumentFieldSchema(
                field_id="pins",
                label="Pin Definition",
                required=True,
                value_type="table",
                retrieval_policy_id="retrieval-pins",
                verification_policy_id="verify-pins",
                required_capabilities=["tabular_lookup"],
                table_columns={"A": "connector", "B": "pin", "C": "signal"},
            ),
        ],
    )


def _analysis() -> TemplateAnalysis:
    return TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="template-hash",
        format="xlsx",
        status="ready_for_confirmation",
        units=[
            TemplateAnalysisUnit(
                unit_id="product-cell",
                locator={"sheet_name": "Review", "cell": "B2"},
                label="Product",
                writable=True,
            ),
            TemplateAnalysisUnit(
                unit_id="pins-A2",
                locator={"sheet_name": "Review", "cell": "A2"},
                label="connector",
                writable=True,
            ),
            TemplateAnalysisUnit(
                unit_id="pins-B2",
                locator={"sheet_name": "Review", "cell": "B2"},
                label="pin",
                writable=True,
            ),
        ],
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id="product",
                label="Product",
                target_unit_ids=["product-cell"],
                confidence=1,
            ),
            TemplateAnalysisSuggestion(
                semantic_unit_id="pins",
                label="Pin Definition",
                target_unit_ids=["pins-A2", "pins-B2"],
                confidence=1,
                value_shape="repeating_table",
            ),
        ],
    )


def _bindings() -> list[TemplateUnitBinding]:
    return [
        TemplateUnitBinding(
            binding_id="binding-product",
            template_schema_id="schema-1",
            template_schema_version="1",
            semantic_unit_type="field",
            semantic_unit_id="product",
            target_region_ids=["product-cell"],
        ),
        TemplateUnitBinding(
            binding_id="binding-pins",
            template_schema_id="schema-1",
            template_schema_version="1",
            semantic_unit_type="field",
            semantic_unit_id="pins",
            target_region_ids=["pins-A2", "pins-B2"],
            table_schema=WorkbookTableSchema(
                table_region_id="pins-region",
                semantic_unit_id="pins",
                sheet_name="Review",
                header_row=1,
                first_data_row=2,
                last_template_row=20,
                style_source_row=2,
                max_output_rows=19,
                columns=[
                    WorkbookTableColumnSchema(column_id="connector", label="connector", column_letter="A"),
                    WorkbookTableColumnSchema(column_id="pin", label="pin", column_letter="B"),
                ],
            ),
        ),
    ]


def _output_spec() -> OutputSpec:
    return legacy_brief_to_output_spec(
        GenerationBrief(
            purpose="create an ICD",
            scope={
                "project": "ADAS",
                "row_scope": "all_selected_pins",
                "row_keys": ["J1:1", "J1:2"],
            },
        ),
        template_version_id="template-1",
        template_schema_id="schema-1",
        template_schema_version="1",
        document_type="icd",
        target_format="xlsx",
        output_spec_id="spec-1",
        document_schema=_schema(),
    )


def test_legacy_brief_conversion_is_one_way_and_preserves_template_shape():
    spec = _output_spec()
    assert spec.layout_source.mode == "provided_template"
    assert spec.artifact.deliverables[0].format == "xlsx"
    assert spec.purpose == "create an ICD"
    assert [unit.unit_id for unit in spec.outline] == ["product", "pins"]
    assert spec.table_requirements[0].required_columns == ["connector", "pin", "signal"]


def test_shadow_compiler_is_deterministic_and_keeps_physical_bindings_without_content():
    adapter = LegacyTemplatePlanningAdapter(registries=build_builtin_registries())
    first = adapter.compile(
        output_spec=_output_spec(),
        document_schema=_schema(),
        template_analysis=_analysis(),
        bindings=_bindings(),
        source_snapshot_id="snapshot-1",
        source_snapshot_hash="snapshot-hash",
    )
    second = adapter.compile(
        output_spec=_output_spec(),
        document_schema=_schema(),
        template_analysis=_analysis(),
        bindings=_bindings(),
        source_snapshot_id="snapshot-1",
        source_snapshot_hash="snapshot-hash",
    )
    assert first.plan_hash == second.plan_hash
    assert [unit.unit_id for unit in first.semantic_units] == ["product", "pins"]
    assert first.layout_contract.bindings["product"] == ["product-cell"]
    serialized = first.model_dump(mode="json")
    assert "template bytes" not in str(serialized).lower()
    assert "content" not in str(serialized).lower()
    assert first.coverage_contract.requirements[1].required_columns == ["connector", "pin", "signal"]


def test_shadow_compiler_reports_missing_table_rows_and_bindings_without_inventing_keys():
    adapter = LegacyTemplatePlanningAdapter(registries=build_builtin_registries())
    output = _output_spec()
    output = OutputSpec(**{
        **{key: value for key, value in output.model_dump(mode="json").items() if key != "content_hash"},
        "table_requirements": [{
            "unit_id": "pins",
            "row_scope": "all_selected_pins",
            "required_columns": ["connector", "pin"],
        }],
    })
    plan = adapter.compile(
        output_spec=output,
        document_schema=_schema(),
        template_analysis=None,
        bindings=_bindings()[:1],
        source_snapshot_id="snapshot-1",
        source_snapshot_hash="snapshot-hash",
    )
    codes = {issue.code for issue in plan.issues}
    assert "row_scope_unresolved" in codes
    assert "binding_missing" in codes
    assert not any(requirement.row_keys for requirement in plan.coverage_contract.requirements if requirement.kind == "table")
    assert plan.is_executable is False


def test_service_wraps_pure_compiler_without_calling_external_dependencies():
    service = DocumentPlanningService(adapter=LegacyTemplatePlanningAdapter(registries=build_builtin_registries()))
    plan = service.compile(
        output_spec=_output_spec(),
        document_schema=_schema(),
        template_analysis=_analysis(),
        bindings=_bindings(),
        source_snapshot_id="snapshot-1",
        source_snapshot_hash="snapshot-hash",
    )
    assert plan.output_spec_id == "spec-1"
    assert plan.status == "proposed"
