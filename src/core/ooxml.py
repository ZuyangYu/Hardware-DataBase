"""Small, dependency-free OOXML safety and integrity helpers.

Office Open XML stores worksheet/document text as XML 1.0.  Python strings can
contain control characters that are valid in application data but forbidden
by XML 1.0 (for example vertical tab and form feed).  Serializing those values
directly produces a ZIP that has a valid container but cannot be opened by
Excel/Word.  This module keeps the conversion policy in one place and checks
every XML package part before an artifact crosses an application boundary.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET


_XML10_REPLACEMENT = "\ufffd"
_MARKUP_COMPATIBILITY_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_SUPPORTED_OFFICE_FORMATS = frozenset({"xlsx", "xlsm", "docx", "pptx"})
_REQUIRED_PACKAGE_PARTS = {
    "xlsx": frozenset({"[Content_Types].xml", "xl/workbook.xml"}),
    "xlsm": frozenset({"[Content_Types].xml", "xl/workbook.xml"}),
    "docx": frozenset({"[Content_Types].xml", "word/document.xml"}),
    "pptx": frozenset({"[Content_Types].xml", "ppt/presentation.xml"}),
}


def sanitize_xml10_text(value: object) -> str:
    """Return *value* with characters forbidden by XML 1.0 replaced.

    Tabs, line feeds and carriage returns are intentionally retained because
    they are legal XML whitespace and carry useful document layout.  Other
    C0 controls, unpaired surrogates and noncharacters U+FFFE/U+FFFF are
    replaced with U+FFFD so the surrounding text remains readable and the
    generated package remains parseable.
    """

    text = "" if value is None else str(value)
    return "".join(
        character if _is_xml10_codepoint(ord(character)) else _XML10_REPLACEMENT
        for character in text
    )


def _is_xml10_codepoint(codepoint: int) -> bool:
    return (
        codepoint in {0x09, 0x0A, 0x0D}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def validate_ooxml_package(
    content: bytes,
    format: str,
    *,
    require_content_types: bool = True,
) -> None:
    """Fail closed when an Office OOXML package or any XML part is invalid.

    The validator deliberately checks all ``.xml`` and ``.rels`` members, not
    only the main document part.  A malformed worksheet or relationship can
    make the whole workbook unopenable even when ``ZipFile`` reports a healthy
    archive.  Security checks for macros/external links remain owned by the
    existing template/export policy layers.
    """

    normalized = str(format or "").strip().lower().lstrip(".")
    if normalized not in _SUPPORTED_OFFICE_FORMATS:
        raise ValueError(f"unsupported OOXML format: {format}")
    if not isinstance(content, (bytes, bytearray, memoryview)) or not content:
        raise ValueError("artifact OOXML package is empty")

    try:
        with zipfile.ZipFile(io.BytesIO(bytes(content)), "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("artifact OOXML package contains duplicate parts")
            try:
                corrupt_member = archive.testzip()
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ValueError("artifact OOXML package has an unreadable member") from exc
            if corrupt_member is not None:
                raise ValueError(f"artifact OOXML package has a corrupt member: {corrupt_member}")

            required = set(_REQUIRED_PACKAGE_PARTS[normalized])
            if not require_content_types:
                required.discard("[Content_Types].xml")
            if not required.issubset(set(names)):
                raise ValueError("artifact OOXML package is missing required parts")

            for name in names:
                if not (name.lower().endswith(".xml") or name.lower().endswith(".rels")):
                    continue
                try:
                    _validate_xml_part(name, archive.read(name))
                except (ET.ParseError, UnicodeError, ValueError) as exc:
                    raise ValueError(f"artifact XML is invalid: {name}: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("artifact OOXML package is not a valid ZIP") from exc
    except OSError as exc:
        raise ValueError("artifact OOXML package could not be read") from exc


def _validate_xml_part(name: str, content: bytes) -> None:
    """Parse one XML part and validate markup-compatibility prefix values."""

    root = ET.fromstring(content)
    declared_prefixes = {
        prefix or ""
        for _event, (prefix, _uri) in ET.iterparse(
            io.BytesIO(content), events=("start-ns",)
        )
    }
    ignorable_attribute = f"{{{_MARKUP_COMPATIBILITY_NS}}}Ignorable"
    for element in root.iter():
        value = element.attrib.get(ignorable_attribute)
        if value is None:
            continue
        missing = sorted(set(value.split()) - declared_prefixes)
        if missing:
            raise ValueError(
                f"{name} has undeclared mc:Ignorable prefix(es): {', '.join(missing)}"
            )
