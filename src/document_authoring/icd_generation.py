"""Evidence-led, template-driven ICD workbook generation utilities.

The module deliberately takes project files and connector reference designators as
inputs.  It does not contain project names, connector names, or fixed workbook
coordinates; the table and scalar targets are discovered from the uploaded
template's labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

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
