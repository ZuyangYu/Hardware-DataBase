"""Evidence-led, template-driven ICD workbook generation utilities.

The module deliberately takes project files and connector reference designators as
inputs.  It does not contain project names, connector names, or fixed workbook
coordinates; the table and scalar targets are discovered from the uploaded
template's labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable
import zipfile

from src.circuit.models import ComponentInstance
from src.circuit.parsers.edf_parser import EdfParser
from src.document_authoring.models import (
    RendererPolicy,
    WorkbookFill,
    WorkbookFillPlan,
    WorkbookRegionSchema,
    WorkbookTableColumnSchema,
    WorkbookTableFill,
    WorkbookTableSchema,
)
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.template_analysis import workbook_value_hash
from src.pipelines.spreadsheet.xlsx_parser import ParsedSheet, parse_xlsx


_PIN_REFERENCE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]+\d+)\s*-\s*(\d+)(?![A-Za-z0-9_])")
_MODEL_PATTERN = re.compile(r"{label}\s*[：:]\s*([^；;（(]+)")


@dataclass(frozen=True)
class IcdGenerationResult:
    content: bytes
    source_summary: dict[str, Any]
    integrity_manifest: dict[str, Any]


@dataclass(frozen=True)
class IcdFrontViewFillResult:
    """Allowlisted writes and blocking diagnostics for ICD connector front views."""

    regions: list[WorkbookRegionSchema]
    fills: list[WorkbookFill]
    issues: list[dict[str, str]]
    detected_layout_count: int = 0


@dataclass(frozen=True)
class IcdFrontViewRenderResult:
    """Pure artifact post-processing result for the normal Harness handoff."""

    content: bytes
    issues: list[dict[str, str]]
    detected_layout_count: int
    integrity_manifest: dict[str, Any] | None = None


_FRONT_VIEW_PIN = re.compile(
    r"^(?P<refdes>[A-Za-z][A-Za-z0-9_]*)\s*[-_/]\s*(?P<pin>[A-Za-z0-9_.]+)$"
)
_PIN_LABELS = ("pin number", "管脚号", "引脚号", "针脚号")
_DEFINITION_LABELS = ("pin definition", "管脚定义", "引脚定义")
_BOARD_PIN_LABELS = ("板端接插件序号", "board connector pin", "connector pin number")


def connector_refdes_from_front_view_template(content: bytes) -> list[str]:
    """Read explicit connector identifiers from governed ICD front-view slots.

    This accepts the immutable uploaded template bytes rather than a filesystem
    path.  It deliberately reuses the layout and slot parser that later fills
    the diagram, so a value such as ``X302-20`` is interpreted once and has the
    same meaning during scope discovery and rendering.  A workbook without a
    complete recognized layout contributes no candidates.
    """

    if not isinstance(content, bytes) or not content:
        return []
    try:
        workbook = parse_xlsx(BytesIO(content))
    except (OSError, ValueError, zipfile.BadZipFile):
        return []

    candidates: list[str] = []
    for sheet in workbook.sheets:
        if _is_example_sheet(sheet.name):
            continue
        for layout in _front_view_layouts(sheet):
            top_pin_row = layout["top_pin_row"]
            if top_pin_row is None:
                continue
            slots = _front_view_slots(
                sheet,
                int(top_pin_row),
                int(layout["board_pin_row"] or 0),
                int(layout["top_definition_row"] or 0),
                lower_pin_row=(
                    int(layout["lower_pin_row"])
                    if layout["lower_pin_row"] is not None
                    else None
                ),
                lower_definition_row=(
                    int(layout["lower_definition_row"])
                    if layout["lower_definition_row"] is not None
                    else None
                ),
            )
            candidates.extend(
                str(slot["refdes"]).strip().upper()
                for slot in slots
                if slot.get("refdes")
            )
    return list(dict.fromkeys(candidates))


def build_front_view_fills(
    workbook: Any,
    frozen_pin_mappings: Iterable[dict[str, Any]],
) -> IcdFrontViewFillResult:
    """Fill discovered ICD front-view slots without changing their geometry.

    The template, rather than a generated row order, owns the physical order of
    the slots.  A slot is usable only when its connector and pin number are
    explicit (``X302-20``) or can be safely completed from the one connector
    already identified in the same layout plus its board-pin-number cell.
    """

    mappings = {
        _pin_key(str(item.get("refdes") or ""), str(item.get("pin_name") or "")): {
            "refdes": str(item.get("refdes") or "").strip(),
            "pin_name": _normalize_pin(str(item.get("pin_name") or "")),
            "net_name": str(item.get("net_name") or "NC").strip() or "NC",
        }
        for item in frozen_pin_mappings
        if str(item.get("refdes") or "").strip() and str(item.get("pin_name") or "").strip()
    }
    regions: list[WorkbookRegionSchema] = []
    fills: list[WorkbookFill] = []
    issues: list[dict[str, str]] = []
    layout_number = 0
    for sheet in getattr(workbook, "sheets", []):
        if _is_example_sheet(sheet.name):
            continue
        for layout in _front_view_layouts(sheet):
            layout_number += 1
            layout_id = f"icd-front-view-{layout_number}"
            layout_regions, layout_fills, layout_issues = _front_view_layout_fills(
                sheet,
                layout_id,
                layout,
                mappings,
            )
            # A half-rendered physical diagram is misleading.  Keep every
            # layout all-or-nothing while allowing another connector layout in
            # the same template to render when it is independently complete.
            if layout_issues:
                issues.extend(layout_issues)
                continue
            regions.extend(layout_regions)
            fills.extend(layout_fills)
    return IcdFrontViewFillResult(
        regions=regions,
        fills=fills,
        issues=issues,
        detected_layout_count=layout_number,
    )


def render_icd_front_views(
    artifact_content: bytes,
    frozen_pin_mappings: Iterable[dict[str, Any]],
    *,
    target_format: str = "xlsx",
    template_version_id: str = "icd-front-view",
) -> IcdFrontViewRenderResult:
    """Apply frozen ICD facts to an already-rendered candidate workbook.

    This deliberately accepts bytes, so the login/Harness pipeline can call it
    after generic rendering without relying on the source-generator script or
    a filesystem template path.  A layout with unresolved slots returns its
    original bytes and blocking issues; it is never partially overwritten.
    """

    suffix = ".xlsm" if target_format.casefold() == "xlsm" else ".xlsx"
    descriptor, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(artifact_content)
        workbook = parse_xlsx(path)
    finally:
        # parse_xlsx has consumed the file before the renderer needs the
        # original bytes; remove it even when parsing a malformed artifact.
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
    front_view = build_front_view_fills(workbook, frozen_pin_mappings)
    if not front_view.detected_layout_count or front_view.issues:
        return IcdFrontViewRenderResult(
            content=artifact_content,
            issues=front_view.issues,
            detected_layout_count=front_view.detected_layout_count,
        )
    rendered = XlsmRenderer().render(
        artifact_content,
        front_view.regions,
        WorkbookFillPlan(
            template_version_id=template_version_id,
            fills=front_view.fills,
        ),
        RendererPolicy(renderer_policy_id="icd-front-view"),
        security_approved=True,
    )
    return IcdFrontViewRenderResult(
        content=rendered.content,
        issues=[],
        detected_layout_count=front_view.detected_layout_count,
        integrity_manifest=rendered.integrity_manifest,
    )


def _front_view_layouts(sheet: ParsedSheet) -> list[dict[str, int | None]]:
    """Find the repeated five-row (or compact three-row) front-view pattern."""

    labels = {
        row_index: _front_view_row_label(row)
        for row_index, row in enumerate(sheet.rows)
    }
    layouts: list[dict[str, int | None]] = []
    row_index = 0
    while row_index < len(sheet.rows):
        if labels[row_index] != "definition":
            row_index += 1
            continue
        start_row = row_index
        pin_row = start_row + 1
        board_row = start_row + 2
        if labels.get(pin_row) != "pin" or labels.get(board_row) != "board_pin":
            row_index += 1
            continue
        lower_pin_row: int | None = None
        lower_definition_row: int | None = None
        if (
            labels.get(board_row + 1) == "pin"
            and labels.get(board_row + 2) == "definition"
        ):
            lower_pin_row = board_row + 1
            lower_definition_row = board_row + 2
            row_index = lower_definition_row + 1
        else:
            row_index = board_row + 1
        layouts.append({
            "top_definition_row": start_row,
            "top_pin_row": pin_row,
            "board_pin_row": board_row,
            "lower_pin_row": lower_pin_row,
            "lower_definition_row": lower_definition_row,
        })
    # A title and a partial row pair are still a governed ICD front view: it
    # must surface an actionable blocking diagnostic instead of being ignored.
    for row_index, label in labels.items():
        if label != "definition":
            continue
        if any(layout["top_definition_row"] == row_index for layout in layouts):
            continue
        if labels.get(row_index + 1) == "board_pin":
            layouts.append({
                "top_definition_row": row_index,
                "top_pin_row": None,
                "board_pin_row": row_index + 1,
                "lower_pin_row": None,
                "lower_definition_row": None,
            })
    return sorted(layouts, key=lambda layout: int(layout["top_definition_row"] or 0))


def _front_view_layout_fills(
    sheet: ParsedSheet,
    layout_id: str,
    layout: dict[str, int | None],
    mappings: dict[str, dict[str, str]],
) -> tuple[list[WorkbookRegionSchema], list[WorkbookFill], list[dict[str, str]]]:
    top_definition_row = int(layout["top_definition_row"] or 0)
    top_pin_row = layout["top_pin_row"]
    board_pin_row = int(layout["board_pin_row"] or 0)
    if top_pin_row is None:
        return [], [], [{
            "code": "icd_front_view_unresolved_layout",
            "severity": "blocking",
            "layout_id": layout_id,
            "message": "前视图缺少可解析的“管脚号 Pin Number”格位；请在模板中保留例如 X1900-1 的管脚号。",
        }]
    top_pin_row = int(top_pin_row)
    lower_pin_row = layout["lower_pin_row"]
    lower_definition_row = layout["lower_definition_row"]
    slots = _front_view_slots(
        sheet,
        top_pin_row,
        board_pin_row,
        top_definition_row,
        lower_pin_row=int(lower_pin_row) if lower_pin_row is not None else None,
        lower_definition_row=int(lower_definition_row) if lower_definition_row is not None else None,
    )
    if not slots:
        return [], [], [{
            "code": "icd_front_view_unresolved_layout",
            "severity": "blocking",
            "layout_id": layout_id,
            "message": "前视图缺少可解析的“管脚号 Pin Number”格位；请在模板中保留例如 X1900-1 的管脚号。",
        }]
    inferred_refdes = {slot["refdes"] for slot in slots if slot.get("refdes")}
    issues: list[dict[str, str]] = []
    resolved_slots: list[dict[str, Any]] = []
    for slot in slots:
        refdes = str(slot.get("refdes") or "")
        pin_name = str(slot.get("pin_name") or "")
        if not refdes and len(inferred_refdes) == 1:
            refdes = next(iter(inferred_refdes))
        if not refdes or not pin_name:
            issues.append({
                "code": "icd_front_view_unresolved_layout",
                "severity": "blocking",
                "layout_id": layout_id,
                "message": "前视图存在无法确定连接器或管脚号的格位；请保留 X1900-1 形式的管脚号，或提供唯一的连接器范围。",
            })
            continue
        key = _pin_key(refdes, pin_name)
        mapping = mappings.get(key)
        if mapping is None:
            issues.append({
                "code": "icd_front_view_unknown_pin",
                "severity": "blocking",
                "layout_id": layout_id,
                "cell": str(slot["pin_cell"]),
                "refdes": refdes,
                "pin_name": pin_name,
                "message": "前视图格位引用的管脚不在冻结 ICD 范围中。",
            })
            continue
        resolved_slots.append({**slot, "mapping": mapping})
    if issues:
        return [], [], issues
    regions: list[WorkbookRegionSchema] = []
    fills: list[WorkbookFill] = []
    seen_cells: set[str] = set()
    for slot in resolved_slots:
        mapping = slot["mapping"]
        _add_front_view_fill(
            sheet, layout_id, str(slot["definition_cell"]), "definition",
            mapping["net_name"], regions, fills, seen_cells,
        )
        _add_front_view_fill(
            sheet, layout_id, str(slot["pin_cell"]), "pin",
            f"{mapping['refdes']}-{mapping['pin_name']}", regions, fills, seen_cells,
        )
        board_cell = slot.get("board_pin_cell")
        if board_cell:
            _add_front_view_fill(
                sheet, layout_id, str(board_cell), "board-pin",
                mapping["pin_name"], regions, fills, seen_cells,
            )
    return regions, fills, []


def _front_view_slots(
    sheet: ParsedSheet,
    top_pin_row: int,
    board_pin_row: int,
    top_definition_row: int,
    *,
    lower_pin_row: int | None,
    lower_definition_row: int | None,
) -> list[dict[str, str | None]]:
    slots: list[dict[str, str | None]] = []
    slots.extend(_front_view_row_slots(
        sheet, top_pin_row, top_definition_row, board_pin_row,
    ))
    if lower_pin_row is not None and lower_definition_row is not None:
        slots.extend(_front_view_row_slots(
            sheet, lower_pin_row, lower_definition_row, None,
        ))
    return slots


def _front_view_row_slots(
    sheet: ParsedSheet,
    pin_row: int,
    definition_row: int,
    board_pin_row: int | None,
) -> list[dict[str, str | None]]:
    pin_values = _sheet_row_values(sheet, pin_row)
    board_values = _sheet_row_values(sheet, board_pin_row) if board_pin_row is not None else {}
    slots: list[dict[str, str | None]] = []
    for column in sorted(set(pin_values) | set(board_values)):
        if column == 1:
            continue
        pin_value = pin_values.get(column, "")
        board_value = board_values.get(column, "")
        match = _FRONT_VIEW_PIN.fullmatch(pin_value.strip())
        if not match and not board_value.strip():
            continue
        slots.append({
            "refdes": match.group("refdes") if match else None,
            "pin_name": _normalize_pin(match.group("pin")) if match else _normalize_pin(board_value),
            "pin_cell": _cell_ref(column, pin_row),
            "definition_cell": _cell_ref(column, definition_row),
            "board_pin_cell": _cell_ref(column, board_pin_row) if board_pin_row is not None else None,
        })
    return slots


def _add_front_view_fill(
    sheet: ParsedSheet,
    layout_id: str,
    cell: str,
    role: str,
    value: str,
    regions: list[WorkbookRegionSchema],
    fills: list[WorkbookFill],
    seen_cells: set[str],
) -> None:
    if cell in seen_cells:
        return
    seen_cells.add(cell)
    baseline_value = next(
        (item.value for item in sheet.cells if item.ref.upper() == cell),
        None,
    )
    region_id = f"{layout_id}-{cell.lower()}-{role}"
    regions.append(WorkbookRegionSchema(
        region_id=region_id,
        sheet_name=sheet.name,
        locator={"cell": cell},
        role="evidence_derived",
        write_policy="deterministic_only",
        expected_value_hash=workbook_value_hash(baseline_value),
        allow_nonempty_overwrite=True,
    ))
    fills.append(WorkbookFill(
        region_id=region_id,
        semantic_unit_id="icd_front_view",
        value=value,
    ))


def _front_view_row_label(row: list[str]) -> str | None:
    label = next((str(value) for value in row if str(value).strip()), "")
    normalized = _normalize(label)
    if _header_contains(normalized, _DEFINITION_LABELS):
        return "definition"
    if _header_contains(normalized, _PIN_LABELS):
        return "pin"
    if _header_contains(normalized, _BOARD_PIN_LABELS):
        return "board_pin"
    return None


def _sheet_row_values(sheet: ParsedSheet, row_index: int | None) -> dict[int, str]:
    if row_index is None:
        return {}
    physical_row = (
        sheet.row_indices[row_index]
        if row_index < len(sheet.row_indices)
        else row_index + 1
    )
    return {
        cell.col_index: cell.value
        for cell in sheet.cells
        if cell.row_index == physical_row
    }


def _cell_ref(column_index: int, row_index: int) -> str:
    return f"{_column_letter(column_index)}{row_index + 1}"


def build_connector_rows(
    connectors: Iterable[ComponentInstance],
    *,
    function_notes: dict[str, str] | None = None,
    reservation_notes: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Make explicit ICD rows from all parsed pins, including NC and grounds."""

    function_notes = function_notes or {}
    reservation_notes = reservation_notes or {}
    rows: list[dict[str, str]] = []
    for connector in connectors:
        for pin in connector.pins:
            pin_name = _normalize_pin(pin.name)
            key = _pin_key(connector.refdes, pin_name)
            connected = str(pin.net or "").strip()
            rows.append({
                "pin": f"{connector.refdes}-{pin_name}",
                "definition": connected or "NC",
                "function": function_notes.get(key, ""),
                "notice": reservation_notes.get(
                    key,
                    "" if connected else "源文件未声明网络连接",
                ),
            })
    return rows


def load_selected_connectors(
    edf_path: str | Path,
    connector_refdes: Iterable[str],
) -> list[ComponentInstance]:
    """Load precisely the author-requested connector scope from an EDF file."""

    selected = {str(refdes).strip().casefold() for refdes in connector_refdes if str(refdes).strip()}
    if not selected:
        raise ValueError("at least one connector reference designator is required")
    instances, _nets, _modules = EdfParser(str(edf_path)).parse()
    found = [instance for instance in instances if instance.refdes.casefold() in selected]
    found_names = {instance.refdes.casefold() for instance in found}
    missing = sorted(selected - found_names)
    if missing:
        raise ValueError(f"connector reference designators were not found in EDF: {missing}")
    return found


def extract_fpt_pin_notes(
    fpt_path: str | Path | None,
    connector_refdes: Iterable[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Extract the first direct functional statement and reservation note per pin."""

    if not fpt_path:
        return {}, {}
    selected = {str(refdes).strip().casefold() for refdes in connector_refdes}
    functions: dict[str, str] = {}
    reservations: dict[str, str] = {}
    first_direct_reference_seen: set[str] = set()
    workbook = parse_xlsx(str(fpt_path))
    for sheet in workbook.sheets:
        if _is_example_sheet(sheet.name):
            continue
        for row in sheet.rows:
            values = [str(value).strip() for value in row if str(value).strip()]
            references = [
                _pin_key(refdes, pin)
                for value in values
                for refdes, pin in _PIN_REFERENCE.findall(value)
                if refdes.casefold() in selected
            ]
            if not references:
                continue
            descriptive = [
                value for value in values
                if not _PIN_REFERENCE.search(value) and not _is_test_status(value)
            ]
            function = next((value for value in descriptive if "预留" not in value), "")
            reservation = next((value for value in descriptive if "预留" in value), "")
            for key in references:
                # FPT contains different test scenarios for one physical pin.
                # Only the first direct statement is safe to turn into an ICD
                # attribute; a later scenario must not retrospectively mark an
                # active interface as "reserved".
                if key in first_direct_reference_seen:
                    continue
                first_direct_reference_seen.add(key)
                if function:
                    functions[key] = function
                if reservation:
                    reservations[key] = reservation
    return functions, reservations


def extract_project_metadata(
    connectors: Iterable[ComponentInstance],
    *,
    fpt_path: str | Path | None = None,
    requirements_path: str | Path | None = None,
) -> dict[str, str]:
    """Collect scalar metadata from supplied project sources, without defaults."""

    connector_list = list(connectors)
    board_model, harness_model = _extract_connector_models(requirements_path)
    component_models = _unique_nonempty(
        connector.value or connector.part_number for connector in connector_list
    )
    if board_model:
        component_models = [
            board_model,
            *[model for model in component_models if model not in board_model],
        ]
    return {
        "product_name": _extract_project_name(fpt_path),
        "customer_number": "",
        "pcb_connector": "；".join(component_models),
        "harness_connector": harness_model,
        "location_number": "、".join(connector.refdes for connector in connector_list),
    }


def generate_icd_workbook(
    *,
    template_path: str | Path,
    edf_path: str | Path,
    connector_refdes: Iterable[str],
    fpt_path: str | Path | None = None,
    requirements_path: str | Path | None = None,
) -> IcdGenerationResult:
    """Generate an ICD candidate from an ICD-like workbook and project files."""

    connector_refs = [str(refdes).strip() for refdes in connector_refdes if str(refdes).strip()]
    connectors = load_selected_connectors(edf_path, connector_refs)
    function_notes, reservation_notes = extract_fpt_pin_notes(fpt_path, connector_refs)
    rows = build_connector_rows(
        connectors,
        function_notes=function_notes,
        reservation_notes=reservation_notes,
    )
    metadata = extract_project_metadata(
        connectors,
        fpt_path=fpt_path,
        requirements_path=requirements_path,
    )
    content = Path(template_path).read_bytes()
    workbook = parse_xlsx(str(template_path))
    sheet, header_index, columns = _discover_pin_table(workbook.sheets)
    table_schema = _build_table_schema(sheet, header_index, columns)
    regions, fills = _build_scalar_fills(workbook.sheets, metadata)
    table_rows = [_map_table_row(columns, row) for row in rows]
    plan = WorkbookFillPlan(
        template_version_id="icd-source-evaluation",
        fills=fills,
        table_fills=[WorkbookTableFill(
            table_region_id=table_schema.table_region_id,
            semantic_unit_id=table_schema.semantic_unit_id,
            rows=table_rows,
        )],
    )
    rendered = XlsmRenderer().render(
        content,
        regions,
        plan,
        RendererPolicy(renderer_policy_id="icd-source-evaluation"),
        security_approved=True,
        table_schemas=[table_schema],
    )
    return IcdGenerationResult(
        content=rendered.content,
        source_summary={
            "connectors": [connector.refdes for connector in connectors],
            "pin_count": len(rows),
            "function_note_count": len(function_notes),
            "reservation_note_count": len(reservation_notes),
            "metadata": metadata,
        },
        integrity_manifest=rendered.integrity_manifest,
    )


def _discover_pin_table(sheets: list[ParsedSheet]) -> tuple[ParsedSheet, int, list[dict[str, str]]]:
    for sheet in sheets:
        if _is_example_sheet(sheet.name):
            continue
        for header_index, row in enumerate(sheet.rows):
            columns = _table_columns(row)
            if {"pin", "definition"} <= {column["column_id"] for column in columns}:
                return sheet, header_index, columns
    raise ValueError("template has no recognizable Pin Number / Pin Definition table")


def _table_columns(header: list[str]) -> list[dict[str, str]]:
    columns: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for index, label in enumerate(header):
        text = str(label).strip()
        if not text:
            continue
        normalized = _normalize(text)
        if _header_contains(normalized, ("pin number", "管脚号", "引脚号", "针脚号")):
            column_id = "pin"
        elif _header_contains(normalized, ("pin definition", "管脚定义", "引脚定义", "signal definition")):
            column_id = "definition"
        elif _header_contains(normalized, ("function", "功能描述", "功能")):
            column_id = "function"
        elif _header_contains(normalized, ("notice", "备注", "说明")):
            column_id = "notice"
        else:
            column_id = f"column_{index + 1}"
        while column_id in used_ids:
            column_id = f"{column_id}_{index + 1}"
        used_ids.add(column_id)
        columns.append({
            "column_id": column_id,
            "label": text,
            "column_letter": _column_letter(index + 1),
        })
    return columns


def _build_table_schema(
    sheet: ParsedSheet,
    header_index: int,
    columns: list[dict[str, str]],
) -> WorkbookTableSchema:
    first_data_row = header_index + 2
    last_template_row = first_data_row - 1
    for row_index in range(header_index + 1, len(sheet.rows)):
        if not any(str(value).strip() for value in sheet.rows[row_index]):
            break
        last_template_row = row_index + 1
    if last_template_row < first_data_row:
        raise ValueError("Pin Definition table has no writable sample row")
    expected_value_hashes = {
        f"{column['column_letter']}{row_number}": workbook_value_hash(
            _row_value(sheet.rows, row_number - 1, _column_number(column["column_letter"]) - 1)
        )
        for row_number in range(first_data_row, last_template_row + 1)
        for column in columns
    }
    return WorkbookTableSchema(
        table_region_id="discovered-pin-definition-table",
        semantic_unit_id="pin_definition",
        sheet_name=sheet.name,
        header_row=header_index + 1,
        first_data_row=first_data_row,
        last_template_row=last_template_row,
        style_source_row=last_template_row,
        max_output_rows=max(1_000, last_template_row - first_data_row + 1),
        columns=[WorkbookTableColumnSchema(**column) for column in columns],
        expected_value_hashes=expected_value_hashes,
        allow_example_region_replacement=True,
    )


def _build_scalar_fills(
    sheets: list[ParsedSheet],
    values: dict[str, str],
) -> tuple[list[WorkbookRegionSchema], list[WorkbookFill]]:
    labels = {
        "product_name": ("产品名称", "product name", "项目名称", "project /serial"),
        "customer_number": ("客户编号", "customer number"),
        "pcb_connector": ("板端接插件", "pcb connector"),
        "harness_connector": ("线束端", "harness connector"),
        "location_number": ("控制器上编号", "location number"),
    }
    regions: list[WorkbookRegionSchema] = []
    fills: list[WorkbookFill] = []
    for field, candidates in labels.items():
        target = _find_scalar_target(sheets, candidates)
        if target is None:
            continue
        sheet, cell, baseline_value = target
        region_id = f"discovered-{field}"
        regions.append(WorkbookRegionSchema(
            region_id=region_id,
            sheet_name=sheet.name,
            locator={"cell": cell},
            role="evidence_derived",
            write_policy="validated_draft",
            expected_value_hash=workbook_value_hash(baseline_value),
            allow_nonempty_overwrite=True,
        ))
        fills.append(WorkbookFill(
            region_id=region_id,
            semantic_unit_id=field,
            value=values.get(field, ""),
        ))
    return regions, fills


def _find_scalar_target(
    sheets: list[ParsedSheet],
    candidates: tuple[str, ...],
) -> tuple[ParsedSheet, str, str | None] | None:
    for sheet in sheets:
        if _is_example_sheet(sheet.name):
            continue
        for row_index, row in enumerate(sheet.rows):
            for column_index, value in enumerate(row):
                if not _header_contains(_normalize(value), candidates):
                    continue
                for target_index in range(column_index + 1, len(row)):
                    target_value = _row_value([row], 0, target_index)
                    if target_value is not None:
                        return (
                            sheet,
                            f"{_column_letter(target_index + 1)}{row_index + 1}",
                            target_value,
                        )
    return None


def _map_table_row(columns: list[dict[str, str]], row: dict[str, str]) -> dict[str, str]:
    return {
        column["column_id"]: row.get(column["column_id"], "")
        for column in columns
    }


def _extract_project_name(fpt_path: str | Path | None) -> str:
    if not fpt_path:
        return ""
    for sheet in parse_xlsx(str(fpt_path)).sheets:
        if _is_example_sheet(sheet.name):
            continue
        for row in sheet.rows:
            for index, value in enumerate(row):
                if _header_contains(_normalize(value), ("项目名称", "project /serial", "project name")):
                    for candidate in row[index + 1:]:
                        text = str(candidate).strip()
                        if text:
                            return text
    return ""


def _extract_connector_models(requirements_path: str | Path | None) -> tuple[str, str]:
    if not requirements_path:
        return "", ""
    text = "\n".join(
        str(value)
        for sheet in parse_xlsx(str(requirements_path)).sheets
        for row in sheet.rows
        for value in row
        if str(value).strip()
    )
    return (
        _model_after_label(text, "板端型号"),
        _model_after_label(text, "线束端型号"),
    )


def _model_after_label(text: str, label: str) -> str:
    match = re.search(_MODEL_PATTERN.pattern.format(label=re.escape(label)), text)
    if not match:
        return ""
    return re.sub(r"\s+", "/", match.group(1).strip())


def _normalize_pin(value: str) -> str:
    return str(value).strip().removeprefix("&")


def _pin_key(refdes: str, pin: str) -> str:
    return f"{str(refdes).strip().casefold()}:{_normalize_pin(pin).casefold()}"


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _header_contains(value: str, candidates: tuple[str, ...]) -> bool:
    return any(candidate in value for candidate in candidates)


def _is_test_status(value: str) -> bool:
    return _normalize(value) in {"stb,stg,opl", "na", "n/a"}


def _is_example_sheet(name: str) -> bool:
    normalized = _normalize(name)
    return any(token in normalized for token in ("example", "sample", "示例", "样例"))


def _unique_nonempty(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _row_value(rows: list[list[str]], row_index: int, column_index: int) -> str | None:
    if row_index >= len(rows) or column_index >= len(rows[row_index]):
        return None
    value = rows[row_index][column_index]
    return str(value) if str(value).strip() else None


def _column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _column_number(letter: str) -> int:
    result = 0
    for character in letter.upper():
        result = result * 26 + ord(character) - 64
    return result
