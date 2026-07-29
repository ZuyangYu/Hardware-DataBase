from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

from src.document_authoring.icd_generation import (
    build_front_view_fills,
    connector_refdes_from_front_view_template,
    render_icd_front_views,
)
from src.document_authoring.models import RendererPolicy, ValidationReport, WorkbookFillPlan
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.icd_scope_decision import IcdScopeDecision
from src.document_authoring.models import IcdScopeReview
from src.document_authoring.service import DocumentGenerationService
from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx


def _xlsx(path: Path, rows: list[list[str]], *, merged: str = "") -> None:
    def cell(column: str, row: int, value: str) -> str:
        return (
            f'<c r="{column}{row}" s="7" t="inlineStr"><is><t>{value}'
            "</t></is></c>"
        )

    sheet_rows = "".join(
        f'<row r="{row_number}">' + "".join(
            cell(chr(ord("A") + column_number), row_number, value)
            for column_number, value in enumerate(row)
            if value
        ) + "</row>"
        for row_number, row in enumerate(rows, start=1)
    )
    merge_xml = f'<mergeCells count="1"><mergeCell ref="{merged}"/></mergeCells>' if merged else ""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="接口" sheetId="1" r:id="rId1"/></sheets></workbook>',
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
            f"<sheetData>{sheet_rows}</sheetData>{merge_xml}</worksheet>",
        )


def _values(content: bytes, tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "generated.xlsx"
    path.write_bytes(content)
    return parse_xlsx(str(path)).sheets[0].rows


def test_front_view_fills_definitions_and_pin_numbers_without_reordering_slots(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    _xlsx(template, [
        ["板端接插件前视图管序布局和定义"],
        ["管脚定义 Pin Definition", "旧3", "旧2", "旧1"],
        ["管脚号 Pin Number", "X1900-3", "X1900-2", "X1900-1"],
        ["板端接插件序号", "99", "99", "99"],
        ["管脚号 Pin Number", "X1900-4", "X1900-5", ""],
        ["管脚定义 Pin Definition", "旧4", "旧5", ""],
    ], merged="A1:D1")
    mappings = [
        {"refdes": "X1900", "pin_name": str(pin), "net_name": net}
        for pin, net in [("1", "CAN_H"), ("2", "CAN_L"), ("3", "NC"), ("4", "ETH_P"), ("5", "GND")]
    ]

    front_view = build_front_view_fills(parse_xlsx(str(template)), mappings)

    assert front_view.issues == []
    assert [(fill.region_id, fill.value) for fill in front_view.fills] == [
        ("icd-front-view-1-b2-definition", "NC"),
        ("icd-front-view-1-b3-pin", "X1900-3"),
        ("icd-front-view-1-b4-board-pin", "3"),
        ("icd-front-view-1-c2-definition", "CAN_L"),
        ("icd-front-view-1-c3-pin", "X1900-2"),
        ("icd-front-view-1-c4-board-pin", "2"),
        ("icd-front-view-1-d2-definition", "CAN_H"),
        ("icd-front-view-1-d3-pin", "X1900-1"),
        ("icd-front-view-1-d4-board-pin", "1"),
        ("icd-front-view-1-b6-definition", "ETH_P"),
        ("icd-front-view-1-b5-pin", "X1900-4"),
        ("icd-front-view-1-c6-definition", "GND"),
        ("icd-front-view-1-c5-pin", "X1900-5"),
    ]

    # A physical slot has one board-pin cell shared by its top/bottom depiction.
    # The lower pin cells are a second row of the same layout, not a second column.
    # Rendering validates that only layout values change; merge/style/layout remain.
    result = XlsmRenderer().render(
        template.read_bytes(),
        front_view.regions,
        WorkbookFillPlan(template_version_id="test", fills=front_view.fills),
        RendererPolicy(renderer_policy_id="test"),
        security_approved=True,
    )
    rows = _values(result.content, tmp_path)
    assert rows[1][1:] == ["NC", "CAN_L", "CAN_H"]
    assert rows[2][1:] == ["X1900-3", "X1900-2", "X1900-1"]
    assert rows[3][1:] == ["3", "2", "1"]
    assert rows[5][1:3] == ["ETH_P", "GND"]
    with zipfile.ZipFile(BytesIO(result.content)) as archive:
        assert '<mergeCell ref="A1:D1"' in archive.read("xl/worksheets/sheet1.xml").decode()


def test_front_view_routes_multiple_connector_layouts_by_embedded_refdes(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    _xlsx(template, [
        ["板端接插件前视图管序布局和定义"],
        ["管脚定义 Pin Definition", "旧"],
        ["管脚号 Pin Number", "X1900-1"],
        ["板端接插件序号", "1"],
        ["管脚定义 Pin Definition", "旧"],
        ["管脚号 Pin Number", "X1902-1"],
        ["板端接插件序号", "1"],
    ])

    front_view = build_front_view_fills(parse_xlsx(str(template)), [
        {"refdes": "X1900", "pin_name": "1", "net_name": "CAN_H"},
        {"refdes": "X1902", "pin_name": "1", "net_name": "ETH_P"},
    ])

    assert front_view.issues == []
    values = {fill.region_id: fill.value for fill in front_view.fills}
    assert values["icd-front-view-1-b2-definition"] == "CAN_H"
    assert values["icd-front-view-2-b5-definition"] == "ETH_P"


def test_front_view_artifact_shim_uses_frozen_facts_without_a_template_path(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    _xlsx(template, [
        ["板端接插件前视图管序布局和定义"],
        ["管脚定义 Pin Definition", "旧"],
        ["管脚号 Pin Number", "X1902-1"],
        ["板端接插件序号", "1"],
    ])

    rendered = render_icd_front_views(
        template.read_bytes(),
        [{"refdes": "X1902", "pin_name": "1", "net_name": "ETH_P"}],
    )

    assert rendered.issues == []
    assert rendered.detected_layout_count == 1
    assert _values(rendered.content, tmp_path)[1][1] == "ETH_P"


def test_front_view_connector_parser_reads_explicit_refdes_from_template_bytes(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    _xlsx(template, [
        ["板端接插件前视图管序布局和定义"],
        ["管脚定义 Pin Definition", "旧"],
        ["管脚号 Pin Number", "X302-20"],
        ["板端接插件序号", "20"],
    ])

    assert connector_refdes_from_front_view_template(template.read_bytes()) == ["X302"]


def test_front_view_connector_parser_ignores_non_layout_template_bytes(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    _xlsx(template, [["Pin Definition"], ["plain table"]])

    assert connector_refdes_from_front_view_template(template.read_bytes()) == []


def test_harness_finalization_overlay_uses_the_frozen_scope_not_writer_order(tmp_path: Path):
    template_path = tmp_path / "template.xlsx"
    _xlsx(template_path, [
        ["板端接插件前视图管序布局和定义"],
        ["管脚定义 Pin Definition", "旧"],
        ["管脚号 Pin Number", "X1902-1"],
        ["板端接插件序号", "1"],
    ])
    review = IcdScopeReview(
        work_order_id="work-front-view",
        source_snapshot_hash="snapshot",
        status="frozen",
        decision=IcdScopeDecision(frozen_pin_mappings=[{
            "refdes": "X1902", "pin_name": "1", "net_name": "ETH_P",
        }]),
    )
    service = object.__new__(DocumentGenerationService)
    service.store = Mock()
    service.store.get_icd_scope_review.return_value = review
    content, manifest, issues = service._apply_icd_front_view_layout(
        SimpleNamespace(work_order_id="work-front-view"),
        SimpleNamespace(format="xlsx", template_version_id="template-front-view"),
        template_path.read_bytes(),
        {
            "manifest_hash": "base-manifest",
            "changed_parts": [],
            "policy_violations": [],
            "cell_policy_violations": [],
            "cell_changes": [],
            "table_row_operations": [],
        },
    )

    assert issues == []
    assert manifest["manifest_hash"] != "base-manifest"
    assert _values(content, tmp_path)[1][1] == "ETH_P"


def test_harness_front_view_issue_becomes_an_icd_approval_blocker(tmp_path: Path):
    template_path = tmp_path / "template.xlsx"
    _xlsx(template_path, [
        ["板端接插件前视图管序布局和定义"],
        ["管脚定义 Pin Definition", "旧"],
        ["管脚号 Pin Number", "X1902-99"],
        ["板端接插件序号", "99"],
    ])
    review = IcdScopeReview(
        work_order_id="work-front-view-invalid",
        source_snapshot_hash="snapshot",
        status="frozen",
        decision=IcdScopeDecision(frozen_pin_mappings=[{
            "refdes": "X1902", "pin_name": "1", "net_name": "ETH_P",
        }]),
    )
    service = object.__new__(DocumentGenerationService)
    service.store = Mock()
    service.store.get_icd_scope_review.return_value = review
    content, _manifest, issues = service._apply_icd_front_view_layout(
        SimpleNamespace(work_order_id="work-front-view-invalid"),
        SimpleNamespace(format="xlsx", template_version_id="template-front-view"),
        template_path.read_bytes(),
        {"manifest_hash": "base-manifest"},
    )
    report = service._append_icd_validation_issues(
        ValidationReport(
            validation_report_id="report-front-view", work_order_id="work-front-view-invalid",
            status="passed", evidence_matrix_hash="matrix",
        ),
        issues,
    )

    assert content == template_path.read_bytes()
    assert report.status == "requires_human"
    assert service._has_icd_blocking_issue(report.issues)


def test_front_view_reports_unknown_or_unparsed_slots_as_blocking(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    _xlsx(template, [
        ["板端接插件前视图管序布局和定义"],
        ["管脚定义 Pin Definition", "旧"],
        ["管脚号 Pin Number", "X1900-99"],
        ["板端接插件序号", "99"],
        ["管脚定义 Pin Definition", "旧"],
        ["板端接插件序号", "2"],
    ])

    front_view = build_front_view_fills(parse_xlsx(str(template)), [
        {"refdes": "X1900", "pin_name": "1", "net_name": "CAN_H"},
    ])

    assert front_view.fills == []
    assert front_view.issues == [
        {
            "code": "icd_front_view_unknown_pin",
            "severity": "blocking",
            "layout_id": "icd-front-view-1",
            "cell": "B3",
            "refdes": "X1900",
            "pin_name": "99",
            "message": "前视图格位引用的管脚不在冻结 ICD 范围中。",
        },
        {
            "code": "icd_front_view_unresolved_layout",
            "severity": "blocking",
            "layout_id": "icd-front-view-2",
            "message": "前视图缺少可解析的“管脚号 Pin Number”格位；请在模板中保留例如 X1900-1 的管脚号。",
        },
    ]
