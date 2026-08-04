from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from src.document_authoring.artifact_preview import preview_artifact


def _xlsx_bytes() -> bytes:
    workbook = '''<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets>{sheets}</sheets>
    </workbook>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}</Relationships>'''
    sheet_xml = '''<?xml version="1.0" encoding="UTF-8"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>'''
    sheets = "".join(
        f'<sheet name="Sheet {index}" sheetId="{index}" r:id="rId{index}"/>'
        for index in range(1, 5)
    )
    rels = "".join(
        '<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet{0}.xml"/>'.format(index)
        for index in range(1, 5)
    )
    rows = "".join(
        "<row r=\"{row}\">{cells}</row>".format(
            row=row,
            cells="".join(
                '<c r="{column}{row}" t="inlineStr"><is><t>R{row}C{column}</t></is></c>'.format(
                    column=chr(64 + column), row=row,
                )
                for column in range(1, 14 if row == 1 else 2)
            ),
        )
        for row in range(1, 52)
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook.format(sheets=sheets))
        archive.writestr("xl/_rels/workbook.xml.rels", relationships.format(relationships=rels))
        for index in range(1, 5):
            archive.writestr("xl/worksheets/sheet{0}.xml".format(index), sheet_xml.format(rows=rows))
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:r><w:t>第一段</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格单元</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      </w:body>
    </w:document>'''
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_xlsx_preview_is_bounded_and_marks_truncation():
    preview = preview_artifact(_xlsx_bytes(), "xlsx")

    assert preview["format"] == "xlsx"
    assert preview["truncated"] is True
    assert [sheet["name"] for sheet in preview["sheets"]] == ["Sheet 1", "Sheet 2", "Sheet 3"]
    assert len(preview["sheets"][0]["rows"]) == 50
    assert preview["sheets"][0]["rows"][0] == [f"R1C{chr(64 + index)}" for index in range(1, 13)]


def test_docx_preview_exposes_paragraph_and_table_text():
    preview = preview_artifact(_docx_bytes(), "docx")

    assert preview == {
        "format": "docx",
        "truncated": False,
        "warnings": [],
        "paragraphs": ["第一段"],
        "tables": [[ ["表格单元"] ]],
    }


def test_preview_returns_a_safe_warning_for_invalid_content():
    preview = preview_artifact(b"not an office package", "xlsx")

    assert preview["format"] == "xlsx"
    assert preview["sheets"] == []
    assert preview["warnings"]
