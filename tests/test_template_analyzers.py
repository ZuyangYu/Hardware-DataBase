from __future__ import annotations

import io
import zipfile

from src.document_authoring.template_analyzers import analyze_template


def _package(parts: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def _replace_part(content: bytes, name: str, before: bytes, after: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        parts = {part_name: archive.read(part_name) for part_name in archive.namelist()}
    parts[name] = parts[name].replace(before, after, 1)
    return _package(parts)


def _xlsm_with_formula_and_vba() -> bytes:
    return _package({
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''',
        "_rels/.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        "xl/workbook.xml": b'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": b'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData><row r="1"><c r="A1"><f>SUM(B1:B2)</f><v>3</v></c><c r="B1" t="inlineStr"><is><t>editable</t></is></c></row></sheetData></worksheet>''',
        "xl/vbaProject.bin": b"not-sent-to-an-llm",
    })


def _xlsx_with_merged_and_protected_cells() -> bytes:
    return _package({
        "xl/workbook.xml": b'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": b'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetProtection sheet="1"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>anchor</t></is></c><c r="B1" t="inlineStr"><is><t>merged</t></is></c></row></sheetData><mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells></worksheet>''',
    })


def _docx_with_paragraph_table_and_external_link() -> bytes:
    return _package({
        "word/document.xml": b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<w:body><w:p><w:r><w:t>Summary</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>''',
        "word/_rels/document.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.test" TargetMode="External"/>
</Relationships>''',
    })


def test_xlsm_analysis_never_marks_formula_or_active_content_cell_writable():
    analysis = analyze_template(_xlsm_with_formula_and_vba(), "xlsm")

    by_id = {unit.unit_id: unit for unit in analysis.units}
    assert by_id["sheet:Review!A1"].writable is False
    assert by_id["sheet:Review!A1"].blocked_reason == "formula"
    assert by_id["sheet:Review!B1"].writable is False
    assert by_id["sheet:Review!B1"].blocked_reason == "active_content"
    assert analysis.status == "requires_human"


def test_xlsx_analysis_blocks_protected_and_merged_non_anchor_cells():
    analysis = analyze_template(_xlsx_with_merged_and_protected_cells(), "xlsx")

    by_id = {unit.unit_id: unit for unit in analysis.units}
    assert by_id["sheet:Review!A1"].blocked_reason == "protected"
    assert by_id["sheet:Review!B1"].blocked_reason == "merged_non_anchor"
    assert analysis.status == "ready_for_confirmation"


def test_xlsx_analysis_fails_closed_for_an_unknown_style_in_a_protected_sheet():
    content = _replace_part(
        _xlsx_with_merged_and_protected_cells(),
        "xl/worksheets/sheet1.xml",
        b'<c r="A1"',
        b'<c r="A1" s="999"',
    )

    analysis = analyze_template(content, "xlsx")

    by_id = {unit.unit_id: unit for unit in analysis.units}
    assert by_id["sheet:Review!A1"].blocked_reason == "protected"


def test_docx_analysis_exposes_paragraph_and_table_cells_but_protects_external_relationships():
    analysis = analyze_template(_docx_with_paragraph_table_and_external_link(), "docx")

    assert any(unit.locator == {"paragraph_index": 0} for unit in analysis.units)
    assert any(
        unit.locator == {"table_index": 0, "row_index": 0, "cell_index": 0}
        for unit in analysis.units
    )
    assert all(unit.writable is False for unit in analysis.units if unit.blocked_reason == "external_relationship")
    assert analysis.status == "requires_human"
