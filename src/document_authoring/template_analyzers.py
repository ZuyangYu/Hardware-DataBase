"""Deterministic, package-only structural inventory for OOXML templates."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from typing import Literal
from xml.etree import ElementTree as ET

from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisUnit,
    TemplateNeighbor,
    workbook_cell_coordinates,
    workbook_value_hash,
)


SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"x": SPREADSHEET_NS, "r": OFFICE_REL_NS, "pr": PACKAGE_REL_NS, "w": WORD_NS}
_PLACEHOLDER_RE = re.compile(
    r"^(?:\{\{[A-Za-z_][A-Za-z0-9_.-]*\}\}|\$\{[A-Za-z_][A-Za-z0-9_.-]*\}|<<[A-Za-z_][A-Za-z0-9_.-]*>>)$"
)
_MAX_VALUE_PREVIEW = 256
_NEIGHBOR_RADIUS = 2
_FILLABLE_TABLE_HEADER_RE = re.compile(
    r"(?:\\bfunction\\b|\\bdescription\\b|功能描述|功能说明|功能)",
    re.IGNORECASE,
)


def analyze_template(
    content: bytes, format: Literal["xlsx", "xlsm", "docx"],
) -> TemplateAnalysis:
    """Return a deterministic structural inventory without opening an Office model.

    The analysis identifier is content-addressed because an upload has not yet
    acquired a persisted ``TemplateVersion`` identifier at this boundary.
    """
    digest = hashlib.sha256(content).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as package:
            if format in {"xlsx", "xlsm"}:
                units, requires_human = _analyze_workbook(package)
            else:
                units, requires_human = _analyze_docx(package)
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
        return _analysis(digest, format, [], "failed")
    return _analysis(
        digest,
        format,
        units,
        "requires_human" if requires_human else "ready_for_confirmation",
    )


def _analysis(
    digest: str,
    format: Literal["xlsx", "xlsm", "docx"],
    units: list[TemplateAnalysisUnit],
    status: Literal["ready_for_confirmation", "requires_human", "failed"],
) -> TemplateAnalysis:
    return TemplateAnalysis(
        analysis_id=f"analysis-{digest[:16]}",
        template_version_id=f"unregistered-{digest[:16]}",
        content_hash=digest,
        format=format,
        status=status,
        units=units,
    )


def _analyze_workbook(package: zipfile.ZipFile) -> tuple[list[TemplateAnalysisUnit], bool]:
    names = set(package.namelist())
    active_content = _workbook_has_active_content(names, package)
    workbook = ET.fromstring(package.read("xl/workbook.xml"))
    workbook_protected = workbook.find("x:workbookProtection", NS) is not None
    rel_targets = _relationship_targets(package.read("xl/_rels/workbook.xml.rels"))
    styles_xml = package.read("xl/styles.xml") if "xl/styles.xml" in names else None
    styles = _style_protection(styles_xml)
    style_fingerprints = _style_fingerprints(styles_xml)
    shared_strings = _shared_strings(package.read("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in names else []
    units: list[TemplateAnalysisUnit] = []
    for sheet in workbook.findall("x:sheets/x:sheet", NS):
        sheet_name = sheet.attrib.get("name", "Sheet")
        sheet_hidden = sheet.attrib.get("state", "visible").lower() in {"hidden", "veryhidden"}
        relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id", "")
        target = rel_targets.get(relationship_id)
        if not target:
            continue
        sheet_path = _part_path("xl", target)
        if sheet_path not in names:
            continue
        root = ET.fromstring(package.read(sheet_path))
        sheet_protected = workbook_protected or root.find("x:sheetProtection", NS) is not None
        merged_non_anchors = _merged_non_anchors(root)
        hidden_column_ranges, invalid_hidden_column_range = _hidden_column_ranges(root)
        sheet_units: list[tuple[TemplateAnalysisUnit, int, int]] = []
        nonempty_values: dict[tuple[int, int], str] = {}
        for row in root.findall(".//x:sheetData/x:row", NS):
            row_hidden = row.attrib.get("hidden") in {"1", "true", "True"}
            for cell in row.findall("x:c", NS):
                ref = cell.attrib.get("r", "")
                if not ref:
                    continue
                column, row_number = _cell_coordinates(ref)
                if column is None or row_number is None:
                    raise ValueError(f"invalid workbook cell reference: {ref}")
                has_formula = cell.find("x:f", NS) is not None
                style = _style_index(cell.attrib.get("s"))
                invalid_style = style is None or style not in styles
                # A protected sheet must fail closed when its style table is
                # malformed or references an unknown cell style.
                style_locked, style_hidden = styles.get(style, (True, False)) if style is not None else (True, False)
                protected = sheet_protected and (invalid_style or style_locked)
                hidden_column = invalid_hidden_column_range or any(
                    lower <= column <= upper for lower, upper in hidden_column_ranges
                )
                hidden = row_hidden or style_hidden
                value_kind, value_preview, value_hash = _workbook_cell_value(cell, shared_strings)
                blocked_reason = _workbook_blocked_reason(
                    has_formula=has_formula,
                    merged_non_anchor=ref in merged_non_anchors,
                    invalid_style=invalid_style,
                    protected=protected,
                    hidden_sheet=sheet_hidden,
                    hidden_column=hidden_column,
                    hidden=hidden,
                    active_content=active_content,
                )
                unit = TemplateAnalysisUnit(
                    unit_id=f"sheet:{sheet_name}!{ref}",
                    locator={"sheet_name": sheet_name, "cell": ref},
                    label=f"{sheet_name}!{ref}",
                    writable=blocked_reason is None,
                    blocked_reason=blocked_reason,
                    value_preview=value_preview,
                    value_hash=value_hash,
                    value_kind=value_kind,
                    style_fingerprint=style_fingerprints.get(
                        style,
                        hashlib.sha256(f"style:{style}".encode("utf-8")).hexdigest()[:16],
                    ),
                    structural_role_hint=_structural_role(value_kind, value_preview),
                )
                sheet_units.append((unit, column, row_number))
                if value_preview:
                    nonempty_values[(column, row_number)] = value_preview
        enriched_cells: list[tuple[TemplateAnalysisUnit, int, int]] = []
        for unit, column, row_number in sheet_units:
            neighbors = [
                TemplateNeighbor(
                    relative_row=neighbor_row - row_number,
                    relative_column=neighbor_column - column,
                    value_preview=preview,
                )
                for neighbor_row in range(max(1, row_number - _NEIGHBOR_RADIUS), row_number + _NEIGHBOR_RADIUS + 1)
                for neighbor_column in range(max(1, column - _NEIGHBOR_RADIUS), column + _NEIGHBOR_RADIUS + 1)
                if (neighbor_column, neighbor_row) != (column, row_number)
                if (preview := nonempty_values.get((neighbor_column, neighbor_row))) is not None
            ]
            enriched_cells.append((
                unit.model_copy(update={"neighborhood": neighbors}),
                column,
                row_number,
            ))
        units.extend(_classify_workbook_regions(enriched_cells))
    return units, active_content


def _shared_strings(content: bytes) -> list[str]:
    root = ET.fromstring(content)
    return [
        "".join(text.text or "" for text in item.findall(".//x:t", NS)).strip()
        for item in root.findall("x:si", NS)
    ]


def _workbook_cell_value(
    cell: ET.Element,
    shared_strings: list[str],
) -> tuple[
    Literal["blank", "text", "number", "boolean", "formula", "error"],
    str | None,
    str,
]:
    if cell.find("x:f", NS) is not None:
        return "formula", None, workbook_value_hash(None)
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        full_value = "".join(text.text or "" for text in cell.findall(".//x:t", NS)).strip() or None
        return "text", _bounded_preview(full_value or ""), workbook_value_hash(full_value)
    value = cell.findtext("x:v", default="", namespaces=NS)
    if cell_type == "s":
        try:
            full_value = shared_strings[int(value)] or None
            return "text", _bounded_preview(full_value or ""), workbook_value_hash(full_value)
        except (ValueError, IndexError):
            return "error", None, workbook_value_hash(None)
    if cell_type == "b":
        full_value = "TRUE" if value == "1" else "FALSE"
        return "boolean", full_value, workbook_value_hash(full_value)
    if cell_type == "e":
        full_value = value.strip() or None
        return "error", _bounded_preview(value), workbook_value_hash(full_value)
    if cell_type in {"str", "d"}:
        full_value = value.strip() or None
        return "text", _bounded_preview(value), workbook_value_hash(full_value)
    if value == "":
        return "blank", None, workbook_value_hash(None)
    full_value = value.strip()
    return "number", _bounded_preview(value), workbook_value_hash(full_value)


def _bounded_preview(value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:_MAX_VALUE_PREVIEW]


def _structural_role(
    value_kind: Literal["blank", "text", "number", "boolean", "formula", "error"],
    value_preview: str | None,
) -> Literal["unknown", "fixed_label", "placeholder", "value", "layout_blank"]:
    if value_kind == "formula":
        return "unknown"
    if value_kind == "blank":
        return "layout_blank"
    if value_kind == "text" and value_preview and _PLACEHOLDER_RE.fullmatch(value_preview):
        return "placeholder"
    if value_kind == "text":
        return "fixed_label"
    return "value"


def _classify_workbook_regions(
    cells: list[tuple[TemplateAnalysisUnit, int, int]],
) -> list[TemplateAnalysisUnit]:
    """Conservatively identify scalar values beside a fixed row label.

    This classifier intentionally recognizes only a narrow layout pattern.  A
    cell needs an immediately-left fixed label and may not be part of a wider
    text header row before it can become a candidate.  All other blanks keep
    their existing ``layout_blank`` role and remain non-candidates.
    """
    by_row: dict[int, list[tuple[TemplateAnalysisUnit, int]]] = {}
    for unit, column, row in cells:
        by_row.setdefault(row, []).append((unit, column))

    classified: dict[str, TemplateAnalysisUnit] = {
        unit.unit_id: unit.model_copy(update={
            "candidate_for_auto_fill": unit.writable
            and unit.structural_role_hint == "placeholder",
        })
        for unit, _column, _row in cells
    }
    table_header_runs: list[tuple[int, list[tuple[str, int]]]] = []
    for row_number, row_cells in by_row.items():
        ordered = sorted(row_cells, key=lambda item: item[1])
        text_runs = _contiguous_text_runs(ordered)
        header_ids = {
            unit.unit_id
            for run in text_runs
            if len(run) >= 3
            for unit, _column in run
        }
        for unit_id in header_ids:
            unit = classified[unit_id]
            classified[unit_id] = unit.model_copy(update={
                "structural_role_hint": "table_header",
                "candidate_for_auto_fill": False,
            })
        table_header_runs.extend(
            (
                row_number,
                [(unit.unit_id, column) for unit, column in run],
            )
            for run in text_runs
            if len(run) >= 3
        )

        for index in range(len(ordered) - 1):
            left_original, left_column = ordered[index]
            right_original, right_column = ordered[index + 1]
            if right_column != left_column + 1:
                continue
            left = classified[left_original.unit_id]
            right = classified[right_original.unit_id]
            if (
                not right.writable
                or left.structural_role_hint != "fixed_label"
                or left.value_kind != "text"
            ):
                continue
            if right.structural_role_hint == "layout_blank":
                classified[right.unit_id] = right.model_copy(update={
                    "structural_role_hint": "scalar_input",
                    "candidate_for_auto_fill": True,
                })
            elif right.structural_role_hint == "fixed_label":
                classified[right.unit_id] = right.model_copy(update={
                    "structural_role_hint": "sample_value",
                    "candidate_for_auto_fill": True,
                })

    # A table header provides semantic context that a simple left-to-right
    # label/value heuristic cannot.  Only blank cells below an explicitly
    # descriptive header are writable; populated body cells are examples or
    # source values and must never be selected for automatic replacement.
    for header_row, header_run in table_header_runs:
        fillable_columns = {
            column
            for unit_id, column in header_run
            if _is_fillable_table_header(classified[unit_id].value_preview)
        }
        if not fillable_columns:
            continue
        header_columns = {column for _unit_id, column in header_run}
        for row_number, row_cells in by_row.items():
            if row_number <= header_row:
                continue
            body_cells = {
                column: classified[unit.unit_id]
                for unit, column in row_cells
                if column in header_columns
            }
            if sum(unit.value_kind != "blank" for unit in body_cells.values()) < 2:
                continue
            if sum(
                unit.structural_role_hint == "table_header"
                for unit in body_cells.values()
            ) >= 3:
                continue
            for column, unit in body_cells.items():
                if unit.structural_role_hint == "placeholder":
                    continue
                if unit.value_kind != "blank":
                    classified[unit.unit_id] = unit.model_copy(update={
                        "structural_role_hint": "sample_value",
                        "candidate_for_auto_fill": False,
                    })
                elif (
                    column in fillable_columns
                    and unit.writable
                    and unit.structural_role_hint == "layout_blank"
                ):
                    classified[unit.unit_id] = unit.model_copy(update={
                        "structural_role_hint": "scalar_input",
                        "candidate_for_auto_fill": True,
                    })
    return [classified[unit.unit_id] for unit, _column, _row in cells]


def _contiguous_text_runs(
    ordered: list[tuple[TemplateAnalysisUnit, int]],
) -> list[list[tuple[TemplateAnalysisUnit, int]]]:
    runs: list[list[tuple[TemplateAnalysisUnit, int]]] = []
    current: list[tuple[TemplateAnalysisUnit, int]] = []
    previous_column: int | None = None
    for unit, column in ordered:
        if (
            unit.writable
            and unit.value_kind == "text"
            and unit.structural_role_hint == "fixed_label"
            and (previous_column is None or column == previous_column + 1)
        ):
            current.append((unit, column))
        else:
            if current:
                runs.append(current)
            current = []
        previous_column = column
    if current:
        runs.append(current)
    return runs


def _is_fillable_table_header(value_preview: str | None) -> bool:
    return bool(value_preview and _FILLABLE_TABLE_HEADER_RE.search(value_preview))


def _workbook_has_active_content(names: set[str], package: zipfile.ZipFile) -> bool:
    sensitive_markers = ("/embeddings/", "/activex/", "/ctrlprops/", "/vba")
    if any(marker in name.lower() for name in names for marker in sensitive_markers):
        return True
    if any(name.startswith("xl/externalLinks/") for name in names):
        return True
    return any(_has_external_relationship(package.read(name)) for name in names if name.endswith(".rels"))


def _workbook_blocked_reason(
    *,
    has_formula: bool,
    merged_non_anchor: bool,
    invalid_style: bool,
    protected: bool,
    hidden_sheet: bool,
    hidden_column: bool,
    hidden: bool,
    active_content: bool,
) -> str | None:
    if has_formula:
        return "formula"
    if merged_non_anchor:
        return "merged_non_anchor"
    if hidden_sheet:
        return "hidden_sheet"
    if hidden_column:
        return "hidden_column"
    if invalid_style:
        return "invalid_style"
    if protected:
        return "protected"
    if hidden:
        return "hidden"
    if active_content:
        return "active_content"
    return None


def _style_protection(styles_xml: bytes | None) -> dict[int, tuple[bool, bool]]:
    if not styles_xml:
        return {0: (True, False)}
    root = ET.fromstring(styles_xml)
    result: dict[int, tuple[bool, bool]] = {}
    for index, xf in enumerate(root.findall("x:cellXfs/x:xf", NS)):
        protection = xf.find("x:protection", NS)
        locked = protection is None or protection.attrib.get("locked", "1") != "0"
        hidden = protection is not None and protection.attrib.get("hidden", "0") == "1"
        result[index] = (locked, hidden)
    return result or {0: (True, False)}


def _style_fingerprints(styles_xml: bytes | None) -> dict[int, str]:
    if not styles_xml:
        return {0: hashlib.sha256(b"default-cell-style").hexdigest()[:16]}
    root = ET.fromstring(styles_xml)
    result = {
        index: hashlib.sha256(ET.tostring(xf, encoding="utf-8")).hexdigest()[:16]
        for index, xf in enumerate(root.findall("x:cellXfs/x:xf", NS))
    }
    return result or {0: hashlib.sha256(b"default-cell-style").hexdigest()[:16]}


def _style_index(value: str | None) -> int | None:
    if value is None:
        return 0
    if not value.isascii() or not value.isdecimal():
        return None
    return int(value)


def _hidden_column_ranges(root: ET.Element) -> tuple[list[tuple[int, int]], bool]:
    ranges: list[tuple[int, int]] = []
    invalid_hidden_range = False
    for column in root.findall("x:cols/x:col", NS):
        if column.attrib.get("hidden") not in {"1", "true", "True"}:
            continue
        try:
            lower = int(column.attrib["min"])
            upper = int(column.attrib["max"])
        except (KeyError, ValueError):
            invalid_hidden_range = True
            continue
        if lower < 1 or upper < lower:
            invalid_hidden_range = True
            continue
        ranges.append((lower, upper))
    return ranges, invalid_hidden_range


def _merged_non_anchors(root: ET.Element) -> set[str]:
    result: set[str] = set()
    for merged in root.findall(".//x:mergeCell", NS):
        reference = merged.attrib.get("ref", "")
        if ":" not in reference:
            continue
        start, end = reference.split(":", 1)
        start_col, start_row = _cell_coordinates(start)
        end_col, end_row = _cell_coordinates(end)
        if start_col is None or start_row is None or end_col is None or end_row is None:
            continue
        for row in range(min(start_row, end_row), max(start_row, end_row) + 1):
            for col in range(min(start_col, end_col), max(start_col, end_col) + 1):
                ref = f"{_column_name(col)}{row}"
                if ref != start:
                    result.add(ref)
    return result


def _cell_coordinates(reference: str) -> tuple[int | None, int | None]:
    try:
        return workbook_cell_coordinates(reference)
    except ValueError:
        return None, None


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _analyze_docx(package: zipfile.ZipFile) -> tuple[list[TemplateAnalysisUnit], bool]:
    names = set(package.namelist())
    root = ET.fromstring(package.read("word/document.xml"))
    hazards = _docx_hazards(names, package)
    blocked_reason = next(iter(hazards), None)
    units: list[TemplateAnalysisUnit] = []
    body = root.find("w:body", NS)
    if body is None:
        return units, bool(hazards)
    paragraph_index = 0
    table_index = 0
    for child in body:
        if child.tag == f"{{{WORD_NS}}}p":
            units.append(_docx_unit(
                unit_id=f"paragraph:{paragraph_index}",
                locator={"paragraph_index": paragraph_index},
                label=f"Paragraph {paragraph_index + 1}",
                blocked_reason=blocked_reason,
            ))
            paragraph_index += 1
        elif child.tag == f"{{{WORD_NS}}}tbl":
            for row_index, row in enumerate(child.findall("w:tr", NS)):
                for cell_index, _cell in enumerate(row.findall("w:tc", NS)):
                    units.append(_docx_unit(
                        unit_id=f"table:{table_index}:{row_index}:{cell_index}",
                        locator={"table_index": table_index, "row_index": row_index, "cell_index": cell_index},
                        label=f"Table {table_index + 1} cell {row_index + 1},{cell_index + 1}",
                        blocked_reason=blocked_reason,
                    ))
            table_index += 1
    return units, bool(hazards)


def _docx_unit(*, unit_id: str, locator: dict[str, int], label: str, blocked_reason: str | None) -> TemplateAnalysisUnit:
    return TemplateAnalysisUnit(
        unit_id=unit_id,
        locator=locator,
        label=label,
        writable=blocked_reason is None,
        blocked_reason=blocked_reason,
    )


def _docx_hazards(names: set[str], package: zipfile.ZipFile) -> list[str]:
    hazards: list[str] = []
    if any(_has_external_relationship(package.read(name)) for name in names if name.endswith(".rels")):
        hazards.append("external_relationship")
    lowered = {name.lower() for name in names}
    if any("/embeddings/" in name or "/activex/" in name or "/vba" in name for name in lowered):
        hazards.append("active_content")
    if any(name.startswith("_xmlsignatures/") or name.endswith("origin.sigs") for name in lowered):
        hazards.append("signature")
    if "word/settings.xml" in names:
        settings = ET.fromstring(package.read("word/settings.xml"))
        if settings.find("w:documentProtection", NS) is not None:
            hazards.append("document_protection")
    return hazards


def _relationship_targets(rels_xml: bytes) -> dict[str, str]:
    root = ET.fromstring(rels_xml)
    return {
        relation.attrib.get("Id", ""): relation.attrib.get("Target", "")
        for relation in root.findall("pr:Relationship", NS)
    }


def _has_external_relationship(rels_xml: bytes) -> bool:
    root = ET.fromstring(rels_xml)
    return any(
        relation.attrib.get("TargetMode", "").lower() == "external"
        for relation in root.findall("pr:Relationship", NS)
    )


def _part_path(base: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(base, target.lstrip("/")))
