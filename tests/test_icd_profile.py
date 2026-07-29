from io import BytesIO
import zipfile

from src.document_authoring.icd_profile import IcdTemplateProfile, classify_icd_template


def _workbook_bytes(sheet_name: str, rows: list[list[str]]) -> bytes:
    content = BytesIO()
    sheet_rows = "".join(
        f'<row r="{row_number}">' + "".join(
            f'<c r="{chr(65 + column_number)}{row_number}" t="inlineStr">'
            f"<is><t>{value}</t></is></c>"
            for column_number, value in enumerate(row)
            if value
        ) + "</row>"
        for row_number, row in enumerate(rows, start=1)
    )
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{sheet_rows}</sheetData></worksheet>",
        )
    return content.getvalue()


def _formal_icd_bytes() -> bytes:
    return _workbook_bytes("ICD", [
        ["在控制器上编号：Location Number", "J7"],
        ["板端接插件供应商/型号：", "BOARD-CONNECTOR-01"],
        [],
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["1", "CAN_H"],
    ])


def _example_only_bytes() -> bytes:
    return _workbook_bytes("Example", [
        ["在控制器上编号：Location Number", "J7"],
        ["板端接插件供应商/型号：", "BOARD-CONNECTOR-01"],
        [],
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["1", "CAN_H"],
    ])


def _generic_bytes() -> bytes:
    return _workbook_bytes("Budget", [
        ["Month", "Amount"],
        ["January", "100"],
    ])


def _codes(issues: list[dict[str, str]]) -> set[str]:
    return {issue["code"] for issue in issues}


def test_formal_icd_requires_identity_connector_and_pin_definition_contract():
    profile = classify_icd_template(_formal_icd_bytes(), "xlsx")

    assert profile.kind == "icd"
    assert profile.connector_blocks


def test_example_only_workbook_is_not_releasable_icd_template():
    profile = classify_icd_template(_example_only_bytes(), "xlsx")

    assert profile.kind == "icd_sample"
    assert "formal_connector_block_missing" in _codes(profile.issues)


def test_normal_spreadsheet_remains_generic():
    assert classify_icd_template(_generic_bytes(), "xlsx").kind == "generic"


def test_connector_block_keeps_the_actual_cells_of_identity_values():
    content = _workbook_bytes("ICD", [
        ["在控制器上编号：Location Number", "", "J7"],
        ["板端接插件供应商/型号：", "", "BOARD-CONNECTOR-01"],
        [],
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["1", "CAN_H"],
    ])

    block = classify_icd_template(content, "xlsx").connector_blocks[0]

    assert block.location_cell == "C1"
    assert block.board_connector_model_cell == "C2"


def test_profile_contract_does_not_require_front_view_data():
    profile = IcdTemplateProfile(
        kind="generic",
        reasons=[],
        connector_blocks=[],
        issues=[],
    )

    assert profile.connector_refdes == []
