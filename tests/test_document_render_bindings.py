"""Task 5 server-owned render-binding tests."""

from __future__ import annotations

import pytest

from src.document_authoring.document_model import DocumentModel, ParagraphBlock, TypedTableBlock
from src.document_authoring.models import (
    TemplateUnitBinding,
    WorkbookRegionSchema,
    WorkbookTableColumnSchema,
    WorkbookTableSchema,
    TypedTableRow,
)
from src.document_authoring.planning.models import TemplateContract
from src.document_authoring.render_bindings import (
    LegacyFillPlanAdapter,
    RenderBinding,
    RenderBindingResolver,
)


def _model() -> DocumentModel:
    return DocumentModel(
        document_id="doc-1", plan_id="plan-1", plan_version=1, plan_hash="sha256:plan",
        blocks=[
            ParagraphBlock(block_id="b-cover", unit_id="cover", content="Cover"),
            TypedTableBlock(
                block_id="b-pins", unit_id="pins", columns=["pin", "signal"],
                rows=[TypedTableRow(
                    row_key="J1:1", cells={"pin": "1", "signal": "CAN"},
                    evidence_ids=["e1"],
                )], expected_row_keys=["J1:1"],
            ),
        ],
    )


def _template_contract() -> TemplateContract:
    return TemplateContract(
        kind="template", template_version_id="tv-1", template_schema_id="ts-1",
        template_schema_version="1", bindings={
            "cover": ["region-cover"], "pins": ["table-pins"],
        },
    )


def _bindings():
    return [
        TemplateUnitBinding(
            binding_id="binding-cover", template_schema_id="ts-1", template_schema_version="1",
            semantic_unit_type="section", semantic_unit_id="cover", target_region_ids=["region-cover"],
        ),
        TemplateUnitBinding(
            binding_id="binding-pins", template_schema_id="ts-1", template_schema_version="1",
            semantic_unit_type="field", semantic_unit_id="pins", target_region_ids=["table-pins"],
            table_schema=WorkbookTableSchema(
                table_region_id="table-pins", semantic_unit_id="pins", sheet_name="Review",
                header_row=1, first_data_row=2, last_template_row=2, style_source_row=2,
                max_output_rows=2, columns=[
                    WorkbookTableColumnSchema(column_id="pin", label="Pin", column_letter="A"),
                    WorkbookTableColumnSchema(column_id="signal", label="Signal", column_letter="B"),
                ], expected_row_keys=["J1:1"], required_columns=["pin", "signal"],
            ),
        ),
    ]


def test_resolver_only_emits_registered_server_owned_bindings():
    resolved = RenderBindingResolver().resolve(
        _template_contract(), _model(), _bindings(),
        regions=[WorkbookRegionSchema(
            region_id="region-cover", sheet_name="Review", locator={"cell": "A1"},
            role="semantic_draft", write_policy="validated_draft",
        )],
    )

    assert any(binding.unit_id == "cover" and binding.region_id == "region-cover" for binding in resolved.bindings)
    assert any(
        binding.unit_id == "pins" and binding.row_key == "J1:1" and binding.column_id == "signal"
        for binding in resolved.bindings
    )
    assert resolved.binding_hash


def test_binding_model_has_no_model_selected_coordinate_and_static_regions_are_rejected():
    with pytest.raises(ValueError):
        RenderBinding.model_validate({
            "unit_id": "cover", "region_id": "region-cover", "physical_locator": "A1",
        })

    static = WorkbookRegionSchema(
        region_id="region-cover", sheet_name="Review", locator={"cell": "A1"},
        role="formula", write_policy="never", preserve_formula=True,
    )
    with pytest.raises(PermissionError, match="writable"):
        RenderBindingResolver().resolve(_template_contract(), _model(), _bindings(), regions=[static])


def test_legacy_fill_plan_adapter_preserves_scalar_and_typed_table_contracts():
    fill_plan = LegacyFillPlanAdapter.to_fill_plan(
        _model(),
        template_version_id="tv-1",
        output_format="xlsx",
        bindings=_bindings(),
    )

    assert [fill.semantic_unit_id for fill in fill_plan.fills] == ["cover"]
    assert fill_plan.fills[0].region_id == "region-cover"
    assert len(fill_plan.table_fills) == 1
    row = fill_plan.table_fills[0].rows[0]
    assert row.row_key == "J1:1"
    assert row.cells == {"pin": "1", "signal": "CAN"}

    with pytest.raises(PermissionError, match="DOCX"):
        LegacyFillPlanAdapter.to_fill_plan(
            _model(),
            template_version_id="tv-1",
            output_format="docx",
            bindings=_bindings(),
        )
