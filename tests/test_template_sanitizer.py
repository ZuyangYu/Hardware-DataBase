from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from src.document_authoring.template_sanitizer import (
    TemplateSanitizationError,
    sanitize_template,
)


CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
WORDPROCESSING_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _xml(value: str) -> bytes:
    return value.encode()


def _package(
    entries: dict[str, bytes],
    *,
    preserved_info: ZipInfo | None = None,
) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            if preserved_info is not None and name == preserved_info.filename:
                archive.writestr(preserved_info, value)
            else:
                archive.writestr(name, value)
        archive.comment = b"synthetic-package"
    return output.getvalue()


def _relationships(*relationships: str) -> bytes:
    return _xml(
        f'<Relationships xmlns="{RELATIONSHIPS_NS}">'
        f"{''.join(relationships)}"
        "</Relationships>",
    )


def _relationship(
    relationship_id: str,
    relationship_type: str,
    target: str,
    *,
    external: bool = False,
) -> str:
    target_mode = ' TargetMode="External"' if external else ""
    return (
        f'<Relationship Id="{relationship_id}" Type="{OFFICE_REL_NS}/{relationship_type}" '
        f'Target="{target}"{target_mode}/>'
    )


def _content_types(*overrides: tuple[str, str]) -> bytes:
    entries = [
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Default Extension="bin" ContentType="application/octet-stream"/>',
    ]
    entries.extend(
        f'<Override PartName="/{part}" ContentType="{content_type}"/>'
        for part, content_type in overrides
    )
    return _xml(f'<Types xmlns="{CONTENT_TYPES_NS}">{"".join(entries)}</Types>')


def _active_parts(content: bytes) -> list[str]:
    active_fragments = (
        "vbaproject",
        "/externallinks/",
        "/embeddings/",
        "/activex/",
        "/ctrlprops/",
    )
    with ZipFile(BytesIO(content)) as archive:
        return sorted(
            name
            for name in archive.namelist()
            if any(fragment in f"/{name.lower()}" for fragment in active_fragments)
        )


def _external_relationships(content: bytes) -> list[str]:
    with ZipFile(BytesIO(content)) as archive:
        return sorted(
            f"{name}#{relationship_id}"
            for name in archive.namelist()
            if name.endswith(".rels")
            for relationship_id in _external_relationship_ids(archive.read(name))
        )


def _external_relationship_ids(content: bytes) -> list[str]:
    from xml.etree import ElementTree

    root = ElementTree.fromstring(content)
    return [
        relationship.attrib["Id"]
        for relationship in root
        if relationship.attrib.get("TargetMode", "").lower() == "external"
    ]


def _workbook_with_vba_external_link_and_embedded_object() -> bytes:
    preserved_info = ZipInfo("docProps/custom.bin", (2020, 1, 2, 3, 4, 6))
    preserved_info.compress_type = ZIP_STORED
    preserved_info.comment = b"keep-entry-comment"
    preserved_info.extra = b"\x0a\x00\x00\x00"
    preserved_info.external_attr = 0o640 << 16
    return _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                ),
                (
                    "xl/worksheets/sheet1.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                ),
                ("xl/vbaProject.bin", "application/vnd.ms-office.vbaProject"),
                (
                    "xl/externalLinks/externalLink1.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml",
                ),
                ("xl/embeddings/oleObject1.bin", "application/vnd.openxmlformats-officedocument.oleObject"),
                ("xl/activeX/activeX1.bin", "application/vnd.ms-office.activeX"),
                ("xl/ctrlProps/ctrlProp1.xml", "application/vnd.ms-excel.controlproperties+xml"),
            ),
            "_rels/.rels": _relationships(
                _relationship(
                    "rRoot",
                    "officeDocument",
                    "xl/workbook.xml",
                ),
            ),
            "xl/workbook.xml": _xml(
                f'<workbook xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
                '<sheets><sheet name="Safe" sheetId="1" r:id="rSheet"/></sheets>'
                '<externalReferences><externalReference r:id="rExternalLink"/></externalReferences>'
                "</workbook>",
            ),
            "xl/_rels/workbook.xml.rels": _relationships(
                _relationship("rSheet", "worksheet", "worksheets/sheet1.xml"),
                _relationship("rVba", "vbaProject", "vbaProject.bin"),
                _relationship("rExternalLink", "externalLink", "externalLinks/externalLink1.xml"),
            ),
            "xl/worksheets/sheet1.xml": _xml(
                f'<worksheet xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
                "<sheetData/>"
                '<oleObjects><oleObject progId="Package" r:id="rOle"/></oleObjects>'
                '<controls><control shapeId="1" r:id="rControl"/></controls>'
                '<hyperlinks><hyperlink ref="A1" r:id="rWeb"/></hyperlinks>'
                "</worksheet>",
            ),
            "xl/worksheets/_rels/sheet1.xml.rels": _relationships(
                _relationship("rOle", "oleObject", "../embeddings/oleObject1.bin"),
                _relationship("rActiveX", "control", "../activeX/activeX1.bin"),
                _relationship("rControl", "ctrlProp", "../ctrlProps/ctrlProp1.xml"),
                _relationship("rWeb", "hyperlink", "https://example.invalid", external=True),
            ),
            "xl/externalLinks/externalLink1.xml": _xml(
                f'<externalLink xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
                '<externalBook r:id="rFile"/></externalLink>',
            ),
            "xl/externalLinks/_rels/externalLink1.xml.rels": _relationships(
                _relationship("rFile", "externalLinkPath", "file:///unsafe.xlsx", external=True),
            ),
            "xl/vbaProject.bin": b"synthetic-vba",
            "xl/embeddings/oleObject1.bin": b"synthetic-ole",
            "xl/activeX/activeX1.bin": b"synthetic-activex",
            "xl/ctrlProps/ctrlProp1.xml": _xml("<formControl/>"),
            "docProps/custom.bin": b"preserve-exactly",
        },
        preserved_info=preserved_info,
    )


def _docx_with_external_link_and_ole_object() -> bytes:
    return _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "word/document.xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                ),
                ("word/embeddings/oleObject1.bin", "application/vnd.openxmlformats-officedocument.oleObject"),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "word/document.xml"),
            ),
            "word/document.xml": _xml(
                f'<w:document xmlns:w="{WORDPROCESSING_NS}" xmlns:r="{OFFICE_REL_NS}">'
                "<w:body><w:p>"
                '<w:object><o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
                'r:id="rOle"/></w:object>'
                '<w:hyperlink r:id="rWeb"><w:r><w:t>unsafe link</w:t></w:r></w:hyperlink>'
                "<w:r><w:t>safe text</w:t></w:r>"
                "</w:p></w:body></w:document>",
            ),
            "word/_rels/document.xml.rels": _relationships(
                _relationship("rOle", "oleObject", "embeddings/oleObject1.bin"),
                _relationship("rWeb", "hyperlink", "https://example.invalid", external=True),
            ),
            "word/embeddings/oleObject1.bin": b"synthetic-ole",
        },
    )


def _package_with_dangling_relationship() -> bytes:
    return _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
            "xl/_rels/workbook.xml.rels": _relationships(
                _relationship("rMissing", "worksheet", "worksheets/missing.xml"),
            ),
        },
    )


def test_sanitize_xlsm_removes_active_parts_and_returns_xlsx():
    source = _workbook_with_vba_external_link_and_embedded_object()

    result = sanitize_template(source, "xlsm")

    assert result.format == "xlsx"
    assert _active_parts(result.content) == []
    assert _external_relationships(result.content) == []
    assert "xl/vbaProject.bin" in result.removed_parts
    assert result.removed_parts == sorted(result.removed_parts)
    assert result.removed_relationships == sorted(result.removed_relationships)
    with ZipFile(BytesIO(result.content)) as archive:
        content_types = archive.read("[Content_Types].xml")
        workbook = archive.read("xl/workbook.xml")
        worksheet = archive.read("xl/worksheets/sheet1.xml")
        preserved = archive.getinfo("docProps/custom.bin")
        assert b"macroEnabled" not in content_types
        assert b"spreadsheetml.sheet.main+xml" in content_types
        assert b"externalReference" not in workbook
        assert b"oleObject" not in worksheet
        assert b"<control" not in worksheet
        assert b"hyperlink" not in worksheet
        assert archive.read(preserved) == b"preserve-exactly"
        assert preserved.date_time == (2020, 1, 2, 3, 4, 6)
        assert preserved.compress_type == ZIP_STORED
        assert preserved.comment == b"keep-entry-comment"
        assert preserved.external_attr == 0o640 << 16
        assert archive.comment == b"synthetic-package"


def test_sanitize_docx_removes_embedded_object_and_external_relationship():
    result = sanitize_template(_docx_with_external_link_and_ole_object(), "docx")

    assert result.format == "docx"
    assert _active_parts(result.content) == []
    assert _external_relationships(result.content) == []
    with ZipFile(BytesIO(result.content)) as archive:
        document = archive.read("word/document.xml")
        assert b"OLEObject" not in document
        assert b"unsafe link" not in document
        assert b"safe text" in document


def test_sanitize_docx_removes_an_external_relationship_consumed_by_r_link():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "word/document.xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                ),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "word/document.xml"),
            ),
            "word/document.xml": _xml(
                f'<w:document xmlns:w="{WORDPROCESSING_NS}" xmlns:r="{OFFICE_REL_NS}" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                '<w:body><w:drawing><a:blip r:link="rExternalImage"/></w:drawing>'
                "</w:body></w:document>",
            ),
            "word/_rels/document.xml.rels": _relationships(
                _relationship(
                    "rExternalImage",
                    "image",
                    "https://example.invalid/image.png",
                    external=True,
                ),
            ),
        },
    )

    result = sanitize_template(content, "docx")

    with ZipFile(BytesIO(result.content)) as archive:
        assert b"rExternalImage" not in archive.read("word/document.xml")


def test_sanitize_xlsx_removes_a_form_control_vml_content_type():
    content_types = _content_types(
        (
            "xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        ),
        (
            "xl/worksheets/sheet1.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        ),
    ).replace(
        b"</Types>",
        b'<Default Extension="vml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/></Types>',
    )
    content = _package(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(
                f'<workbook xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
                '<sheets><sheet name="Safe" sheetId="1" r:id="rSheet"/></sheets>'
                "</workbook>",
            ),
            "xl/_rels/workbook.xml.rels": _relationships(
                _relationship("rSheet", "worksheet", "worksheets/sheet1.xml"),
            ),
            "xl/worksheets/sheet1.xml": _xml(
                f'<worksheet xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
                '<sheetData/><legacyDrawing r:id="rVml"/></worksheet>',
            ),
            "xl/worksheets/_rels/sheet1.xml.rels": _relationships(
                _relationship("rVml", "vmlDrawing", "../drawings/vmlDrawing1.vml"),
            ),
            "xl/drawings/vmlDrawing1.vml": _xml(
                '<xml xmlns:x="urn:schemas-microsoft-com:office:excel">'
                '<shape><x:ClientData ObjectType="Button"/></shape></xml>',
            ),
        },
    )

    result = sanitize_template(content, "xlsx")

    with ZipFile(BytesIO(result.content)) as archive:
        assert "xl/drawings/vmlDrawing1.vml" not in archive.namelist()
        assert b'Extension="vml"' not in archive.read("[Content_Types].xml")
        assert b"legacyDrawing" not in archive.read("xl/worksheets/sheet1.xml")


def test_sanitize_xlsx_removes_active_vml_renamed_with_an_xml_suffix():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
                (
                    "xl/worksheets/sheet1.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                ),
                (
                    "xl/drawings/control.xml",
                    "application/vnd.openxmlformats-officedocument.vmlDrawing",
                ),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(
                f'<workbook xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
                '<sheets><sheet name="Safe" sheetId="1" r:id="rSheet"/></sheets>'
                "</workbook>",
            ),
            "xl/_rels/workbook.xml.rels": _relationships(
                _relationship("rSheet", "worksheet", "worksheets/sheet1.xml"),
            ),
            "xl/worksheets/sheet1.xml": _xml(
                f'<worksheet xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
                '<sheetData/><legacyDrawing r:id="rVml"/></worksheet>',
            ),
            "xl/worksheets/_rels/sheet1.xml.rels": _relationships(
                _relationship("rVml", "vmlDrawing", "../drawings/control.xml"),
            ),
            "xl/drawings/control.xml": _xml(
                '<xml xmlns:x="urn:schemas-microsoft-com:office:excel">'
                '<shape><x:ClientData ObjectType="Button"/></shape></xml>',
            ),
        },
    )

    result = sanitize_template(content, "xlsx")

    assert "xl/drawings/control.xml" in result.removed_parts
    with ZipFile(BytesIO(result.content)) as archive:
        assert "xl/drawings/control.xml" not in archive.namelist()
        assert b"/xl/drawings/control.xml" not in archive.read("[Content_Types].xml")
        assert b"rVml" not in archive.read("xl/worksheets/_rels/sheet1.xml.rels")
        assert b"legacyDrawing" not in archive.read("xl/worksheets/sheet1.xml")


def test_sanitize_rejects_a_package_with_a_dangling_relationship():
    with pytest.raises(TemplateSanitizationError, match="dangling"):
        sanitize_template(_package_with_dangling_relationship(), "xlsx")


def test_sanitize_rejects_a_package_without_an_office_document_root_relationship():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
            ),
            "_rels/.rels": _relationships(),
            "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
        },
    )

    with pytest.raises(TemplateSanitizationError, match="package root relationship"):
        sanitize_template(content, "xlsx")


def test_sanitize_rejects_a_dangling_content_type_override():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
                ("xl/activeX/missing.bin", "application/vnd.ms-office.activeX"),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
        },
    )

    with pytest.raises(TemplateSanitizationError, match="dangling content type"):
        sanitize_template(content, "xlsx")


def test_sanitize_rejects_residual_active_xml_without_a_relationship():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "word/document.xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                ),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "word/document.xml"),
            ),
            "word/document.xml": _xml(
                f'<w:document xmlns:w="{WORDPROCESSING_NS}">'
                "<w:body><w:object/></w:body></w:document>",
            ),
        },
    )

    with pytest.raises(TemplateSanitizationError, match="residual active XML"):
        sanitize_template(content, "docx")


@pytest.mark.parametrize(
    ("relationships", "workbook", "message"),
    [
        (
            _xml(
                '<Relationships xmlns="urn:not-opc">'
                '<Relationship Id="rRoot" Type="urn:evil/officeDocument" '
                'Target="xl/workbook.xml"/></Relationships>',
            ),
            _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
            "namespace",
        ),
        (
            _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            _xml(f'<notAWorkbook xmlns="{SPREADSHEET_NS}"/>'),
            "main part",
        ),
        (
            _relationships(
                _relationship("rRoot1", "officeDocument", "xl/workbook.xml"),
                _relationship("rRoot2", "officeDocument", "xl/workbook.xml"),
            ),
            _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
            "exactly one",
        ),
    ],
)
def test_sanitize_rejects_spoofed_or_ambiguous_package_roots(
    relationships,
    workbook,
    message,
):
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
            ),
            "_rels/.rels": relationships,
            "xl/workbook.xml": workbook,
        },
    )

    with pytest.raises(TemplateSanitizationError, match=message):
        sanitize_template(content, "xlsx")


def test_sanitize_rejects_a_part_without_content_type_coverage():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
            "xl/unsupported/payload.unknown": b"untyped",
        },
    )

    with pytest.raises(TemplateSanitizationError, match="content type"):
        sanitize_template(content, "xlsx")


def test_sanitize_rejects_malformed_xml_declared_with_a_non_xml_suffix():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
                ("custom/data.dat", "application/xml"),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
            "custom/data.dat": b"<malformed",
        },
    )

    with pytest.raises(TemplateSanitizationError, match="XML"):
        sanitize_template(content, "xlsx")


def test_sanitize_rejects_a_noncanonical_content_type_part_name():
    content_types = _content_types(
        (
            "xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        ),
    ).replace(b"/xl/workbook.xml", b"/xl/folder/../workbook.xml")
    content = _package(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
        },
    )

    with pytest.raises(TemplateSanitizationError, match="PartName"):
        sanitize_template(content, "xlsx")


def test_sanitize_rejects_a_utf16_doctype():
    workbook = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<!DOCTYPE workbook [<!ENTITY active "unsafe">]>'
        f'<workbook xmlns="{SPREADSHEET_NS}">&active;</workbook>'
    ).encode("utf-16")
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": workbook,
        },
    )

    with pytest.raises(TemplateSanitizationError, match="DTD|entities"):
        sanitize_template(content, "xlsx")


def test_sanitize_rejects_a_high_compression_ratio_package():
    content = _package(
        {
            "[Content_Types].xml": _content_types(
                (
                    "xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
            ),
            "_rels/.rels": _relationships(
                _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
            ),
            "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
            "docProps/padding.bin": b"\x00" * 2_000_000,
        },
    )

    with pytest.raises(TemplateSanitizationError, match="resource limit"):
        sanitize_template(content, "xlsx")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not a zip", "ZIP"),
        (
                _package(
                    {
                        "[Content_Types].xml": _xml("<Types"),
                        "_rels/.rels": _relationships(
                            _relationship("rRoot", "officeDocument", "xl/workbook.xml"),
                        ),
                        "xl/workbook.xml": _xml(f'<workbook xmlns="{SPREADSHEET_NS}"/>'),
                    },
                ),
            "XML",
        ),
        (
            _package({"[Content_Types].xml": _content_types()}),
            "package root",
        ),
    ],
)
def test_sanitize_fails_closed_for_malformed_or_incomplete_packages(content, message):
    with pytest.raises(TemplateSanitizationError, match=message):
        sanitize_template(content, "xlsx")
