"""Bounded, read-only previews for generated Office artifacts."""

from __future__ import annotations

from io import BytesIO
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def preview_artifact(
    content: bytes,
    artifact_format: str,
    *,
    max_sheets: int = 3,
    max_rows: int = 50,
    max_columns: int = 12,
    max_paragraphs: int = 100,
) -> dict[str, Any]:
    """Return a compact, non-executable preview for an Office artifact.

    The caller owns authorization and artifact lookup.  This function only
    reads supplied bytes; malformed packages become a warning instead of an
    exception so the review UI remains usable.
    """

    normalized_format = artifact_format.lower().lstrip(".")
    if normalized_format in {"xlsx", "xlsm"}:
        result = {"format": normalized_format, "truncated": False, "warnings": [], "sheets": []}
        try:
            with ZipFile(BytesIO(content)) as archive:
                return _preview_xlsx(archive, result, max_sheets, max_rows, max_columns)
        except (BadZipFile, KeyError, ElementTree.ParseError, ValueError) as exc:
            result["warnings"].append(_warning(exc))
            return result
    if normalized_format == "docx":
        result = {"format": normalized_format, "truncated": False, "warnings": [], "paragraphs": [], "tables": []}
        try:
            with ZipFile(BytesIO(content)) as archive:
                return _preview_docx(archive, result, max_rows, max_columns, max_paragraphs)
        except (BadZipFile, KeyError, ElementTree.ParseError, ValueError) as exc:
            result["warnings"].append(_warning(exc))
            return result
    return {
        "format": normalized_format or "unknown",
        "truncated": False,
        "warnings": ["暂不支持该制品格式的预览。"],
    }


def _preview_xlsx(
    archive: ZipFile,
    result: dict[str, Any],
    max_sheets: int,
    max_rows: int,
    max_columns: int,
) -> dict[str, Any]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relationship.attrib.get("Id", ""): relationship.attrib.get("Target", "")
        for relationship in relationships.findall(f"{_PACKAGE_REL_NS}Relationship")
    }
    shared_strings = _shared_strings(archive)
    sheets = workbook.findall(f".//{_SHEET_NS}sheet")
    if len(sheets) > max_sheets:
        result["truncated"] = True
    for sheet in sheets[:max_sheets]:
        target = targets.get(sheet.attrib.get(f"{_REL_NS}id", ""))
        if not target:
            result["warnings"].append(f"工作表 {sheet.attrib.get('name', '')} 缺少关系目标。")
            continue
        path = target.lstrip("/")
        if not path.startswith("xl/"):
            path = f"xl/{path}"
        worksheet = ElementTree.fromstring(archive.read(path))
        rows, was_truncated = _worksheet_rows(worksheet, shared_strings, max_rows, max_columns)
        result["truncated"] = bool(result["truncated"] or was_truncated)
        result["sheets"].append({"name": sheet.attrib.get("name", "未命名工作表"), "rows": rows})
    return result


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.itertext()) for node in root.findall(f"{_SHEET_NS}si")]


def _worksheet_rows(
    worksheet: ElementTree.Element,
    shared_strings: list[str],
    max_rows: int,
    max_columns: int,
) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    truncated = False
    for row_index, row in enumerate(worksheet.findall(f".//{_SHEET_NS}row"), start=1):
        if row_index > max_rows:
            truncated = True
            break
        values: dict[int, str] = {}
        max_column_seen = 0
        for fallback_column, cell in enumerate(row.findall(f"{_SHEET_NS}c"), start=1):
            column = _column_index(cell.attrib.get("r", "")) or fallback_column
            if column > max_columns:
                truncated = True
                continue
            max_column_seen = max(max_column_seen, column)
            values[column] = _cell_value(cell, shared_strings)
        rows.append([values.get(column, "") for column in range(1, max_column_seen + 1)])
    return rows, truncated


def _column_index(reference: str) -> int | None:
    letters = "".join(character for character in reference if character.isalpha()).upper()
    if not letters:
        return None
    result = 0
    for letter in letters:
        result = result * 26 + ord(letter) - ord("A") + 1
    return result


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        inline = cell.find(f"{_SHEET_NS}is")
        return "".join(inline.itertext()) if inline is not None else ""
    value = cell.findtext(f"{_SHEET_NS}v") or ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError):
            return value
    return value


def _preview_docx(
    archive: ZipFile,
    result: dict[str, Any],
    max_rows: int,
    max_columns: int,
    max_paragraphs: int,
) -> dict[str, Any]:
    document = ElementTree.fromstring(archive.read("word/document.xml"))
    body = document.find(f"{_DOCX_NS}body")
    if body is None:
        result["warnings"].append("DOCX 缺少正文。")
        return result
    for child in body:
        if child.tag == f"{_DOCX_NS}p":
            if len(result["paragraphs"]) >= max_paragraphs:
                result["truncated"] = True
                break
            text = _docx_text(child)
            if text:
                result["paragraphs"].append(text)
        elif child.tag == f"{_DOCX_NS}tbl":
            table, was_truncated = _docx_table(child, max_rows, max_columns)
            result["tables"].append(table)
            result["truncated"] = bool(result["truncated"] or was_truncated)
    return result


def _docx_table(table: ElementTree.Element, max_rows: int, max_columns: int) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    truncated = False
    for row in table.findall(f"{_DOCX_NS}tr"):
        if len(rows) >= max_rows:
            truncated = True
            break
        cells = row.findall(f"{_DOCX_NS}tc")
        if len(cells) > max_columns:
            truncated = True
        rows.append([_docx_text(cell) for cell in cells[:max_columns]])
    return rows, truncated


def _docx_text(element: ElementTree.Element) -> str:
    return "".join(element.itertext()).strip()


def _warning(exc: Exception) -> str:
    return f"无法解析 Office 制品预览：{type(exc).__name__}。"
