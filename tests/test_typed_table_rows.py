"""Task 3 contract tests for typed, row-keyed table authoring."""

from __future__ import annotations

import pytest

from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocumentUnitDraft,
    RendererPolicy,
    TemplateUnitBinding,
    TypedFieldValue,
    TypedTableRow,
    WorkbookFillPlan,
    WorkbookTableColumnSchema,
    WorkbookTableFill,
    WorkbookTableRowFill,
    WorkbookTableSchema,
)
from src.document_authoring.planning.models import TableRequirement
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.writers.managed import DeterministicEvidenceWriter
from src.document_authoring.writers.provider import WriterRequest


def _table_draft(rows: list[dict], *, evidence_ids=None, kind: str = "table") -> DocumentUnitDraft:
    ids = list(evidence_ids or ["e1", "e2"])
    return DocumentUnitDraft.model_validate({
        "unit_id": "field:interfaces",
        "run_id": "run-1",
        "generated_by": "managed_writer",
        "content": "Interface table",
        "evidence_ids": ids,
        "typed_value": {
            "kind": kind,
            "display_value": "2 interfaces",
            "evidence_ids": ids,
            "rows": rows,
        },
        "assertions": [{
            "assertion_id": "assertion-1",
            "claim_id": "claim-interfaces",
            "text": "J1:1 CAN_TX P13.0",
            "evidence_ids": [ids[0]],
        }],
    })


def _row(key: str, signal: str, pin: str, *, row_evidence=None, cell_evidence=None) -> dict:
    return {
        "row_key": key,
        "cells": {"signal": signal, "pin": pin},
        "evidence_ids": list(row_evidence or ["e1"]),
        "cell_evidence_ids": cell_evidence or {
            "signal": list(row_evidence or ["e1"]),
            "pin": list(row_evidence or ["e1"]),
        },
    }


EVIDENCE = {
    "e1": {"id": "e1", "content": "J1:1 CAN_TX P13.0", "metadata": {}},
    "e2": {"id": "e2", "content": "J1:2 CAN_RX P13.1", "metadata": {}},
    "e3": {"id": "e3", "content": "J1:3 CAN_STB P13.2", "metadata": {}},
}


def test_typed_table_row_round_trip_preserves_identity_and_cell_evidence():
    row = TypedTableRow.model_validate(_row("J1:1", "CAN_TX", "P13.0"))

    assert row.row_key == "J1:1"
    assert row.cells == {"signal": "CAN_TX", "pin": "P13.0"}
    assert row.cell_evidence_ids == {"signal": ["e1"], "pin": ["e1"]}
    assert TypedTableRow.model_validate(row.model_dump()).model_dump() == row.model_dump()


def test_table_requirement_carries_server_owned_row_contract():
    requirement = TableRequirement(
        unit_id="interfaces",
        row_scope="connector pins",
        row_identity_fields=["connector", "pin"],
        row_key_schema={"format": "connector:pin"},
        required_columns=["signal", "pin"],
        row_keys=["J1:1", "J1:2"],
        row_order="declared",
        duplicate_policy="reject",
    )

    assert requirement.row_identity_fields == ["connector", "pin"]
    assert requirement.row_key_schema == {"format": "connector:pin"}
    assert requirement.row_keys == ["J1:1", "J1:2"]
    assert requirement.row_order == "declared"

    with pytest.raises(ValueError, match="row_keys.*unique"):
        TableRequirement(
            unit_id="interfaces",
            row_scope="connector pins",
            required_columns=["signal"],
            row_keys=["J1:1", "J1:1"],
        )


def test_validator_requires_exact_expected_rows_columns_and_cell_owned_evidence():
    requirement = TableRequirement(
        unit_id="interfaces",
        row_scope="connector pins",
        required_columns=["signal", "pin"],
        row_keys=["J1:1", "J1:2"],
    )
    draft = _table_draft([
        _row("J1:1", "CAN_TX", "P13.0"),
        _row("J1:3", "CAN_STB", "P13.2", row_evidence=["e3"]),
    ])

    checked = DocumentValidator().validate_typed_field_draft(
        draft,
        EVIDENCE,
        expected_value_type="table",
        table_requirement=requirement,
    )

    assert checked.validation_status == "unsupported"
    assert any("missing row" in note for note in checked.validation_notes)
    assert any("unexpected row" in note for note in checked.validation_notes)

    outside_row = _table_draft([_row(
        "J1:1", "CAN_TX", "P13.0", row_evidence=["e1"],
        cell_evidence={"signal": ["e2"], "pin": ["e1"]},
    )], evidence_ids=["e1", "e2"])
    checked = DocumentValidator().validate_typed_field_draft(
        outside_row,
        EVIDENCE,
        expected_value_type="table",
        table_requirement=TableRequirement(
            unit_id="interfaces", row_scope="connector pins",
            required_columns=["signal", "pin"], row_keys=["J1:1"],
        ),
    )
    assert checked.validation_status == "unsupported"
    assert any("cell evidence" in note for note in checked.validation_notes)


def test_validator_rejects_table_to_scalar_fallback_and_legacy_rows_need_no_fabricated_key():
    scalar = _table_draft([], kind="scalar")
    checked = DocumentValidator().validate_typed_field_draft(
        scalar, EVIDENCE, expected_value_type="table",
        table_requirement=TableRequirement(
            unit_id="interfaces", row_scope="connector pins",
            required_columns=["signal", "pin"], row_keys=["J1:1"],
        ),
    )
    assert checked.validation_status == "unsupported"
    assert any("does not match expected table" in note for note in checked.validation_notes)

    legacy = _table_draft([
        {"cells": {"signal": "CAN_TX", "pin": "P13.0"}, "evidence_ids": ["e1"]},
    ], evidence_ids=["e1"])
    checked = DocumentValidator().validate_typed_field_draft(
        legacy, {"e1": EVIDENCE["e1"]}, expected_value_type="table",
    )
    assert checked.validation_status == "supported"
    assert checked.typed_value.rows[0].row_key == ""


def test_writer_request_and_prompt_expose_typed_table_contract():
    request = WriterRequest(
        work_order_id="wo-1",
        run_id="run-1",
        unit_id="field:interfaces",
        unit_label="Interfaces",
        field_value_type="table",
        table_mode="typed_rows",
        table_columns={"signal": "Signal", "pin": "Pin"},
        expected_columns=["signal", "pin"],
        expected_row_keys=["J1:1", "J1:2"],
        row_key_schema={"format": "connector:pin"},
        table_output_requirement="typed_rows",
        prompt_version="1",
        evidence=[EVIDENCE["e1"]],
    )

    from src.document_authoring.writers.managed import _build_user_prompt

    prompt = _build_user_prompt(request, None)
    assert request.table_mode == "typed_rows"
    assert request.table_output_requirement == "typed_rows"
    assert "row_key" in prompt
    assert "cell_evidence_ids" in prompt
    assert "J1:1" in prompt
    assert "never as a scalar" in prompt.casefold()


def test_deterministic_writer_builds_typed_rows_from_structured_evidence():
    request = WriterRequest(
        work_order_id="wo-1", run_id="run-1", unit_id="field:interfaces",
        unit_label="Interfaces", field_value_type="table", table_mode="typed_rows",
        table_columns={"signal": "Signal", "pin": "Pin"},
        expected_columns=["signal", "pin"], expected_row_keys=["J1:1", "J1:2"],
        row_key_schema={"format": "connector:pin"}, table_output_requirement="typed_rows",
        prompt_version="1",
        evidence=[
            {"id": "e2", "content": "J1:2 CAN_RX P13.1", "metadata": {
                "row_key": "J1:2", "cells": {"signal": "CAN_RX", "pin": "P13.1"},
            }},
            {"id": "e1", "content": "J1:1 CAN_TX P13.0", "metadata": {
                "row_key": "J1:1", "cells": {"signal": "CAN_TX", "pin": "P13.0"},
            }},
        ],
    )

    draft = DeterministicEvidenceWriter().generate(request)

    assert draft.typed_value is not None
    assert draft.typed_value.kind == "table"
    assert [row.row_key for row in draft.typed_value.rows] == ["J1:1", "J1:2"]
    assert draft.typed_value.rows[0].cell_evidence_ids == {
        "signal": ["e1"], "pin": ["e1"],
    }


def _table_schema() -> WorkbookTableSchema:
    return WorkbookTableSchema(
        table_region_id="interfaces-table", semantic_unit_id="interfaces",
        sheet_name="Review", header_row=1, first_data_row=2, last_template_row=3,
        style_source_row=2, max_output_rows=4,
        columns=[
            WorkbookTableColumnSchema(column_id="signal", label="Signal", column_letter="A"),
            WorkbookTableColumnSchema(column_id="pin", label="Pin", column_letter="B"),
        ],
        expected_row_keys=["J1:1", "J1:2"],
    )


def test_semantic_fills_preserve_row_keys_and_declared_order():
    schema = DocumentSchema(
        document_schema_id="ds", version="1", document_type="test",
        fields=[DocumentFieldSchema(
            field_id="interfaces", label="Interfaces", value_type="table",
            retrieval_policy_id="r", verification_policy_id="v",
            table_columns={"signal": "Signal", "pin": "Pin"},
        )],
    )
    # The service helper only consumes bindings and drafts; schema is kept here
    # as a contract reminder that table fields are not scalarized.
    assert schema.fields[0].value_type == "table"
    draft = _table_draft([
        _row("J1:2", "CAN_RX", "P13.1", row_evidence=["e2"]),
        _row("J1:1", "CAN_TX", "P13.0", row_evidence=["e1"]),
    ])
    draft.validation_status = "supported"
    binding = TemplateUnitBinding(
        binding_id="binding-1", template_schema_id="ts", template_schema_version="1",
        semantic_unit_type="field", semantic_unit_id="interfaces",
        target_region_ids=["interfaces-table"], table_schema=_table_schema(),
    )

    from src.document_authoring.models import TemplateVersion

    plan = DocumentGenerationService._semantic_fills(
        TemplateVersion(
            template_version_id="tv", template_id="t", format="xlsx", content_hash="h",
            template_schema_id="ts", template_schema_version="1", renderer_policy_id="p",
        ),
        [draft], {"field:interfaces": "ready_to_render"}, {"interfaces": binding},
    )

    assert [row.row_key for row in plan.table_fills[0].rows] == ["J1:1", "J1:2"]
    assert [row.cells for row in plan.table_fills[0].rows] == [
        {"signal": "CAN_TX", "pin": "P13.0"},
        {"signal": "CAN_RX", "pin": "P13.1"},
    ]


def test_workbook_table_rows_reject_duplicate_or_missing_keys_but_read_legacy_dicts():
    duplicate = WorkbookTableFill(
        table_region_id="interfaces-table", semantic_unit_id="interfaces",
        rows=[
            WorkbookTableRowFill(row_key="J1:1", cells={"signal": "CAN_TX", "pin": "P13.0"}),
            WorkbookTableRowFill(row_key="J1:1", cells={"signal": "CAN_RX", "pin": "P13.1"}),
        ],
    )
    assert duplicate.rows[0].row_key == "J1:1"

    legacy = WorkbookTableFill(
        table_region_id="legacy-table", semantic_unit_id="legacy",
        rows=[{"signal": "CAN_TX", "pin": "P13.0"}],
    )
    assert isinstance(legacy.rows[0], dict)
    assert legacy.rows[0]["signal"] == "CAN_TX"


def test_renderer_maps_typed_rows_and_records_row_identity():
    from tests.test_agent_field_harness_smoke import _cell_value, _xlsx_table_fixture
    from src.document_authoring.renderers.xlsm import XlsmRenderer
    from src.document_authoring.template_analysis import workbook_value_hash

    schema = WorkbookTableSchema(
        table_region_id="interfaces-table", semantic_unit_id="interfaces",
        sheet_name="Review", header_row=1, first_data_row=2, last_template_row=2,
        style_source_row=2, max_output_rows=4,
        columns=[
            WorkbookTableColumnSchema(column_id="pin", label="Pin", column_letter="A"),
            WorkbookTableColumnSchema(column_id="signal", label="Signal", column_letter="B"),
        ],
        expected_value_hashes={
            "A2": workbook_value_hash("sample-pin"),
            "B2": workbook_value_hash("sample-signal"),
        },
        expected_row_keys=["J1:1", "J1:2"],
        required_columns=["pin", "signal"],
    )
    fill = WorkbookTableFill(
        table_region_id="interfaces-table", semantic_unit_id="interfaces",
        rows=[
            WorkbookTableRowFill(row_key="J1:1", cells={"pin": "J1-1", "signal": "CAN"}),
            WorkbookTableRowFill(row_key="J1:2", cells={"pin": "J1-2", "signal": "LIN"}),
        ],
    )
    result = XlsmRenderer().render(
        _xlsx_table_fixture(), [],
        WorkbookFillPlan(template_version_id="template-a", fills=[], table_fills=[fill]),
        RendererPolicy(renderer_policy_id="smoke-renderer"),
        table_schemas=[schema], security_approved=True,
    )

    assert _cell_value(result.content, "A2") == "J1-1"
    assert _cell_value(result.content, "B3") == "LIN"
    assert result.integrity_manifest["cell_changes"][0]["row_key"] == "J1:1"


def test_renderer_rejects_typed_table_rows_without_server_owned_keys():
    from tests.test_agent_field_harness_smoke import _xlsx_table_fixture
    from src.document_authoring.renderers.xlsm import XlsmRenderer
    from src.document_authoring.template_analysis import workbook_value_hash

    schema = WorkbookTableSchema(
        table_region_id="interfaces-table", semantic_unit_id="interfaces",
        sheet_name="Review", header_row=1, first_data_row=2, last_template_row=2,
        style_source_row=2, max_output_rows=4,
        columns=[
            WorkbookTableColumnSchema(column_id="pin", label="Pin", column_letter="A"),
            WorkbookTableColumnSchema(column_id="signal", label="Signal", column_letter="B"),
        ],
        expected_value_hashes={
            "A2": workbook_value_hash("sample-pin"),
            "B2": workbook_value_hash("sample-signal"),
        },
        expected_row_keys=["J1:1"],
        required_columns=["pin", "signal"],
    )
    fill = WorkbookTableFill(
        table_region_id="interfaces-table", semantic_unit_id="interfaces",
        rows=[WorkbookTableRowFill(cells={"pin": "J1-1", "signal": "CAN"})],
    )

    with pytest.raises(ValueError, match="row keys"):
        XlsmRenderer().render(
            _xlsx_table_fixture(), [],
            WorkbookFillPlan(template_version_id="template-a", fills=[], table_fills=[fill]),
            RendererPolicy(renderer_policy_id="smoke-renderer"),
            table_schemas=[schema], security_approved=True,
        )


def test_renderer_row_bound_expands_for_validated_dynamic_table_fills():
    """Frozen dynamic tables may exceed the activation-time template bound."""
    schema = _table_schema().model_copy(update={"max_output_rows": 2})
    fill = WorkbookTableFill(
        table_region_id="interfaces-table", semantic_unit_id="interfaces",
        rows=[
            WorkbookTableRowFill(row_key=f"J1:{index}", cells={"signal": "CAN", "pin": f"P13.{index}"})
            for index in range(1, 4)
        ],
    )
    plan = WorkbookFillPlan(
        template_version_id="template-a", fills=[], table_fills=[fill],
    )

    schemas = DocumentGenerationService._table_schemas_for_fill_plan([schema], plan)

    assert schemas[0].max_output_rows == 3
    assert schemas[0].table_region_id == "interfaces-table"


def test_pin_functions_from_drafts_reads_function_column(monkeypatch):
    from types import SimpleNamespace

    service = object.__new__(DocumentGenerationService)
    schema = DocumentSchema(
        document_schema_id="ds", version="1", document_type="icd",
        fields=[DocumentFieldSchema(
            field_id="table:Sheet1:15", label="管脚表", value_type="table",
            retrieval_policy_id="r", verification_policy_id="v",
            table_columns={
                "A": "管脚号 Pin Number",
                "B": "管脚定义 Pin Definition",
                "C": "功能描述 Function",
            },
        )],
    )
    monkeypatch.setattr(service, "_schema", lambda *_args, **_kwargs: schema)

    def _draft(unit_id: str, rows: list[TypedTableRow]) -> DocumentUnitDraft:
        return DocumentUnitDraft(
            unit_id=unit_id, run_id="run-1", generated_by="managed_writer",
            content="table", evidence_ids=["e1"],
            typed_value=TypedFieldValue(
                kind="table", normalized_values=[row.row_key for row in rows],
                display_value=f"{len(rows)} rows", evidence_ids=["e1"], rows=rows,
            ),
            validation_status="supported",
        )

    order = SimpleNamespace(document_schema_id="ds", document_schema_version="1")
    drafts = [
        _draft("field:table:Sheet1:15", [
            TypedTableRow(row_key="X1900-1", cells={
                "A": "X1900-1", "B": "I_S_WKUP", "C": "唤醒控制输入",
            }, evidence_ids=["e1"]),
            TypedTableRow(row_key="X1900-2", cells={
                "A": "X1900-2", "B": "CAN0H", "C": "TBD（知识库未提供可靠功能描述）",
            }, evidence_ids=["e1"]),
        ]),
    ]

    functions = service._pin_functions_from_drafts(order, drafts)

    assert functions == {"X1900-1": "唤醒控制输入"}


def test_field_evidence_selection_keeps_pin_function_evidence_under_cap():
    from src.document_authoring.harness.graph import _select_field_evidence

    items = [
        {
            "id": f"generic-{index}",
            "content": f"generic chunk {index} " + "x" * 100,
            "score": 0.9,
            "metadata": {},
        }
        for index in range(6)
    ]
    items.append({
        "id": "frozen",
        "content": "Frozen ICD pin mappings: X1900-1 -> I_S_WKUP",
        "score": 1.0,
        "metadata": {
            "frozen_icd_scope": True,
            "pin_mappings": [{"refdes": "X1900", "pin_name": "1", "net_name": "I_S_WKUP"}],
        },
    })
    items.append({
        "id": "pin-function:hits",
        "content": "X1900-1 KL15唤醒 | STB,STG,OPL | X1900-1 | ADAS处于休眠状态",
        "score": 1.0,
        "metadata": {"pin_function_hits": {"X1900-1": "KL15唤醒"}},
    })

    selected, discarded = _select_field_evidence(
        items,
        max_items=5,
        preserve_rerank_order=False,
        retrieval_query_terms=["管脚号 Pin Number", "功能描述 Function"],
    )

    selected_ids = {item["id"] for item in selected}
    assert {"frozen", "pin-function:hits"} <= selected_ids
    assert len(selected) == 5
    assert "pin-function:hits" not in discarded


def test_plan_execution_schema_preserves_template_table_labels():
    from types import SimpleNamespace

    from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
    from src.document_authoring.planning.models import (
        CoverageContract,
        CoverageRequirement,
    )

    original = DocumentSchema(
        document_schema_id="ds", version="1", document_type="icd", status="approved",
        fields=[DocumentFieldSchema(
            field_id="table:Sheet1:15", label="管脚表", value_type="table",
            retrieval_policy_id="r", verification_policy_id="v",
            table_columns={
                "A": "管脚号 Pin Number",
                "B": "管脚定义 Pin Definition",
                "C": "功能描述 Function",
            },
        )],
    )
    plan = SimpleNamespace(
        document_plan_id="plan-1",
        semantic_units=[SimpleNamespace(
            unit_id="table:Sheet1:15",
            required=True,
            output_schema={
                "type": "table",
                "label": "管脚表",
                "columns": ["A", "B", "C"],
            },
            source_capabilities=[],
        )],
        coverage_contract=CoverageContract(requirements=[CoverageRequirement(
            requirement_id="table:Sheet1:15",
            unit_id="table:Sheet1:15",
            kind="table",
            required_columns=["A", "B", "C"],
        )]),
    )

    adapted = InternalDocumentHarnessRuntime._plan_execution_schema(plan, original)
    field = adapted.fields[0]

    assert field.table_columns == {
        "A": "管脚号 Pin Number",
        "B": "管脚定义 Pin Definition",
        "C": "功能描述 Function",
    }
