from io import BytesIO
import zipfile

from src.document_authoring.icd_profile import IcdTemplateProfile, classify_icd_template


def _workbook_bytes(
    sheet_name: str,
    rows: list[list[str]],
    row_numbers: list[int] | None = None,
) -> bytes:
    content = BytesIO()
    row_numbers = row_numbers or list(range(1, len(rows) + 1))
    sheet_rows = "".join(
        f'<row r="{row_number}">' + "".join(
            f'<c r="{chr(65 + column_number)}{row_number}" t="inlineStr">'
            f"<is><t>{value}</t></is></c>"
            for column_number, value in enumerate(row)
            if value
        ) + "</row>"
        for row_number, row in zip(row_numbers, rows, strict=True)
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


def test_adjacent_connector_blocks_keep_each_identity_pair_local_to_its_pin_table():
    content = _workbook_bytes("ICD", [
        ["Location Number", "J-LEFT"],
        ["", "", "", "", "", "Location Number", "J-RIGHT"],
        ["Board Connector", "MODEL-LEFT"],
        ["Pin Number", "Pin Definition"],
        ["", "", "", "", "", "Board Connector", "MODEL-RIGHT"],
        ["", "", "", "", "", "Pin Number", "Pin Definition"],
    ])

    blocks = classify_icd_template(content, "xlsx").connector_blocks

    assert [
        (block.location_number, block.board_connector_model)
        for block in blocks
    ] == [("J-LEFT", "MODEL-LEFT"), ("J-RIGHT", "MODEL-RIGHT")]


def test_connector_block_uses_sparse_physical_rows_for_geometry_and_addresses():
    content = _workbook_bytes(
        "ICD",
        [
            ["Location Number", "J-FAR", "Board Connector", "MODEL-FAR"],
            ["Pin Number", "Pin Definition"],
            ["Location Number", "J-NEAR", "Board Connector", "MODEL-NEAR"],
        ],
        row_numbers=[2, 100, 101],
    )

    block = classify_icd_template(content, "xlsx").connector_blocks[0]

    assert block.location_number == "J-NEAR"
    assert block.location_cell == "B101"
    assert block.board_connector_model == "MODEL-NEAR"
    assert block.board_connector_model_cell == "D101"
    assert block.pin_header_row == 100


def test_identity_label_cannot_be_used_as_a_location_number_value():
    content = _workbook_bytes("ICD", [
        ["Location Number", "Board Connector", "MODEL-1"],
        ["Pin Number", "Pin Definition"],
    ])

    profile = classify_icd_template(content, "xlsx")

    assert profile.kind == "icd_sample"
    assert "formal_connector_block_missing" in _codes(profile.issues)


def test_board_connector_model_label_cannot_be_used_as_a_location_number_value():
    content = _workbook_bytes("ICD", [
        ["Location Number", "Board Connector Model", "M-1"],
        ["Pin Number", "Pin Definition"],
    ])

    profile = classify_icd_template(content, "xlsx")

    assert profile.kind == "icd_sample"
    assert "formal_connector_block_missing" in _codes(profile.issues)


def test_formal_icd_keeps_model_values_that_contain_identity_label_words():
    for label, model in [
        ("Board Connector", "Board Connector 1234"),
        ("PCB Connector", "MOLEX PCB Connector 1234"),
    ]:
        content = _workbook_bytes("ICD", [
            ["Location Number", "J1"],
            [label, model],
            ["Pin Number", "Pin Definition"],
        ])

        profile = classify_icd_template(content, "xlsx")

        assert profile.kind == "icd"
        assert profile.connector_blocks[0].board_connector_model == model


def test_malformed_workbook_xml_is_unreadable_generic_profile():
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook>")

    profile = classify_icd_template(content.getvalue(), "xlsx")

    assert profile.kind == "generic"
    assert profile.reasons == ["unreadable_workbook"]
