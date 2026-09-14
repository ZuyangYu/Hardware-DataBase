"""High-fidelity XLSX cell edits via targeted OOXML surgery.

Only the worksheet part that owns the edited cell is rewritten; every other
zip member (styles, charts, pivot tables, drawings, macros, shared strings)
is copied byte-for-byte.  The child workbook is re-parsed with the project's
own ``parse_xlsx`` before it is accepted, so a candidate can never be built
from a workbook that no longer opens.

v1 operations:

- ``set_cell``: set one cell to a literal value (string), converting it to an
  inline string while keeping its style.
- ``replace_cell_text``: replace one unique cell-text occurrence inside one
  sheet; ambiguous/missing targets and formula cells are rejected.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_CELL_RE = re.compile(r"^([A-Za-z]{1,3})([0-9]{1,7})$")


class XlsxRevisionError(ValueError):
    """Raised when an XLSX edit target is missing, ambiguous or unsafe."""


def _qn(tag: str) -> str:
    return f"{{{_MAIN_NS}}}{tag}"


def _register_namespaces() -> None:
    import xml.etree.ElementTree as ET

    ET.register_namespace("", _MAIN_NS)
    ET.register_namespace("r", _REL_NS)


def _split_cell_ref(cell: str) -> tuple[str, int, int]:
    match = _CELL_RE.match(str(cell or "").strip())
    if match is None:
        raise XlsxRevisionError(f"invalid cell reference: {cell!r}")
    column_letters = match.group(1).upper()
    row_number = int(match.group(2))
    column_number = 0
    for letter in column_letters:
        column_number = column_number * 26 + (ord(letter) - ord("A") + 1)
    return column_letters, column_number, row_number


def _column_index(column_letters: str) -> int:
    value = 0
    for letter in column_letters:
        value = value * 26 + (ord(letter) - ord("A") + 1)
    return value


def _sheet_part_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map sheet display name -> worksheet XML member path."""

    import xml.etree.ElementTree as ET

    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        str(node.get("Id")): str(node.get("Target"))
        for node in rels.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    sheets: dict[str, str] = {}
    for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
        name = str(sheet.get("name") or "")
        rel_id = sheet.get(f"{{{_REL_NS}}}id")
        target = targets.get(str(rel_id) or "")
        if not name or not target:
            continue
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        sheets[name] = target
    return sheets


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root.findall(_qn("si")):
        values.append(
            "".join(node.text or "" for node in item.iter(_qn("t")))
        )
    return values


def _cell_text(cell: Any, shared: list[str]) -> str:
    cell_type = str(cell.get("t") or "")
    if cell_type == "s":
        node = cell.find(_qn("v"))
        try:
            index = int(str(node.text)) if node is not None and node.text else -1
        except (TypeError, ValueError):
            index = -1
        return shared[index] if 0 <= index < len(shared) else ""
    if cell_type == "inlineStr":
        node = cell.find(_qn("is"))
        return (
            "".join(part.text or "" for part in node.iter(_qn("t")))
            if node is not None
            else ""
        )
    node = cell.find(_qn("v"))
    return str(node.text) if node is not None and node.text is not None else ""


def _set_cell_text(cell: Any, value: str) -> None:
    import xml.etree.ElementTree as ET

    for child in list(cell):
        if child.tag in {_qn("v"), _qn("is")}:
            cell.remove(child)
    cell.set("t", "inlineStr")
    inline = ET.SubElement(cell, _qn("is"))
    text = ET.SubElement(inline, _qn("t"))
    text.text = value
    if value != value.strip() or "\n" in value:
        text.set(_XML_SPACE, "preserve")


def _row_number(row: Any) -> int:
    try:
        return int(str(row.get("r") or "0"))
    except (TypeError, ValueError):
        return 0


def _child_cells(row: Any) -> list[Any]:
    return [child for child in row if child.tag == _qn("c")]


def _find_or_create_cell(sheet_root: Any, column_number: int, row_number: int, address: str):
    import xml.etree.ElementTree as ET

    sheet_data = sheet_root.find(_qn("sheetData"))
    if sheet_data is None:
        raise XlsxRevisionError("worksheet has no sheetData")
    row = None
    for candidate in sheet_data.findall(_qn("row")):
        if _row_number(candidate) == row_number:
            row = candidate
            break
    if row is None:
        row = ET.Element(_qn("row"), {"r": str(row_number)})
        inserted = False
        for index, candidate in enumerate(list(sheet_data)):
            if _row_number(candidate) > row_number:
                sheet_data.insert(index, row)
                inserted = True
                break
        if not inserted:
            sheet_data.append(row)
    for cell in _child_cells(row):
        if str(cell.get("r") or "").strip().upper() == address:
            return cell
    cell = ET.Element(_qn("c"), {"r": address})
    inserted = False
    for index, candidate in enumerate(_child_cells(row)):
        match = re.match(r"([A-Za-z]+)", str(candidate.get("r") or ""))
        candidate_column = _column_index(match.group(1).upper()) if match else 0
        if candidate_column > column_number:
            row.insert(index, cell)
            inserted = True
            break
    if not inserted:
        row.append(cell)
    return cell


def _has_formula(cell: Any) -> bool:
    return cell.find(_qn("f")) is not None


def _rewrite_sheet(source: bytes, part_path: str, transform) -> bytes:
    """Copy the archive, rewriting only ``part_path`` through ``transform``."""

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source), "r") as archive:
        infos = archive.infolist()
        with zipfile.ZipFile(output, "w") as child:
            for info in infos:
                payload = archive.read(info.filename)
                if info.filename == part_path:
                    payload = transform(payload)
                child.writestr(info, payload)
    return output.getvalue()


def _load_sheet(source: bytes, sheet: str):
    import xml.etree.ElementTree as ET

    _register_namespaces()
    with zipfile.ZipFile(io.BytesIO(source), "r") as archive:
        sheets = _sheet_part_paths(archive)
        name = str(sheet or "").strip()
        if name not in sheets:
            raise XlsxRevisionError(
                f"worksheet not found: {name!r}; available: {', '.join(sorted(sheets))}"
            )
        part = sheets[name]
        shared = _shared_strings(archive)
        root = ET.fromstring(archive.read(part))
    return part, shared, root


def _validate_child(child_bytes: bytes, *, sheet: str, address: str, expected: str) -> None:
    from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx

    workbook = parse_xlsx(io.BytesIO(child_bytes))
    target = None
    for parsed_sheet in workbook.sheets:
        if parsed_sheet.name == sheet:
            target = parsed_sheet
            break
    if target is None:
        raise XlsxRevisionError("child workbook lost the edited worksheet")
    for cell in target.cells:
        if str(cell.ref or "").strip().upper() == address:
            if str(cell.value or "") != expected:
                raise XlsxRevisionError("child workbook did not apply the edit")
            return
    raise XlsxRevisionError("child workbook lost the edited cell")


def set_cell_value(
    source: bytes, *, sheet: str, cell: str, value: str
) -> tuple[bytes, dict[str, Any]]:
    """Set one cell to a literal string value."""

    address_letters, column_number, row_number = _split_cell_ref(cell)
    address = f"{address_letters}{row_number}"
    part, _shared, root = _load_sheet(source, sheet)

    def transform(payload: bytes) -> bytes:
        import xml.etree.ElementTree as ET

        tree_root = ET.fromstring(payload)
        target = _find_or_create_cell(tree_root, column_number, row_number, address)
        if _has_formula(target):
            raise XlsxRevisionError(
                f"cell {address} contains a formula; refusing to overwrite it"
            )
        _set_cell_text(target, value)
        return ET.tostring(tree_root, encoding="UTF-8", xml_declaration=True)

    child = _rewrite_sheet(source, part, transform)
    _validate_child(child, sheet=sheet, address=address, expected=value)
    audit = {
        "operation": "set_cell",
        "format": "xlsx",
        "sheet": sheet,
        "cell": address,
        "value": value,
    }
    return child, audit


def replace_cell_text(
    source: bytes, *, sheet: str, before: str, after: str
) -> tuple[bytes, dict[str, Any]]:
    """Replace one unique cell-text occurrence inside one worksheet."""

    part, shared, root = _load_sheet(source, sheet)
    matches: list[tuple[str, str]] = []
    for cell in root.iter(_qn("c")):
        address = str(cell.get("r") or "").strip().upper()
        if not address:
            continue
        text = _cell_text(cell, shared)
        if before in text:
            matches.append((address, text))
    if not matches:
        raise XlsxRevisionError(f"replacement target was not found in sheet {sheet!r}")
    if len(matches) > 1:
        raise XlsxRevisionError(
            f"replacement target must occur in exactly one cell (found {len(matches)})"
        )
    address, original = matches[0]

    def transform(payload: bytes) -> bytes:
        import xml.etree.ElementTree as ET

        tree_root = ET.fromstring(payload)
        for cell in tree_root.iter(_qn("c")):
            if str(cell.get("r") or "").strip().upper() != address:
                continue
            if _has_formula(cell):
                raise XlsxRevisionError(
                    f"cell {address} contains a formula; refusing to edit it"
                )
            _set_cell_text(cell, original.replace(before, after, 1))
            return ET.tostring(tree_root, encoding="UTF-8", xml_declaration=True)
        raise XlsxRevisionError(f"cell {address} disappeared during the edit")

    child = _rewrite_sheet(source, part, transform)
    expected = original.replace(before, after, 1)
    _validate_child(child, sheet=sheet, address=address, expected=expected)
    audit = {
        "operation": "replace_cell_text",
        "format": "xlsx",
        "sheet": sheet,
        "cell": address,
        "before": before,
        "after": after,
    }
    return child, audit


_FORMULA_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])(\$?)([A-Za-z]{1,3})(\$?)([0-9]{1,7})(?![A-Za-z0-9_])"
)


def _column_letters(column_number: int) -> str:
    letters = ""
    value = max(1, int(column_number))
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _row_elements(sheet_root: Any):
    sheet_data = sheet_root.find(_qn("sheetData"))
    return [] if sheet_data is None else sheet_data.findall(_qn("row"))


def _insert_row_in_order(sheet_data: Any, row_element: Any, row_number: int) -> None:
    for index, candidate in enumerate(list(sheet_data)):
        if _row_number(candidate) > row_number:
            sheet_data.insert(index, row_element)
            return
    sheet_data.append(row_element)


def _make_row(row_number: int, values: list[str], *, style: Any = None) -> Any:
    import xml.etree.ElementTree as ET

    row = ET.Element(_qn("row"), {"r": str(row_number)})
    for offset, value in enumerate(values or [], start=1):
        text = str(value or "")
        if text == "":
            continue
        cell = ET.SubElement(
            row,
            _qn("c"),
            {"r": f"{_column_letters(offset)}{row_number}"},
        )
        if style is not None:
            cell.set("s", str(style))
        _set_cell_text(cell, text)
    return row


def _shift_formula_text(
    formula: str,
    *,
    threshold: int,
    delta: int,
    deleted: tuple[int, int] | None = None,
) -> str:
    text = str(formula or "")
    if not text:
        return text
    upper = text.upper()
    if "!" in text:
        raise XlsxRevisionError("公式包含跨表引用，无法安全移动行")
    if "INDIRECT(" in upper or "OFFSET(" in upper:
        raise XlsxRevisionError("公式包含 INDIRECT/OFFSET，无法安全移动行")

    def _replace(match: re.Match[str]) -> str:
        column_abs, letters, row_abs, digits = match.groups()
        number = int(digits)
        if deleted is not None and deleted[0] <= number <= deleted[1]:
            raise XlsxRevisionError(f"公式引用了被删除的行 {number}")
        if row_abs != "$" and number > threshold:
            number = number + delta
        return f"{column_abs}{letters}{row_abs}{number}"

    return _FORMULA_REF_RE.sub(_replace, text)


def _shift_all_formulas(
    sheet_root: Any,
    *,
    threshold: int,
    delta: int,
    deleted: tuple[int, int] | None = None,
) -> int:
    changed = 0
    for formula in sheet_root.iter(_qn("f")):
        original = formula.text or ""
        updated = _shift_formula_text(
            original, threshold=threshold, delta=delta, deleted=deleted
        )
        if updated != original:
            formula.text = updated
            changed += 1
    return changed


def _merge_ref_parts(merge: Any) -> tuple[int, int, int, int] | None:
    ref = str(merge.get("ref") or "")
    if ":" not in ref:
        return None
    try:
        _start_letters, start_col, start_row = _split_cell_ref(ref.split(":", 1)[0])
        _end_letters, end_col, end_row = _split_cell_ref(ref.split(":", 1)[1])
    except XlsxRevisionError:
        return None
    return (start_col, start_row, end_col, end_row)


def _set_merge_ref(merge: Any, start_col: int, start_row: int, end_col: int, end_row: int) -> None:
    merge.set(
        "ref",
        f"{_column_letters(start_col)}{start_row}:{_column_letters(end_col)}{end_row}",
    )


def _adjust_merges_insert(sheet_root: Any, after_row: int, delta: int) -> int:
    merge_cells = sheet_root.find(_qn("mergeCells"))
    if merge_cells is None:
        return 0
    changed = 0
    for merge in list(merge_cells):
        parts = _merge_ref_parts(merge)
        if parts is None:
            continue
        start_col, start_row, end_col, end_row = parts
        if start_row > after_row:
            start_row += delta
        if end_row > after_row:
            end_row += delta
        if (start_col, start_row, end_col, end_row) != parts:
            _set_merge_ref(merge, start_col, start_row, end_col, end_row)
            changed += 1
    return changed


def _adjust_merges_delete(sheet_root: Any, first: int, last: int, count: int) -> int:
    merge_cells = sheet_root.find(_qn("mergeCells"))
    if merge_cells is None:
        return 0
    changed = 0
    for merge in list(merge_cells):
        parts = _merge_ref_parts(merge)
        if parts is None:
            continue
        start_col, start_row, end_col, end_row = parts
        remaining = [
            number
            for number in range(start_row, end_row + 1)
            if not (first <= number <= last)
        ]
        if not remaining:
            merge_cells.remove(merge)
            changed += 1
            continue

        def _shift(number: int) -> int:
            return number - count if number > last else number

        new_start = _shift(remaining[0])
        new_end = _shift(remaining[-1])
        if (new_start, new_end) != (start_row, end_row):
            _set_merge_ref(merge, start_col, new_start, end_col, new_end)
            changed += 1
    return changed


def _refresh_dimension(sheet_root: Any) -> None:
    dimension = sheet_root.find(_qn("dimension"))
    if dimension is None:
        return
    max_col = 0
    max_row = 0
    for cell in sheet_root.iter(_qn("c")):
        address = str(cell.get("r") or "")
        if not address:
            continue
        try:
            letters, _col, row = _split_cell_ref(address)
        except XlsxRevisionError:
            continue
        max_col = max(max_col, _column_index(letters))
        max_row = max(max_row, row)
    if max_row == 0 or max_col == 0:
        dimension.set("ref", "A1")
    else:
        dimension.set("ref", f"A1:{_column_letters(max_col)}{max_row}")


def _reindex_row(row: Any, new_row_number: int) -> None:
    row.set("r", str(new_row_number))
    for cell in _child_cells(row):
        address = str(cell.get("r") or "")
        if not address:
            continue
        try:
            letters, _column, _row = _split_cell_ref(address)
        except XlsxRevisionError:
            continue
        cell.set("r", f"{letters}{new_row_number}")


def insert_rows(
    source: bytes,
    *,
    sheet: str,
    after_row: int,
    count: int = 1,
    values: list[list[str]] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Insert empty (or filled) rows after ``after_row``.

    Rows below shift down, merged ranges expand or move, and simple A1
    formulas shift with their data.  Cross-sheet / INDIRECT / OFFSET formulas
    are rejected instead of being rewritten unsafely.
    """

    if int(after_row) < 0:
        raise XlsxRevisionError("after_row must be >= 0")
    if not 1 <= int(count) <= 100:
        raise XlsxRevisionError("row count must be between 1 and 100")
    rows_values = [list(row or []) for row in (values or [])]

    part, _shared, _root = _load_sheet(source, sheet)
    state: dict[str, int] = {}

    def transform(payload: bytes) -> bytes:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(payload)
        sheet_data = root.find(_qn("sheetData"))
        if sheet_data is None:
            raise XlsxRevisionError("worksheet has no sheetData")
        state["updated_formulas"] = _shift_all_formulas(
            root, threshold=int(after_row), delta=int(count)
        )
        state["updated_merges"] = _adjust_merges_insert(
            root, int(after_row), int(count)
        )
        for row in list(_row_elements(root)):
            number = _row_number(row)
            if number > int(after_row):
                _reindex_row(row, number + int(count))
        for offset in range(1, int(count) + 1):
            new_number = int(after_row) + offset
            row_values = rows_values[offset - 1] if offset - 1 < len(rows_values) else []
            _insert_row_in_order(
                sheet_data, _make_row(new_number, row_values), new_number
            )
        _refresh_dimension(root)
        return ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    child = _rewrite_sheet(source, part, transform)
    from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx

    workbook = parse_xlsx(io.BytesIO(child))
    if not any(item.name == sheet for item in workbook.sheets):
        raise XlsxRevisionError("child workbook lost the edited worksheet")
    audit = {
        "operation": "insert_rows",
        "format": "xlsx",
        "sheet": sheet,
        "after_row": int(after_row),
        "count": int(count),
        "filled_rows": len([row for row in rows_values if any(str(v or "") for v in row)]),
        **state,
    }
    return child, audit


def delete_rows(
    source: bytes,
    *,
    sheet: str,
    row: int,
    count: int = 1,
) -> tuple[bytes, dict[str, Any]]:
    """Delete a contiguous row range.

    Formulas that reference a deleted row are rejected (no silent ``#REF!``);
    formulas below the range shift up, merged ranges shrink or disappear.
    """

    first = int(row)
    if first < 1:
        raise XlsxRevisionError("row must be >= 1")
    if not 1 <= int(count) <= 100:
        raise XlsxRevisionError("row count must be between 1 and 100")
    last = first + int(count) - 1

    part, _shared, _root = _load_sheet(source, sheet)
    state: dict[str, int] = {}

    def transform(payload: bytes) -> bytes:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(payload)
        sheet_data = root.find(_qn("sheetData"))
        if sheet_data is None:
            raise XlsxRevisionError("worksheet has no sheetData")
        state["updated_formulas"] = _shift_all_formulas(
            root, threshold=last, delta=-int(count), deleted=(first, last)
        )
        state["updated_merges"] = _adjust_merges_delete(
            root, first, last, int(count)
        )
        removed = 0
        for row_element in list(_row_elements(root)):
            number = _row_number(row_element)
            if first <= number <= last:
                sheet_data.remove(row_element)
                removed += 1
            elif number > last:
                _reindex_row(row_element, number - int(count))
        state["removed_rows"] = removed
        _refresh_dimension(root)
        return ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    child = _rewrite_sheet(source, part, transform)
    from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx

    workbook = parse_xlsx(io.BytesIO(child))
    if not any(item.name == sheet for item in workbook.sheets):
        raise XlsxRevisionError("child workbook lost the edited worksheet")
    audit = {
        "operation": "delete_rows",
        "format": "xlsx",
        "sheet": sheet,
        "row": first,
        "count": int(count),
        **state,
    }
    return child, audit


__all__ = [
    "XlsxRevisionError",
    "delete_rows",
    "insert_rows",
    "replace_cell_text",
    "set_cell_value",
]
