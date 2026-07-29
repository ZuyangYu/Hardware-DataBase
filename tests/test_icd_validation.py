from types import SimpleNamespace
from unittest.mock import Mock
from io import BytesIO
from xml.sax.saxutils import escape
import zipfile

import pytest

from src.document_authoring.icd_validation import validate_icd_pin_set
from src.document_authoring.models import ValidationReport
from src.document_authoring.service import DocumentGenerationService


def generated_workbook_without(pin_key: str) -> bytes:
    rows = [["Connector", "Pin Number", "Pin Definition"]]
    if pin_key != "J7-1":
        rows.append(["J7", "1", "CAN_H"])
    return _xlsx(rows)


def _xlsx(rows: list[list[str]]) -> bytes:
    def cell(column: str, row: int, value: str) -> str:
        return (
            f'<c r="{column}{row}" t="inlineStr"><is><t>'
            f"{escape(value)}</t></is></c>"
        )

    sheet_rows = "".join(
        f'<row r="{row_number}">' + "".join(
            cell(chr(ord("A") + column_number), row_number, value)
            for column_number, value in enumerate(row)
        ) + "</row>"
        for row_number, row in enumerate(rows, start=1)
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Pin Definition" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{sheet_rows}</sheetData></worksheet>",
        )
    return output.getvalue()


def test_validation_blocks_missing_selected_edf_pin():
    issues = validate_icd_pin_set(
        [{"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}],
        generated_workbook_without("J7-1"),
        "xlsx",
    )

    assert issues == [
        {"code": "icd_pin_missing", "severity": "blocking", "key": "j7:1"}
    ]


def test_validation_blocks_duplicate_and_net_mismatch_selected_edf_pins():
    issues = validate_icd_pin_set(
        [{"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}],
        _xlsx([
            ["Connector", "Pin Number", "Pin Definition"],
            ["J7", "1", "CAN_L"],
            ["J7", "1", "CAN_L"],
        ]),
        "xlsx",
    )

    assert issues == [
        {"code": "icd_pin_duplicate", "severity": "blocking", "key": "j7:1"},
        {"code": "icd_pin_net_mismatch", "severity": "blocking", "key": "j7:1"},
    ]


def test_approval_rejects_icd_blocking_issue_even_when_report_is_passed():
    service = object.__new__(DocumentGenerationService)
    candidate = SimpleNamespace(
        stage="review_candidate",
        validation_report_id="report-1",
        work_order_id="work-1",
    )
    service._artifact_for_context = Mock(return_value=candidate)
    service._order = Mock(return_value=SimpleNamespace())
    service.store = SimpleNamespace(
        get_validation_report=Mock(
            return_value=ValidationReport(
                validation_report_id="report-1",
                work_order_id="work-1",
                status="passed",
                issues=[
                    {
                        "code": "icd_pin_missing",
                        "severity": "blocking",
                        "key": "j7:1",
                    }
                ],
                evidence_matrix_hash="matrix-hash",
            )
        )
    )

    with pytest.raises(ValueError, match="ICD blocking"):
        service.approve_document_artifact(SimpleNamespace(), "candidate-1")
