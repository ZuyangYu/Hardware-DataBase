"""Table-shaped drafts must reach real template cells without scalar flattening."""
import hashlib
import io

import zipfile
from xml.etree import ElementTree as ET
import pytest

from src.document_authoring.models import DocumentUnitDraft, RendererPolicy, TemplateVersion
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import TemplateAnalysis, TemplateAnalysisSuggestion, TemplateAnalysisUnit, workbook_value_hash
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.work_order_store import DocumentAuthoringStore


def _fixture():
    from tests.test_writer_brief_passthrough import _xlsx_template_bytes
    headers = {"A1": "Signal", "B1": "Pin"}
    units = []
    for row in range(1, 6):
        for column in ("A", "B"):
            cell = f"{column}{row}"
            value = headers.get(cell)
            units.append(TemplateAnalysisUnit(
                unit_id=cell, locator={"sheet_name": "Review", "cell": cell},
                writable=row > 1, value_preview=value, value_hash=workbook_value_hash(value),
                value_kind="text" if value else "blank",
                structural_role_hint="table_header" if row == 1 else "placeholder",
            ))
    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_xlsx_template_bytes())) as original, zipfile.ZipFile(data, "w") as output:
        for name in original.namelist():
            content = original.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = b'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
                <row r="1"><c r="A1" t="inlineStr"><is><t>Signal</t></is></c><c r="B1" t="inlineStr"><is><t>Pin</t></is></c></row>
                <row r="2"><c r="A2"/><c r="B2"/></row>
                <row r="7"><c r="A7" t="inlineStr"><is><t>DO NOT OVERWRITE FOOTER</t></is></c></row>
                </sheetData></worksheet>'''
            output.writestr(name, content)
    content = data.getvalue()
    template = TemplateVersion(
        template_version_id="tv-table", template_id="interfaces", format="xlsx",
        content_hash=hashlib.sha256(content).hexdigest(), template_schema_id="ts-table",
        template_schema_version="1", renderer_policy_id="table-policy",
    )
    analysis = TemplateAnalysis(
        analysis_id="analysis-table", template_version_id=template.template_version_id,
        content_hash=template.content_hash, format="xlsx", status="ready_for_confirmation",
        units=units, suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="signals", label="Signals", confidence=1,
            target_unit_ids=[unit.unit_id for unit in units if unit.writable],
            value_shape="repeating_table", retrieval_terms=["signals", "pins"],
        )],
    )
    return template, analysis, content


def _draft(rows=None):
    return DocumentUnitDraft.model_validate({
        "unit_id": "field:signals", "run_id": "run-1", "generated_by": "managed_writer",
        "content": "Signal connections", "evidence_ids": ["e1", "e2"],
        "typed_value": {"kind": "table", "display_value": "2 interfaces", "evidence_ids": ["e1", "e2"],
                        "rows": rows if rows is not None else [
                            {"cells": {"A": "CAN_TX", "B": "P13.0"}, "evidence_ids": ["e1"]},
                            {"cells": {"A": "CAN_RX", "B": "P13.1"}, "evidence_ids": ["e2"]},
                        ]},
        "assertions": [{"assertion_id": "a1", "claim_id": "c1", "text": "CAN_TX P13.0", "evidence_ids": ["e1"]}],
    })


EVIDENCE = {"e1": {"content": "CAN_TX P13.0"}, "e2": {"content": "CAN_RX P13.1"}}


def test_table_draft_fills_real_rows_and_preserves_template(tmp_path):
    template, analysis, content = _fixture()
    regions, bindings = DocumentGenerationService._regions_and_bindings(template, analysis)
    store = DocumentAuthoringStore(str(tmp_path / "table.db"), str(tmp_path / "artifacts"))
    service = DocumentGenerationService(None, store)
    service.register_renderer_policy(RendererPolicy(renderer_policy_id="table-policy"))
    service.register_template(template, content, regions=regions, bindings=bindings)
    draft = DocumentValidator().validate_typed_field_draft(_draft(), EVIDENCE, expected_value_type="table")
    assert draft.validation_status == "supported", draft.validation_notes
    plan = service._semantic_fills(template, [draft], {"field:signals": "ready_to_render"}, {b.semantic_unit_id: b for b in bindings})
    rendered, manifest = service._render_fill_plan(template, plan)
    with zipfile.ZipFile(io.BytesIO(rendered)) as package:
        sheet = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values = {cell.get("r"): ''.join(cell.itertext()) for cell in sheet.findall('.//s:c', ns)}
    assert {cell: values[cell] for cell in ["A1", "B1", "A2", "B2", "A3", "B3"]} == {
        "A1": "Signal", "B1": "Pin", "A2": "CAN_TX", "B2": "P13.0", "A3": "CAN_RX", "B3": "P13.1",
    }
    assert values["A7"] == "DO NOT OVERWRITE FOOTER"
    assert len(plan.table_fills[0].rows) == 2


@pytest.mark.parametrize("rows", [
    [{"cells": {"A": "CAN_TX", "B": "P99.9"}, "evidence_ids": ["e1"]}],
    [{"cells": {"A": "CAN_TX", "B": "P13.0"}, "evidence_ids": []}],
    [{"cells": {"A": "CAN_TX", "B": "P13.0"}, "evidence_ids": ["unknown"]}],
])
def test_table_cells_require_their_own_row_evidence(rows):
    draft = DocumentValidator().validate_typed_field_draft(_draft(rows), EVIDENCE, expected_value_type="table")
    assert draft.validation_status == "unsupported"


def test_table_mapping_rejects_holes_in_writable_rectangle():
    template, analysis, _ = _fixture()
    analysis.suggestions[0].target_unit_ids.remove("B3")
    with pytest.raises(ValueError, match="rectangle"):
        DocumentGenerationService._regions_and_bindings(template, analysis)


def test_table_output_cannot_silently_drop_unknown_columns(tmp_path):
    template, analysis, _ = _fixture()
    _, bindings = DocumentGenerationService._regions_and_bindings(template, analysis)
    draft = _draft()
    draft.typed_value.rows[0].cells["unmapped"] = "CAN_TX"
    draft.validation_status = "supported"
    with pytest.raises(ValueError, match="columns"):
        DocumentGenerationService._semantic_fills(template, [draft], {"field:signals": "ready_to_render"}, {b.semantic_unit_id: b for b in bindings})


def test_repeating_table_survives_suggestion_normalization():
    from src.document_authoring.template_suggester import _split_multi_target
    _, analysis, _ = _fixture()
    suggestions = _split_multi_target(analysis.suggestions)
    assert len(suggestions) == 1
    assert len(suggestions[0].target_unit_ids) == 8


def test_safe_rectangle_is_activatable_without_scalar_downgrade():
    from src.document_authoring.template_activation import decide_template_activation
    _, analysis, _ = _fixture()
    decision = decide_template_activation(analysis)
    assert decision.status == "auto_accepted", decision.reason_codes


def test_writer_receives_table_column_contract():
    from src.document_authoring.harness.graph import build_writer_request
    from tests.test_writer_brief_passthrough import _work_order, _run, _requirement
    from src.document_authoring.models import DocumentSchema
    _, analysis, _ = _fixture()
    field = DocumentGenerationService._field_for_suggestion(analysis.suggestions[0], analysis.units)
    schema = DocumentSchema(document_schema_id="ds-1", version="1", document_type="ICD", fields=[field])
    request = build_writer_request(
        work_order=_work_order({}), harness_run=_run(), unit_id="field:signals",
        schema=schema, requirement=_requirement(), evidence=list(EVIDENCE.values()), prompt_version="1",
    )
    assert request.field_value_type == "table"
    assert request.table_columns == {"A": "Signal", "B": "Pin"}
