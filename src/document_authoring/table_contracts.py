"""Build table write authority from inspected template cells, never model coordinates."""
from __future__ import annotations

import hashlib
from collections import defaultdict

from src.document_authoring.template_analysis import workbook_cell_coordinates, workbook_value_hash


def repair_repeating_table_targets(analysis, suggestion):
    """Trim a broad table proposal to an analyzer-inspected body rectangle.

    Models sometimes return the visual table (headers, labels, and decorative
    blanks) instead of the writable data region.  A repair is made only when
    the inventory contains a contiguous, three-column-or-wider header run and
    a bounded body immediately below it.  Otherwise the original proposal is
    preserved so activation remains fail-closed.
    """
    if analysis.format == "docx" or suggestion.value_shape != "repeating_table":
        return suggestion
    target_ids = set(suggestion.target_unit_ids)
    positions = {}
    for unit in analysis.units:
        sheet = unit.locator.get("sheet_name")
        cell = unit.locator.get("cell")
        if not sheet or not cell:
            continue
        try:
            positions[(str(sheet), *workbook_cell_coordinates(str(cell)))] = unit
        except ValueError:
            continue

    headers_by_sheet_row = defaultdict(list)
    for unit in analysis.units:
        if unit.structural_role_hint != "table_header":
            continue
        sheet = unit.locator.get("sheet_name")
        cell = unit.locator.get("cell")
        if not sheet or not cell:
            continue
        try:
            column, row = workbook_cell_coordinates(str(cell))
        except ValueError:
            continue
        headers_by_sheet_row[(str(sheet), row)].append((column, unit))

    candidates = []
    for (sheet, header_row), entries in headers_by_sheet_row.items():
        columns = sorted(column for column, _unit in entries)
        runs = []
        current = []
        for column in columns:
            if current and column != current[-1] + 1:
                runs.append(current)
                current = []
            current.append(column)
        if current:
            runs.append(current)
        for columns_run in runs:
            if len(columns_run) < 3:
                continue
            body_rows = []
            row = header_row + 1
            while True:
                row_units = [positions.get((sheet, column, row)) for column in columns_run]
                if any(unit is None for unit in row_units):
                    break
                populated = sum(unit.value_kind != "blank" for unit in row_units)
                candidate = any(
                    unit.candidate_for_auto_fill or unit.structural_role_hint in {"sample_value", "placeholder"}
                    for unit in row_units
                )
                if not (populated >= 2 or candidate):
                    break
                if any(unit.unit_id in target_ids for unit in row_units):
                    body_rows.append(row)
                row += 1
            if not body_rows:
                continue
            contiguous = [body_rows[0]]
            for body_row in body_rows[1:]:
                if body_row != contiguous[-1] + 1:
                    break
                contiguous.append(body_row)
            candidates.append((sheet, header_row, columns_run, contiguous))

    if not candidates:
        return suggestion
    # False-positive header runs inside a real table's body are common. They
    # are discarded, while genuinely separate competing tables remain an
    # ambiguity and must stay unchanged.
    candidates = [
        candidate for candidate in candidates
        if not any(
            other[0] == candidate[0]
            and other[1] < candidate[1] <= other[3][-1]
            for other in candidates if other is not candidate
        )
    ]
    if len(candidates) != 1:
        return suggestion
    sheet, _header_row, columns, rows = candidates[0]
    repaired_ids = [
        positions[(sheet, column, row)].unit_id
        for row in rows
        for column in columns
    ]
    if not repaired_ids or not set(repaired_ids) <= target_ids or repaired_ids == suggestion.target_unit_ids:
        return suggestion
    return suggestion.model_copy(update={"target_unit_ids": repaired_ids})


def table_schema_from_targets(analysis, suggestion):
    # Local import keeps template inspection independent of runtime models.
    from src.document_authoring.models import WorkbookTableColumnSchema, WorkbookTableSchema

    units = {unit.unit_id: unit for unit in analysis.units}
    targets = [units[key] for key in suggestion.target_unit_ids]
    if not targets or len({u.locator.get("sheet_name") for u in targets}) != 1:
        raise ValueError("table mapping requires a single-sheet rectangle")
    sheet = targets[0].locator["sheet_name"]
    coords = {workbook_cell_coordinates(str(unit.locator.get("cell", ""))) for unit in targets}
    columns = sorted({col for col, row in coords})
    rows = sorted({row for col, row in coords})
    if rows != list(range(rows[0], rows[-1] + 1)) or len(coords) != len(columns) * len(rows):
        raise ValueError("table mapping requires a complete writable rectangle")
    if len(rows) > 10000 or rows[0] <= 1:
        raise ValueError("table mapping requires bounded rows and a preceding header")
    by_cell = {unit.locator.get("cell"): unit for unit in analysis.units if unit.locator.get("sheet_name") == sheet}
    table_columns = []
    for col in columns:
        first = next(unit for unit in targets if workbook_cell_coordinates(unit.locator["cell"]) == (col, rows[0]))
        letter = first.locator["cell"].rstrip("0123456789")
        header = by_cell.get(f"{letter}{rows[0] - 1}")
        if header is None or not header.value_preview or header.structural_role_hint != "table_header":
            raise ValueError("table mapping requires inspected table headers")
        table_columns.append(WorkbookTableColumnSchema(column_id=letter, column_letter=letter, label=header.value_preview))
    for unit in targets:
        if not unit.writable or unit.value_kind == "formula" or unit.unit_id in analysis.locked_unit_ids:
            raise ValueError("table mapping contains a protected cell")
        if unit.value_kind != "blank" and unit.structural_role_hint != "placeholder" and unit.unit_id not in analysis.approved_overwrite_unit_ids:
            raise ValueError("table mapping requires explicit example overwrite approval")
    key = f"{analysis.template_version_id}:{suggestion.semantic_unit_id}"
    return WorkbookTableSchema(
        table_region_id=f"table-{hashlib.sha256(key.encode()).hexdigest()[:16]}",
        semantic_unit_id=suggestion.semantic_unit_id, sheet_name=sheet,
        header_row=rows[0] - 1, first_data_row=rows[0], last_template_row=rows[-1],
        style_source_row=rows[0], max_output_rows=max(len(rows), 1_000), columns=table_columns,
        expected_value_hashes={unit.locator["cell"]: unit.value_hash or workbook_value_hash(unit.value_preview) for unit in targets},
        required_columns=[column.column_id for column in table_columns],
        allow_example_region_replacement=True,
    )
