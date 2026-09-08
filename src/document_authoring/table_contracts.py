"""Build table write authority from inspected template cells, never model coordinates."""
from __future__ import annotations

import hashlib

from src.document_authoring.template_analysis import workbook_cell_coordinates, workbook_value_hash


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
        style_source_row=rows[0], max_output_rows=len(rows), columns=table_columns,
        expected_value_hashes={unit.locator["cell"]: unit.value_hash or workbook_value_hash(unit.value_preview) for unit in targets},
        allow_example_region_replacement=True,
    )
