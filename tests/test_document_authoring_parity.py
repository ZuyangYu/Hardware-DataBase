from __future__ import annotations

from io import BytesIO
import zipfile

import pytest

import src.document_authoring.icd_comparison as icd_comparison
from src.pipelines.spreadsheet.xlsx_parser import ParsedSheet, ParsedWorkbook


def _workbook(rows: list[list[str]], *, file_name: str = "icd.xlsx") -> ParsedWorkbook:
    return ParsedWorkbook(
        file_name=file_name,
        sheets=[ParsedSheet(name="Pin Definition", rows=rows)],
    )


def _api(name: str):
    function = getattr(icd_comparison, name, None)
    assert callable(function), f"Task 7 API is missing: {name}"
    return function


def _package(
    *,
    macro: bool = False,
    protected: bool = True,
    external_link: bool = False,
    vba_payload: bytes = b"synthetic-non-executable-vba-sentinel",
) -> bytes:
    rows = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Pin Definition</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>fixture</t></is></c></row>'
    )
    protection = '<sheetProtection sheet="1"/>' if protected else ""
    workbook_protection = '<workbookProtection lockStructure="1"/>' if protected else ""
    rels = (
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
    )
    content_type = (
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
        if macro
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    )
    entries = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/xl/workbook.xml" ContentType="{content_type}"/>'
            "</Types>"
        ).encode(),
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'{workbook_protection}<sheets><sheet name="Pin Definition" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ).encode(),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}"
            "</Relationships>"
        ).encode(),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{protection}<sheetData>{rows}</sheetData>"
            '<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>'
            "</worksheet>"
        ).encode(),
    }
    if macro:
        entries["xl/vbaProject.bin"] = vba_payload
        entries["xl/_rels/workbook.xml.rels"] = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{rels}<Relationship Id="rVba" '
            'Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" '
            'Target="vbaProject.bin"/></Relationships>'
        ).encode()
    if external_link:
        entries["xl/externalLinks/externalLink1.xml"] = b"<externalLink/>"
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_normalize_pin_definition_exposes_row_keys_columns_and_declared_order():
    normalize = _api("normalize_pin_definition_workbook")
    normalized = normalize(
        _workbook(
            [
                ["管脚号 Pin Number", "管脚定义 Pin Definition"],
                ["X1900-1", "CAN_H"],
                ["X1900-2", "CAN_L"],
            ]
        )
    )

    assert normalized["metric_version"] == "icd-pin-definition-v1"
    assert normalized["required_columns"] == [
        "connector",
        "pin_number",
        "pin_definition",
    ]
    assert normalized["columns"]["required"]["missing"] == []
    assert normalized["columns"]["required"]["completeness"] == 1.0
    assert normalized["row_keys"] == ["x1900:1", "x1900:2"]
    assert [row["declared_order"] for row in normalized["rows"]] == [0, 1]


def test_normalize_pin_definition_records_static_structure_observations():
    normalize = _api("normalize_pin_definition_workbook")
    workbook = ParsedWorkbook(
        file_name="icd.xlsx",
        sheets=[
            ParsedSheet(
                name="Pin Definition",
                rows=[
                    ["Connector", "Pin Number", "Pin Definition"],
                    ["J1", "1", "CAN_H"],
                ],
                merged_ranges=["A1:C1"],
            ),
            ParsedSheet(
                name="Example",
                rows=[["Pin Number", "Pin Definition"], ["1", "ignored"]],
            ),
        ],
        embedded_object_count=2,
        media_object_count=3,
        drawing_object_count=4,
    )

    normalized = normalize(workbook)

    assert normalized["static_structure"] == {
        "sheet_count": 2,
        "formal_sheet_count": 1,
        "example_sheet_count": 1,
        "merged_range_count": 1,
        "embedded_object_count": 2,
        "media_object_count": 3,
        "drawing_object_count": 4,
    }


def test_required_column_completeness_counts_each_repeated_table_block():
    normalize = _api("normalize_pin_definition_workbook")
    normalized = normalize(
        _workbook(
            [
                ["Connector", "Pin Number", "Pin Definition"],
                ["J1", "1", "CAN_H"],
                ["Pin Number", "Pin Definition"],
                ["2", "CAN_L"],
            ]
        )
    )

    required = normalized["columns"]["required"]
    assert required["missing"] == ["connector"]
    assert required["completeness"] == pytest.approx(5 / 6)


def test_comparison_classifies_keys_order_duplicates_cross_field_conflicts_and_evidence():
    compare = _api("compare_pin_definition_workbooks")
    reference = _workbook(
        [
            ["Connector", "Pin Number", "Pin Definition", "Function"],
            ["J1", "1", "CAN_H", "CAN high"],
            ["J1", "2", "CAN_L", "CAN low"],
            ["J2", "1", "GND", "Ground"],
        ]
    )
    generated = _workbook(
        [
            ["Connector", "Pin Number", "Pin Definition", "Function"],
            ["J1", "2", "CAN_L", "CAN low"],
            ["J1", "1", "CAN_H", "wrong function"],
            ["J1", "1", "CAN_H", "wrong function"],
            ["J3", "1", "ETH_P", "Ethernet"],
        ]
    )
    evidence = {
        "j1:1": {
            "connector": ["e:j1:1:c"],
            "pin_number": ["e:j1:1:p"],
            "pin_definition": ["e:j1:1:d"],
        },
        "j1:2": {
            "connector": ["e:j1:2:c"],
            "pin_number": ["e:j1:2:p"],
            "pin_definition": ["e:j1:2:d"],
        },
        "j3:1": {
            "connector": ["e:j3:1:c"],
            "pin_number": ["e:j3:1:p"],
            "pin_definition": ["e:j3:1:d"],
        },
    }

    result = compare(
        reference,
        generated,
        reference_evidence=evidence,
        generated_evidence=evidence,
    )

    assert result["row_keys"] == {
        "reference": ["j1:1", "j1:2", "j2:1"],
        "generated": ["j1:2", "j1:1", "j3:1"],
        "shared": ["j1:1", "j1:2"],
        "missing": ["j2:1"],
        "extra": ["j3:1"],
    }
    assert result["duplicates"]["generated_keys"] == ["j1:1"]
    assert result["order"]["exact"] is False
    assert result["order"]["relative"]["value"] == 0.0
    assert result["cross_field_consistency"]["conflicts"] == [
        {
            "key": "j1:1",
            "field": "function",
            "reference_value": "can high",
            "generated_value": "wrong function",
        }
    ]
    assert result["evidence"]["generated"]["supported_cell_count"] == 9
    assert result["metrics"]["row_key_precision"]["value"] == pytest.approx(2 / 3)
    assert result["metrics"]["row_key_recall"]["value"] == pytest.approx(2 / 3)
    assert result["metrics"]["duplicate_rate"]["value"] == pytest.approx(1 / 4)


def test_explicitly_missing_evidence_is_not_normalized_to_a_literal_none_id():
    normalize = _api("normalize_pin_definition_workbook")
    normalized = normalize(
        _workbook(
            [["Connector", "Pin Number", "Pin Definition"], ["J1", "1", "CAN_H"]]
        ),
        evidence_by_key={
            "j1:1": {"connector": None, "pin_number": None, "pin_definition": None}
        },
    )

    assert normalized["evidence"]["supported_cell_count"] == 0
    assert normalized["evidence"]["coverage"] == 0.0


def test_legacy_comparison_keeps_existing_summary_and_exposes_versioned_parity():
    result = icd_comparison.compare_workbooks(
        _workbook(
            [["Connector", "Pin Number", "Pin Definition"], ["J1", "1", "CAN_H"]]
        ),
        _workbook(
            [["Connector", "Pin Number", "Pin Definition"], ["J1", "1", "CAN_H"]]
        ),
    )

    assert result["summary"] == {
        "reference_pin_count": 1,
        "generated_pin_count": 1,
        "matching_pin_count": 1,
        "mismatched_pin_count": 0,
        "reference_only_pin_count": 0,
        "generated_only_pin_count": 0,
        "exact_match_rate": 1.0,
        "reference_coverage": 1.0,
    }
    assert result["parity"]["metric_version"] == "icd-pin-definition-v1"


def test_ooxml_package_inventory_and_preservation_checks_are_offline_and_format_aware():
    inspect = _api("inspect_ooxml_package")
    compare_packages = _api("compare_ooxml_packages")
    xlsx = _package()
    xlsm = _package(macro=True)

    xlsx_inventory = inspect(xlsx, format="xlsx")
    xlsm_inventory = inspect(xlsm, format="xlsm")
    assert xlsx_inventory["format"] == "xlsx"
    assert xlsx_inventory["has_vba_project"] is False
    assert xlsx_inventory["protected_sheet_count"] == 1
    assert xlsx_inventory["merged_range_count"] == 1
    assert xlsm_inventory["format"] == "xlsm"
    assert xlsm_inventory["has_vba_project"] is True
    assert xlsm_inventory["external_link_count"] == 0

    preserved = compare_packages(
        xlsm, xlsm, reference_format="xlsm", generated_format="xlsm"
    )
    assert preserved["preservation_ok"] is True
    assert preserved["checks"]["vba_preserved"] is True

    changed_macro = compare_packages(
        xlsm,
        _package(macro=True, vba_payload=b"different-synthetic-vba-sentinel"),
        reference_format="xlsm",
        generated_format="xlsm",
    )
    assert changed_macro["preservation_ok"] is False
    assert "generated_changed_vba_project" in changed_macro["issues"]

    unsafe = compare_packages(
        xlsm,
        _package(macro=False, protected=False, external_link=True),
        reference_format="xlsm",
        generated_format="xlsm",
    )
    assert unsafe["preservation_ok"] is False
    assert "generated_removed_vba_project" in unsafe["issues"]
    assert "generated_removed_sheet_protection" in unsafe["issues"]
    assert "generated_added_external_links" in unsafe["issues"]
