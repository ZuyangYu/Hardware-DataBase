"""Regression tests for generated Office package integrity.

Excel reports the worksheet part as the damaged component when a generated
workbook contains XML 1.0 control characters.  These tests cover both the
shared package validator and the two application paths that publish/download
generated workbooks.
"""

from __future__ import annotations

import io
import types
import zipfile
from xml.etree import ElementTree as ET

import pytest

from src.document_authoring.models import RendererPolicy, WorkbookFill, WorkbookFillPlan, WorkbookRegionSchema
from src.document_authoring.renderers.xlsm import MAIN_NS, XlsmRenderer
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.ooxml import sanitize_xml10_text, validate_ooxml_package
from src.document_authoring.template_analysis import workbook_value_hash
from src.result_exports.models import ResultEnvelope
from src.result_exports.renderers import render_result


def _envelope_with_controls() -> ResultEnvelope:
    control = "\x0b"
    return ResultEnvelope(
        title="控制字符测试",
        query="测试",
        answer=f"正文前{control}正文后\x0c",
        tables=[
            {
                "name": "结果",
                "columns": ["列"],
                "rows": [[f"单元格前{control}单元格后"]],
            }
        ],
    )


def _replace_zip_member(content: bytes, member: str, replacement) -> bytes:
    source = io.BytesIO(content)
    output = io.BytesIO()
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rebuilt:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == member:
                data = replacement(data)
            rebuilt.writestr(info, data)
    return output.getvalue()


def _valid_xlsx() -> bytes:
    return render_result(
        ResultEnvelope(title="标题", query="查询", answer="正文"),
        "xlsx",
    ).content


def _namespaced_template() -> bytes:
    parts = {
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''',
        "_rels/.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        "xl/workbook.xml": b'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": b'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
 mc:Ignorable="x14ac xr xr2 xr3"
 xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
 xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision"
 xmlns:xr2="http://schemas.microsoft.com/office/spreadsheetml/2015/revision2"
 xmlns:xr3="http://schemas.microsoft.com/office/spreadsheetml/2016/revision3"
 xr:uid="{00000000-0001-0000-0000-000000000000}">
<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{{summary}}</t></is></c></row></sheetData></worksheet>''',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_sanitize_xml10_text_replaces_invalid_controls_and_keeps_layout_whitespace():
    value = "前\t中\n行\r后\x0b坏\x0c坏\x00坏\ufffe坏"

    assert sanitize_xml10_text(value) == "前\t中\n行\r后�坏�坏�坏�坏"


def test_result_xlsx_renderer_sanitizes_controls_and_is_parseable():
    rendered = render_result(_envelope_with_controls(), "xlsx")

    validate_ooxml_package(rendered.content, "xlsx")
    with zipfile.ZipFile(io.BytesIO(rendered.content)) as archive:
        worksheet_parts = [
            archive.read(info.filename)
            for info in archive.infolist()
            if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml")
        ]
    assert worksheet_parts
    assert all(b"\x0b" not in part and b"\x0c" not in part and b"\x00" not in part for part in worksheet_parts)
    assert any("�" in part.decode("utf-8") for part in worksheet_parts)


def test_ooxml_validator_reports_invalid_worksheet_part():
    corrupted = _replace_zip_member(
        _valid_xlsx(),
        "xl/worksheets/sheet1.xml",
        lambda data: data.replace(b"</worksheet>", b"\x0b</worksheet>", 1),
    )

    with pytest.raises(ValueError, match=r"sheet1\.xml"):
        validate_ooxml_package(corrupted, "xlsx")


def test_ooxml_validator_rejects_undeclared_markup_compatibility_prefix():
    corrupted = _replace_zip_member(
        _namespaced_template(),
        "xl/worksheets/sheet1.xml",
        lambda data: data.replace(
            b' xmlns:xr3="http://schemas.microsoft.com/office/spreadsheetml/2016/revision3"',
            b"",
        ),
    )

    with pytest.raises(ValueError, match=r"xr3|Ignorable"):
        validate_ooxml_package(corrupted, "xlsx")


def test_xlsm_renderer_sanitizes_inline_cell_text_before_xml_serialization():
    cell = ET.Element(f"{{{MAIN_NS}}}c", {"r": "A1"})

    XlsmRenderer._set_inline_string(cell, "前\x0b后")

    serialized = ET.tostring(cell, encoding="utf-8")
    parsed = ET.fromstring(serialized)
    assert "前�后" == "".join(parsed.itertext())


def test_xlsm_renderer_preserves_markup_compatibility_namespace_prefixes():
    region = WorkbookRegionSchema(
        region_id="region-a1",
        sheet_name="Review",
        locator={"cell": "A1"},
        role="semantic_draft",
        write_policy="validated_draft",
        expected_value_hash=workbook_value_hash("{{summary}}"),
        allow_nonempty_overwrite=True,
    )
    plan = WorkbookFillPlan(
        template_version_id="template-1",
        fills=[WorkbookFill(region_id="region-a1", value="生成内容", semantic_unit_id="summary")],
    )

    result = XlsmRenderer().render(
        _namespaced_template(),
        [region],
        plan,
        RendererPolicy(renderer_policy_id="renderer-1"),
        security_approved=True,
    )
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml")
    declared_prefixes = {
        prefix
        for _event, prefix_uri in ET.iterparse(io.BytesIO(sheet), events=("start-ns",))
        for prefix, _uri in [prefix_uri]
    }
    root = ET.fromstring(sheet)
    ignorable = root.attrib["{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable"]
    assert set(ignorable.split()).issubset(declared_prefixes)


def test_document_download_rejects_a_corrupt_legacy_workbook():
    corrupted = _replace_zip_member(
        _valid_xlsx(),
        "xl/worksheets/sheet1.xml",
        lambda data: data.replace(b"</worksheet>", b"\x0b</worksheet>", 1),
    )
    service = object.__new__(DocumentGenerationService)
    artifact = types.SimpleNamespace(
        artifact_id="artifact-1",
        work_order_id="work-order-1",
        stage="review_candidate",
        output_format="xlsx",
    )
    order = types.SimpleNamespace(work_order_id="work-order-1", target_format="xlsx")
    service._artifact_for_context = lambda _ctx, _artifact_id: artifact
    service._order_raw = lambda _work_order_id: order
    service.require_work_order_capability = lambda *_args: None
    service.store = types.SimpleNamespace(read_artifact_content=lambda _artifact_id: corrupted)

    with pytest.raises(ValueError, match=r"sheet1\.xml"):
        service.download_document_artifact("ctx", "artifact-1")
