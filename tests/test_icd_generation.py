from src.circuit.models import ComponentInstance, Pin
from io import BytesIO
import zipfile
from xml.sax.saxutils import escape

from src.document_authoring.icd_generation import (
    build_connector_rows,
    render_icd_pin_table,
)
from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx


def _xlsx_pin_table(rows: list[list[str]]) -> bytes:
    sheet_rows = "".join(
        f'<row r="{row_number}">'
        + "".join(
            f'<c r="{chr(65 + column_number)}{row_number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            for column_number, value in enumerate(row)
        )
        + "</row>"
        for row_number, row in enumerate(rows, start=1)
    )
    files = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="ICD" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{sheet_rows}</sheetData></worksheet>'.encode(),
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    return output.getvalue()


def test_build_connector_rows_keeps_connected_unconnected_and_ground_pins():
    connectors = [
        ComponentInstance(
            refdes="J7",
            library_cell="connector",
            part_number="PN-7",
            pins=[
                Pin(name="&1", net="CAN_H"),
                Pin(name="&2", net=None),
                Pin(name="&3", net="PGND"),
            ],
        ),
    ]

    rows = build_connector_rows(connectors, function_notes={"j7:1": "CAN 通讯"})

    assert rows == [
        {
            "pin": "J7-1",
            "definition": "CAN_H",
            "function": "CAN 通讯",
            "notice": "",
        },
        {
            "pin": "J7-2",
            "definition": "NC",
            "function": "",
            "notice": "源文件未声明网络连接",
        },
        {
            "pin": "J7-3",
            "definition": "PGND",
            "function": "地/回流连接（规则推断）",
            "notice": "",
        },
    ]


def test_build_connector_rows_resolves_xj_functions_from_manual_then_rules():
    connectors = [
        ComponentInstance(
            refdes="X1900",
            library_cell="connector",
            part_number="CONN-1900",
            pins=[Pin(name="&1", net="CAN0H"), Pin(name="&2", net=None)],
        ),
        ComponentInstance(
            refdes="J7",
            library_cell="connector",
            pins=[Pin(name="&1", net="VCC3V3")],
        ),
    ]

    rows = build_connector_rows(
        connectors,
        manual_evidence=[{
            "id": "manual-x1900-1",
            "content": "X1900 pin 1: CAN high differential bus signal.",
            "metadata": {"source_role": "datasheet"},
        }],
    )

    assert rows[0]["function"] == "CAN high differential bus signal."
    assert rows[1]["function"] == ""
    assert rows[2]["function"] == "电源供电连接（规则推断）"


def test_render_icd_pin_table_replaces_example_rows_with_frozen_scope():
    source = _xlsx_pin_table([
        ["管脚号 Pin Number", "管脚定义 Pin Definition", "功能描述 Function", "备注 Notice", "总成ERP\n600600653"],
        ["X302-1", "OLD_1", "旧功能 1", "旧备注 1", "●"],
        ["X302-2", "OLD_2", "旧功能 2", "旧备注 2", "—"],
    ])

    rendered = render_icd_pin_table(
        source,
        [
            {"refdes": "X1900", "pin_name": "1", "net_name": "CAN0_H"},
            {"refdes": "X1900", "pin_name": "2", "net_name": "NC"},
        ],
        allow_rule_inference=True,
        assembly_erp="600608964",
    )

    rows = parse_xlsx(BytesIO(rendered.content)).sheets[0].rows
    assert rows[1][:4] == [
        "X1900-1",
        "CAN0_H",
        "CAN 总线差分信号（规则推断）",
        "",
    ]
    assert rows[2][:4] == ["X1900-2", "NC", "未连接（NC）", ""]
    assert rows[0][4] == "总成ERP\n600608964"
    assert rendered.detected_table is True


def test_render_icd_pin_table_reports_unreadable_workbook_instead_of_silence():
    source = b"not-an-ooxml-package"

    rendered = render_icd_pin_table(
        source,
        [{"refdes": "X1900", "pin_name": "1", "net_name": "CAN0_H"}],
    )

    assert rendered.content == source
    assert rendered.detected_table is False
    assert [issue["code"] for issue in rendered.issues] == ["icd_pin_table_unreadable"]
    assert rendered.issues[0]["severity"] == "blocking"


def test_render_icd_pin_table_keeps_front_view_only_templates_non_blocking():
    source = _xlsx_pin_table([
        ["连接器前视图布局"],
        ["说明", "示例"],
    ])

    rendered = render_icd_pin_table(
        source,
        [{"refdes": "X1900", "pin_name": "1", "net_name": "CAN0_H"}],
    )

    assert rendered.content == source
    assert rendered.detected_table is False
    assert rendered.issues == []


def test_render_icd_pin_table_prefers_evidence_backed_function():
    source = _xlsx_pin_table([
        ["管脚号 Pin Number", "管脚定义 Pin Definition", "功能描述 Function", "备注 Notice"],
        ["X302-1", "OLD_1", "旧功能 1", "旧备注 1"],
    ])

    rendered = render_icd_pin_table(
        source,
        [{"refdes": "X1900", "pin_name": "1", "net_name": "CAN0_H"}],
        function_by_pin={
            "X1900-1": "车身CAN0高",
            "X1900-2": "TBD（知识库未提供可靠功能描述）",
        },
    )

    rows = parse_xlsx(BytesIO(rendered.content)).sheets[0].rows
    assert rows[1][:4] == ["X1900-1", "CAN0_H", "车身CAN0高", ""]
