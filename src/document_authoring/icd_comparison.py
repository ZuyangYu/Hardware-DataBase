"""Generic, repeatable comparison of ICD connector pin tables."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import io
import os
import re
from typing import Any
import zipfile
from xml.etree import ElementTree as ET

from src.pipelines.spreadsheet.xlsx_parser import ParsedWorkbook


PARITY_METRIC_VERSION = "icd-pin-definition-v1"
PACKAGE_METRIC_VERSION = "icd-ooxml-package-v1"
PIN_DEFINITION_REQUIRED_COLUMNS = ("connector", "pin_number", "pin_definition")
PIN_DEFINITION_VALUE_COLUMNS = (
    "connector",
    "pin_number",
    "pin_definition",
    "function",
    "notice",
)


_PIN_HEADERS = {
    "pin",
    "pin no",
    "pin no.",
    "pin number",
    "pin #",
    "pin num",
    "引脚号",
    "管脚号",
    "针脚号",
    "管脚编号",
    "引脚编号",
}
_DEFINITION_HEADERS = {
    "pin definition",
    "signal",
    "signal definition",
    "definition",
    "信号",
    "信号定义",
    "管脚定义",
    "引脚定义",
    "定义",
}
_FUNCTION_HEADERS = {
    "function",
    "function description",
    "功能",
    "功能描述",
    "功能定义",
}
_NOTICE_HEADERS = {
    "notice",
    "remark",
    "remarks",
    "note",
    "备注",
    "说明",
}
_CONNECTOR_HEADERS = {
    "connector",
    "connector id",
    "connector name",
    "connector number",
    "接插件",
    "连接器",
    "连接器编号",
    "接插件编号",
}
_LOCATION_HEADERS = {
    "location",
    "location number",
    "connector location",
    "控制器上编号",
    "位置编号",
    "安装位置",
    "接插件位置",
}
_PIN_IDENTIFIER = re.compile(r"^&?[a-z]*\d+[a-z0-9_.-]*$", re.IGNORECASE)
_EMBEDDED_LOCATION_PIN = re.compile(
    r"^(?P<connector>[a-z]+\d+)[\-_/](?P<pin>[a-z0-9_.]+)$",
    re.IGNORECASE,
)


def compare_workbooks(
    reference: ParsedWorkbook,
    generated: ParsedWorkbook,
    *,
    reference_evidence: Mapping[str, Any] | None = None,
    generated_evidence: Mapping[str, Any] | None = None,
    reference_package: bytes | bytearray | os.PathLike[str] | str | None = None,
    generated_package: bytes | bytearray | os.PathLike[str] | str | None = None,
    reference_format: str | None = None,
    generated_format: str | None = None,
) -> dict[str, Any]:
    """Compare discovered pin tables without relying on template-specific cells."""

    reference_rows, reference_warnings = _extract_pin_rows(reference)
    generated_rows, generated_warnings = _extract_pin_rows(generated)
    warnings = [
        *(["人工 ICD 未发现可识别的管脚表。"] if not reference_rows else []),
        *(["生成 ICD 未发现可识别的管脚表。"] if not generated_rows else []),
        *reference_warnings,
        *generated_warnings,
    ]
    reference_by_key = {row["key"]: row for row in reference_rows}
    generated_by_key = {row["key"]: row for row in generated_rows}
    shared_keys = sorted(set(reference_by_key) & set(generated_by_key))
    matched = [
        {"key": key, "definition": reference_by_key[key]["definition"]}
        for key in shared_keys
        if reference_by_key[key]["definition"] == generated_by_key[key]["definition"]
    ]
    mismatched = [
        {
            "key": key,
            "reference_definition": reference_by_key[key]["definition"],
            "generated_definition": generated_by_key[key]["definition"],
        }
        for key in shared_keys
        if reference_by_key[key]["definition"] != generated_by_key[key]["definition"]
    ]
    reference_only = [
        {"key": key, "definition": reference_by_key[key]["definition"]}
        for key in sorted(set(reference_by_key) - set(generated_by_key))
    ]
    generated_only = [
        {"key": key, "definition": generated_by_key[key]["definition"]}
        for key in sorted(set(generated_by_key) - set(reference_by_key))
    ]
    reference_count = len(reference_by_key)
    covered = len(shared_keys)
    result = {
        "summary": {
            "reference_pin_count": reference_count,
            "generated_pin_count": len(generated_by_key),
            "matching_pin_count": len(matched),
            "mismatched_pin_count": len(mismatched),
            "reference_only_pin_count": len(reference_only),
            "generated_only_pin_count": len(generated_only),
            "exact_match_rate": _rate(len(matched), reference_count),
            "reference_coverage": _rate(covered, reference_count),
        },
        "matched": matched,
        "mismatched": mismatched,
        "reference_only": reference_only,
        "generated_only": generated_only,
        "content_quality": {
            "function": _field_quality(
                reference_by_key,
                generated_by_key,
                shared_keys,
                "function",
            ),
            "notice": _field_quality(
                reference_by_key,
                generated_by_key,
                shared_keys,
                "notice",
            ),
        },
        "warnings": warnings,
    }
    result["parity"] = compare_pin_definition_workbooks(
        reference,
        generated,
        reference_evidence=reference_evidence,
        generated_evidence=generated_evidence,
        reference_package=reference_package,
        generated_package=generated_package,
        reference_format=reference_format,
        generated_format=generated_format,
    )
    return result


def normalize_pin_definition_workbook(
    workbook: ParsedWorkbook,
    *,
    evidence_by_key: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a Pin Definition workbook into a format-neutral table.

    The normalized row key is the stable ``connector:pin_number`` identity
    already used by the legacy ICD validator.  This function retains every
    row occurrence so duplicate rows cannot disappear from the parity report;
    consumers that need a set use ``row_keys`` or the first occurrence per key.
    """

    rows, table_specs, warnings = _extract_pin_rows_detailed(
        workbook,
        evidence_by_key=evidence_by_key,
    )
    counts = Counter(row["key"] for row in rows)
    duplicate_keys = sorted(key for key, count in counts.items() if count > 1)
    unique_rows = _unique_rows(rows)
    required_present = [
        column
        for column in PIN_DEFINITION_REQUIRED_COLUMNS
        if any(spec["required_present"].get(column, False) for spec in table_specs)
    ]
    required_missing = [
        column
        for column in PIN_DEFINITION_REQUIRED_COLUMNS
        if any(not spec["required_present"].get(column, False) for spec in table_specs)
        or not table_specs
    ]
    required_denominator = (
        len(table_specs) * len(PIN_DEFINITION_REQUIRED_COLUMNS)
        if table_specs
        else len(PIN_DEFINITION_REQUIRED_COLUMNS)
    )
    required_numerator = sum(
        sum(
            bool(spec["required_present"].get(column, False))
            for column in PIN_DEFINITION_REQUIRED_COLUMNS
        )
        for spec in table_specs
    )
    table_payload = [
        {
            "header_order": spec["header_order"],
            "required_present": dict(spec["required_present"]),
            "missing": [
                column
                for column in PIN_DEFINITION_REQUIRED_COLUMNS
                if not spec["required_present"].get(column, False)
            ],
        }
        for spec in table_specs
    ]
    output_rows = []
    for row in rows:
        output = dict(row)
        output.pop("_table_id", None)
        output_rows.append(output)
    return {
        "metric_version": PARITY_METRIC_VERSION,
        "required_columns": list(PIN_DEFINITION_REQUIRED_COLUMNS),
        "columns": {
            "required": {
                "expected": list(PIN_DEFINITION_REQUIRED_COLUMNS),
                "present": required_present,
                "missing": required_missing,
                "numerator": required_numerator,
                "denominator": required_denominator,
                "completeness": _rate(required_numerator, required_denominator),
            },
            "tables": table_payload,
        },
        "rows": output_rows,
        "row_keys": [row["key"] for row in unique_rows],
        "duplicate_keys": duplicate_keys,
        "duplicate_rows": [
            {"key": key, "count": counts[key]} for key in duplicate_keys
        ],
        "evidence": _evidence_summary(
            unique_rows,
            evidence_provided=evidence_by_key is not None,
        ),
        "static_structure": _static_structure(workbook),
        "warnings": warnings,
    }


def compare_pin_definition_workbooks(
    reference: ParsedWorkbook,
    generated: ParsedWorkbook,
    *,
    reference_evidence: Mapping[str, Any] | None = None,
    generated_evidence: Mapping[str, Any] | None = None,
    reference_package: bytes | bytearray | os.PathLike[str] | str | None = None,
    generated_package: bytes | bytearray | os.PathLike[str] | str | None = None,
    reference_format: str | None = None,
    generated_format: str | None = None,
) -> dict[str, Any]:
    """Compare Pin Definition tables using stable row and cell contracts."""

    normalized_reference = normalize_pin_definition_workbook(
        reference,
        evidence_by_key=reference_evidence,
    )
    normalized_generated = normalize_pin_definition_workbook(
        generated,
        evidence_by_key=generated_evidence,
    )
    reference_rows = _unique_rows(normalized_reference["rows"])
    generated_rows = _unique_rows(normalized_generated["rows"])
    reference_keys = [row["key"] for row in reference_rows]
    generated_keys = [row["key"] for row in generated_rows]
    reference_set = set(reference_keys)
    generated_set = set(generated_keys)
    shared_keys = sorted(reference_set & generated_set)
    missing_keys = sorted(reference_set - generated_set)
    extra_keys = sorted(generated_set - reference_set)
    generated_by_key = {row["key"]: row for row in generated_rows}
    reference_by_key = {row["key"]: row for row in reference_rows}

    shared_reference_order = [key for key in reference_keys if key in generated_set]
    shared_generated_order = [key for key in generated_keys if key in reference_set]
    relative_numerator, relative_denominator = _relative_order_score(
        shared_reference_order,
        shared_generated_order,
    )
    cross_field = _cross_field_consistency(
        reference_by_key,
        generated_by_key,
        shared_keys,
    )
    package_result = None
    if reference_package is not None and generated_package is not None:
        package_result = compare_ooxml_packages(
            reference_package,
            generated_package,
            reference_format=reference_format,
            generated_format=generated_format,
        )

    metrics = {
        "row_key_precision": _parity_metric(
            "row_key_precision",
            len(shared_keys),
            len(generated_keys),
            "not_below_baseline",
        ),
        "row_key_recall": _parity_metric(
            "row_key_recall",
            len(shared_keys),
            len(reference_keys),
            "not_below_baseline",
        ),
        "row_key_f1": _parity_metric(
            "row_key_f1",
            2 * len(shared_keys),
            len(reference_keys) + len(generated_keys),
            "not_below_baseline",
        ),
        "required_column_completeness": _parity_metric(
            "required_column_completeness",
            normalized_generated["columns"]["required"]["numerator"],
            normalized_generated["columns"]["required"]["denominator"],
            "not_below_baseline",
            detail={
                "reference_missing": normalized_reference["columns"]["required"][
                    "missing"
                ],
                "generated_missing": normalized_generated["columns"]["required"][
                    "missing"
                ],
            },
        ),
        "exact_order_rate": _parity_metric(
            "exact_order_rate",
            int(shared_reference_order == shared_generated_order),
            1,
            "not_below_baseline",
            detail={
                "reference": shared_reference_order,
                "generated": shared_generated_order,
            },
        ),
        "relative_order_rate": _parity_metric(
            "relative_order_rate",
            relative_numerator,
            relative_denominator,
            "not_below_baseline",
        ),
        "duplicate_rate": _parity_metric(
            "duplicate_rate",
            len(normalized_generated["rows"]) - len(generated_keys),
            len(normalized_generated["rows"]),
            "not_above_baseline",
        ),
        "missing_row_rate": _parity_metric(
            "missing_row_rate",
            len(missing_keys),
            len(reference_keys),
            "not_above_baseline",
        ),
        "extra_row_rate": _parity_metric(
            "extra_row_rate",
            len(extra_keys),
            len(generated_keys),
            "not_above_baseline",
        ),
        "cross_field_consistency_rate": _parity_metric(
            "cross_field_consistency_rate",
            cross_field["numerator"],
            cross_field["denominator"],
            "not_below_baseline",
        ),
        "evidence_support_rate": _parity_metric(
            "evidence_support_rate",
            normalized_generated["evidence"]["supported_cell_count"],
            normalized_generated["evidence"]["required_cell_count"],
            "not_below_baseline",
            unknown=not normalized_generated["evidence"]["provided"],
        ),
        "physical_protection_rate": _parity_metric(
            "physical_protection_rate",
            int(bool(package_result and package_result["preservation_ok"])),
            1,
            "not_below_baseline",
            unknown=package_result is None,
        ),
    }
    return {
        "metric_version": PARITY_METRIC_VERSION,
        "normalized": {
            "reference": normalized_reference,
            "generated": normalized_generated,
        },
        "row_keys": {
            "reference": reference_keys,
            "generated": generated_keys,
            "shared": shared_keys,
            "missing": missing_keys,
            "extra": extra_keys,
        },
        "duplicates": {
            "reference_keys": normalized_reference["duplicate_keys"],
            "generated_keys": normalized_generated["duplicate_keys"],
            "reference_rows": normalized_reference["duplicate_rows"],
            "generated_rows": normalized_generated["duplicate_rows"],
        },
        "columns": {
            "reference": normalized_reference["columns"],
            "generated": normalized_generated["columns"],
        },
        "order": {
            "reference": reference_keys,
            "generated": generated_keys,
            "shared_reference": shared_reference_order,
            "shared_generated": shared_generated_order,
            "exact": shared_reference_order == shared_generated_order,
            "relative": _parity_metric(
                "relative_order_rate",
                relative_numerator,
                relative_denominator,
                "not_below_baseline",
            ),
        },
        "cross_field_consistency": cross_field,
        "evidence": {
            "reference": normalized_reference["evidence"],
            "generated": normalized_generated["evidence"],
        },
        "package": package_result,
        "metrics": metrics,
    }


def inspect_ooxml_package(
    content: bytes | bytearray | os.PathLike[str] | str,
    *,
    format: str | None = None,
) -> dict[str, Any]:
    """Inventory an XLSX/XLSM package without loading or executing Office code."""

    package_bytes = _package_bytes(content)
    with zipfile.ZipFile(io.BytesIO(package_bytes), "r") as package:
        names = sorted(package.namelist())
        name_set = set(names)
        macro_parts = sorted(
            name
            for name in names
            if name.casefold().endswith("vbaproject.bin") or "/vba" in name.casefold()
        )
        external_link_parts = sorted(
            name for name in names if name.casefold().startswith("xl/externallinks/")
        )
        embedded_parts = sorted(
            name
            for name in names
            if "/embeddings/" in name.casefold()
            or "/activex/" in name.casefold()
            or "/ctrlprops/" in name.casefold()
        )
        content_types = (
            package.read("[Content_Types].xml")
            if "[Content_Types].xml" in name_set
            else b""
        )
        macro_enabled = b"macroenabled" in content_types.lower() or bool(macro_parts)
        workbook_protected = False
        formula_count = 0
        merged_range_count = 0
        protected_sheet_count = 0
        sheet_count = 0
        for path in names:
            if path == "xl/workbook.xml":
                root = ET.fromstring(package.read(path))
                workbook_protected = _has_local_element(root, "workbookProtection")
                sheet_count = sum(1 for _ in _local_elements(root, "sheet"))
            if not path.casefold().startswith(
                "xl/worksheets/"
            ) or not path.casefold().endswith(".xml"):
                continue
            root = ET.fromstring(package.read(path))
            protected_sheet_count += int(_has_local_element(root, "sheetProtection"))
            formula_count += sum(1 for _ in _local_elements(root, "f"))
            merged_range_count += sum(1 for _ in _local_elements(root, "mergeCell"))
        source_name = str(getattr(content, "name", ""))
        inferred_format = "xlsm" if macro_enabled else "xlsx"
        if format:
            inferred_format = str(format).casefold()
        elif source_name.casefold().endswith(".xlsm"):
            inferred_format = "xlsm"
        part_hashes = {
            name: hashlib.sha256(package.read(name)).hexdigest() for name in names
        }
    return {
        "metric_version": PACKAGE_METRIC_VERSION,
        "format": inferred_format,
        "package_hash": hashlib.sha256(package_bytes).hexdigest(),
        "part_hashes": part_hashes,
        "has_vba_project": bool(macro_parts),
        "macro_parts": macro_parts,
        "external_link_count": len(external_link_parts),
        "external_link_parts": external_link_parts,
        "embedded_parts": embedded_parts,
        "sheet_count": sheet_count,
        "protected_sheet_count": protected_sheet_count,
        "workbook_protected": workbook_protected,
        "formula_count": formula_count,
        "merged_range_count": merged_range_count,
    }


def compare_ooxml_packages(
    reference: bytes | bytearray | os.PathLike[str] | str,
    generated: bytes | bytearray | os.PathLike[str] | str,
    *,
    reference_format: str | None = None,
    generated_format: str | None = None,
) -> dict[str, Any]:
    """Compare safe, deterministic package observations for XLSX/XLSM output."""

    reference_inventory = inspect_ooxml_package(reference, format=reference_format)
    generated_inventory = inspect_ooxml_package(generated, format=generated_format)
    vba_content_preserved = _vba_content_preserved(
        reference_inventory,
        generated_inventory,
    )
    checks = {
        "vba_preserved": (
            not reference_inventory["has_vba_project"]
            or generated_inventory["has_vba_project"]
        ),
        "vba_content_preserved": vba_content_preserved,
        "external_links_not_added": (
            generated_inventory["external_link_count"]
            <= reference_inventory["external_link_count"]
        ),
        "sheet_protection_preserved": (
            generated_inventory["protected_sheet_count"]
            >= reference_inventory["protected_sheet_count"]
        ),
        "workbook_protection_preserved": (
            not reference_inventory["workbook_protected"]
            or generated_inventory["workbook_protected"]
        ),
        "formulas_preserved": (
            generated_inventory["formula_count"] == reference_inventory["formula_count"]
        ),
        "merged_ranges_preserved": (
            generated_inventory["merged_range_count"]
            == reference_inventory["merged_range_count"]
        ),
    }
    issues = []
    if not checks["vba_preserved"]:
        issues.append("generated_removed_vba_project")
    elif not checks["vba_content_preserved"]:
        issues.append("generated_changed_vba_project")
    if not checks["external_links_not_added"]:
        issues.append("generated_added_external_links")
    if not checks["sheet_protection_preserved"]:
        issues.append("generated_removed_sheet_protection")
    if not checks["workbook_protection_preserved"]:
        issues.append("generated_removed_workbook_protection")
    if not checks["formulas_preserved"]:
        issues.append("generated_changed_formula_count")
    if not checks["merged_ranges_preserved"]:
        issues.append("generated_changed_merged_range_count")
    return {
        "metric_version": PACKAGE_METRIC_VERSION,
        "reference": reference_inventory,
        "generated": generated_inventory,
        "checks": checks,
        "issues": issues,
        "preservation_ok": not issues,
    }


def _vba_content_preserved(
    reference: Mapping[str, Any],
    generated: Mapping[str, Any],
) -> bool:
    reference_parts = reference.get("macro_parts") or []
    if not reference_parts:
        return True
    generated_parts = set(generated.get("macro_parts") or [])
    reference_hashes = reference.get("part_hashes") or {}
    generated_hashes = generated.get("part_hashes") or {}
    return all(
        part in generated_parts
        and reference_hashes.get(part) == generated_hashes.get(part)
        for part in reference_parts
    )


def _extract_pin_rows(
    workbook: ParsedWorkbook,
) -> tuple[list[dict[str, str]], list[str]]:
    """Return the legacy first-row-per-key view used by ICD validation."""

    detailed_rows, _table_specs, _warnings = _extract_pin_rows_detailed(
        workbook,
        evidence_by_key=None,
    )
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    seen_keys: set[str] = set()
    for row in detailed_rows:
        key = row["key"]
        if key in seen_keys:
            warnings.append(f"管脚键重复，已忽略后续记录：{key}。")
            continue
        seen_keys.add(key)
        rows.append(
            {
                "key": key,
                "definition": row["pin_definition"],
                "function": row["function"],
                "notice": row["notice"],
                "sheet": row["sheet"],
            }
        )
    return rows, warnings


def _extract_pin_rows_detailed(
    workbook: ParsedWorkbook,
    *,
    evidence_by_key: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Extract all table occurrences while retaining structural provenance."""

    rows: list[dict[str, Any]] = []
    table_specs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for sheet in workbook.sheets:
        if _is_example_sheet(sheet.name):
            continue
        headers = _find_headers(sheet.rows)
        for header_number, header in enumerate(headers):
            (
                header_index,
                connector_column,
                pin_column,
                definition_column,
                function_column,
                notice_column,
            ) = header
            end_index = (
                headers[header_number + 1][0]
                if header_number + 1 < len(headers)
                else len(sheet.rows)
            )
            location, location_ref = _location_before_header_detail(
                sheet,
                header_index,
            )
            table_id = len(table_specs)
            table_specs.append(
                {
                    "header_order": table_id,
                    "required_present": {
                        "connector": connector_column is not None or bool(location),
                        "pin_number": pin_column is not None,
                        "pin_definition": definition_column is not None,
                    },
                }
            )
            for row_index in range(header_index + 1, end_index):
                row = sheet.rows[row_index]
                pin_raw = _cell(row, pin_column)
                if not _is_pin_identifier(pin_raw):
                    continue
                normalized_pin = _normalize(pin_raw).removeprefix("&")
                embedded = _EMBEDDED_LOCATION_PIN.fullmatch(normalized_pin)
                if connector_column is not None:
                    connector = _normalize(_cell(row, connector_column))
                elif location:
                    connector = _normalize(location)
                elif embedded:
                    connector = _normalize(embedded.group("connector"))
                else:
                    connector = ""
                pin_number = (
                    _normalize(embedded.group("pin")) if embedded else normalized_pin
                )
                key = _pin_key(
                    _cell(row, connector_column)
                    if connector_column is not None
                    else location,
                    pin_raw,
                )
                if embedded and not connector:
                    key = _pin_key("", pin_raw)
                cell_refs = _row_cell_refs(
                    sheet,
                    row_index,
                    connector_column=connector_column,
                    pin_column=pin_column,
                    definition_column=definition_column,
                    function_column=function_column,
                    notice_column=notice_column,
                    location_ref=location_ref,
                    embedded_connector=embedded is not None
                    and connector_column is None
                    and not location,
                )
                rows.append(
                    {
                        "_table_id": table_id,
                        "key": key,
                        "connector": connector,
                        "pin_number": pin_number,
                        "pin_definition": _normalize(_cell(row, definition_column)),
                        "definition": _normalize(_cell(row, definition_column)),
                        "function": _normalize(_cell(row, function_column)),
                        "notice": _normalize(_cell(row, notice_column)),
                        "sheet": _normalize(sheet.name),
                        "declared_order": len(rows),
                        "physical_row": _physical_row(sheet, row_index),
                        "cell_refs": cell_refs,
                        "evidence": _evidence_for_key(evidence_by_key, key),
                    }
                )
            table_rows = [row for row in rows if row["_table_id"] == table_id]
            if any(row["connector"] for row in table_rows):
                table_specs[table_id]["required_present"]["connector"] = True
    return rows, table_specs, warnings


def _unique_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("key") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _row_cell_refs(
    sheet: Any,
    row_index: int,
    *,
    connector_column: int | None,
    pin_column: int,
    definition_column: int,
    function_column: int | None,
    notice_column: int | None,
    location_ref: str,
    embedded_connector: bool,
) -> dict[str, str]:
    refs: dict[str, str] = {}
    if connector_column is not None:
        refs["connector"] = _cell_reference(sheet, row_index, connector_column)
    elif location_ref:
        refs["connector"] = location_ref
    elif embedded_connector:
        refs["connector"] = _cell_reference(sheet, row_index, pin_column)
    refs["pin_number"] = _cell_reference(sheet, row_index, pin_column)
    refs["pin_definition"] = _cell_reference(sheet, row_index, definition_column)
    if function_column is not None:
        refs["function"] = _cell_reference(sheet, row_index, function_column)
    if notice_column is not None:
        refs["notice"] = _cell_reference(sheet, row_index, notice_column)
    return refs


def _cell_reference(sheet: Any, row_index: int, column_index: int) -> str:
    physical_row = _physical_row(sheet, row_index)
    physical_column = column_index + 1
    for cell in getattr(sheet, "cells", []) or []:
        if cell.row_index == physical_row and cell.col_index == physical_column:
            return cell.ref
    return f"{_column_letter(column_index)}{physical_row}"


def _column_letter(column_index: int) -> str:
    if column_index < 0:
        return ""
    letters = ""
    value = column_index
    while True:
        value, remainder = divmod(value, 26)
        letters = chr(ord("A") + remainder) + letters
        if value == 0:
            return letters
        value -= 1


def _location_before_header_detail(sheet: Any, header_index: int) -> tuple[str, str]:
    rows = sheet.rows
    for row_index in range(header_index - 1, -1, -1):
        row = rows[row_index]
        normalized = [_normalize(value) for value in row]
        location_label_index = _first_index(normalized, _LOCATION_HEADERS)
        if location_label_index is None:
            continue
        for value_index, value in enumerate(
            row[location_label_index + 1 :], start=location_label_index + 1
        ):
            if str(value).strip():
                return (
                    str(value).strip(),
                    _cell_reference(sheet, row_index, value_index),
                )
    return "", ""


def _evidence_for_key(
    evidence_by_key: Mapping[str, Any] | None,
    key: str,
) -> dict[str, Any]:
    if evidence_by_key is None:
        return {"provided": False, "by_column": {}}
    payload: Any = None
    for candidate, value in evidence_by_key.items():
        if _normalize(candidate) == _normalize(key):
            payload = value
            break
    by_column: dict[str, list[str]] = {}
    row_ids: list[str] = []
    if isinstance(payload, Mapping):
        for raw_field, raw_ids in payload.items():
            field = _canonical_evidence_field(raw_field)
            ids = _evidence_ids(raw_ids)
            if field in PIN_DEFINITION_VALUE_COLUMNS:
                by_column[field] = ids
            elif str(raw_field).casefold() in {
                "row",
                "row_id",
                "row_ids",
                "evidence_ids",
            }:
                row_ids.extend(ids)
    elif isinstance(payload, Sequence) and not isinstance(
        payload, (str, bytes, bytearray)
    ):
        row_ids.extend(_evidence_ids(payload))
    elif payload is not None:
        row_ids.extend(_evidence_ids(payload))
    if row_ids:
        for field in PIN_DEFINITION_REQUIRED_COLUMNS:
            by_column.setdefault(field, list(dict.fromkeys(row_ids)))
    return {"provided": True, "by_column": by_column}


def _canonical_evidence_field(value: object) -> str:
    normalized = _normalize(value)
    aliases = {
        "connector": "connector",
        "connector id": "connector",
        "pin": "pin_number",
        "pin no": "pin_number",
        "pin number": "pin_number",
        "pin_number": "pin_number",
        "definition": "pin_definition",
        "pin definition": "pin_definition",
        "pin_definition": "pin_definition",
        "signal": "pin_definition",
        "function": "function",
        "notice": "notice",
        "remark": "notice",
    }
    return aliases.get(normalized, normalized)


def _evidence_ids(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = value.get("evidence_ids", value.get("ids", []))
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = value
    else:
        values = [value]
    return list(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )


def _evidence_summary(
    rows: Sequence[dict[str, Any]],
    *,
    evidence_provided: bool,
) -> dict[str, Any]:
    required_cell_count = len(rows) * len(PIN_DEFINITION_REQUIRED_COLUMNS)
    supported_cell_count = 0
    unsupported_keys: list[str] = []
    by_column = {column: 0 for column in PIN_DEFINITION_REQUIRED_COLUMNS}
    for row in rows:
        evidence = row.get("evidence") or {}
        columns = evidence.get("by_column") or {}
        row_supported = True
        for column in PIN_DEFINITION_REQUIRED_COLUMNS:
            if columns.get(column):
                supported_cell_count += 1
                by_column[column] += 1
            else:
                row_supported = False
        if not row_supported:
            unsupported_keys.append(row["key"])
    return {
        "provided": evidence_provided,
        "required_cell_count": required_cell_count,
        "supported_cell_count": supported_cell_count,
        "unsupported_keys": unsupported_keys,
        "by_column": by_column,
        "coverage": (
            None
            if not evidence_provided
            else _rate(supported_cell_count, required_cell_count)
        ),
    }


def _static_structure(workbook: ParsedWorkbook) -> dict[str, int]:
    example_sheets = [
        sheet for sheet in workbook.sheets if _is_example_sheet(sheet.name)
    ]
    return {
        "sheet_count": len(workbook.sheets),
        "formal_sheet_count": len(workbook.sheets) - len(example_sheets),
        "example_sheet_count": len(example_sheets),
        "merged_range_count": sum(
            len(getattr(sheet, "merged_ranges", []) or []) for sheet in workbook.sheets
        ),
        "embedded_object_count": int(
            getattr(workbook, "embedded_object_count", 0) or 0
        ),
        "media_object_count": int(getattr(workbook, "media_object_count", 0) or 0),
        "drawing_object_count": int(getattr(workbook, "drawing_object_count", 0) or 0),
    }


def _relative_order_score(
    reference: Sequence[str],
    generated: Sequence[str],
) -> tuple[int, int]:
    if len(reference) < 2:
        return 1, 1
    generated_positions = {key: index for index, key in enumerate(generated)}
    numerator = 0
    denominator = 0
    for left_index, left in enumerate(reference):
        for right in reference[left_index + 1 :]:
            denominator += 1
            if generated_positions.get(left, -1) < generated_positions.get(right, -1):
                numerator += 1
    return numerator, denominator


def _cross_field_consistency(
    reference_by_key: Mapping[str, dict[str, Any]],
    generated_by_key: Mapping[str, dict[str, Any]],
    shared_keys: Sequence[str],
) -> dict[str, Any]:
    conflicts: list[dict[str, str]] = []
    numerator = 0
    denominator = 0
    for key in shared_keys:
        reference_row = reference_by_key[key]
        generated_row = generated_by_key[key]
        for field in ("pin_definition", "function", "notice"):
            reference_value = str(reference_row.get(field) or "")
            generated_value = str(generated_row.get(field) or "")
            if not reference_value:
                continue
            denominator += 1
            if reference_value == generated_value:
                numerator += 1
            else:
                conflicts.append(
                    {
                        "key": key,
                        "field": field,
                        "reference_value": reference_value,
                        "generated_value": generated_value,
                    }
                )
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": _rate(numerator, denominator),
        "conflicts": conflicts,
    }


def _parity_metric(
    metric_name: str,
    numerator: int | float,
    denominator: int,
    direction: str,
    *,
    detail: dict[str, Any] | None = None,
    unknown: bool = False,
) -> dict[str, Any]:
    status = "success"
    value: float | None
    if unknown or denominator <= 0:
        value = None
        status = "inconclusive"
    else:
        value = round(float(numerator) / denominator, 6)
    return {
        "metric_name": metric_name,
        "metric_version": PARITY_METRIC_VERSION,
        "value": value,
        "status": status,
        "denominator": denominator,
        "numerator": float(numerator),
        "direction": direction,
        "detail": detail or {},
    }


def _package_bytes(
    content: bytes | bytearray | os.PathLike[str] | str,
) -> bytes:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, (str, os.PathLike)):
        with open(content, "rb") as source:
            return source.read()
    reader = getattr(content, "read", None)
    if callable(reader):
        value = reader()
        if isinstance(value, bytes):
            return value
    raise TypeError("OOXML package must be bytes or a readable path/stream")


def _local_elements(root: ET.Element, name: str):
    return (
        element
        for element in root.iter()
        if str(element.tag).rsplit("}", 1)[-1] == name
    )


def _has_local_element(root: ET.Element, name: str) -> bool:
    return next(_local_elements(root, name), None) is not None


def _find_headers(
    rows: list[list[str]],
) -> list[tuple[int, int | None, int, int, int | None, int | None]]:
    headers: list[tuple[int, int | None, int, int, int | None, int | None]] = []
    for row_index, row in enumerate(rows):
        normalized = [_normalize(value) for value in row]
        pin_column = _first_index(normalized, _PIN_HEADERS)
        definition_column = _first_index(normalized, _DEFINITION_HEADERS)
        if (
            pin_column is None
            or definition_column is None
            or pin_column == definition_column
        ):
            continue
        headers.append(
            (
                row_index,
                _first_index(normalized, _CONNECTOR_HEADERS),
                pin_column,
                definition_column,
                _first_index(normalized, _FUNCTION_HEADERS),
                _first_index(normalized, _NOTICE_HEADERS),
            )
        )
    return headers


def _location_before_header(rows: list[list[str]], header_index: int) -> str:
    """Find the closest location scalar preceding a repeated connector table."""

    for row in reversed(rows[:header_index]):
        normalized = [_normalize(value) for value in row]
        location_label_index = _first_index(normalized, _LOCATION_HEADERS)
        if location_label_index is None:
            continue
        for value in row[location_label_index + 1 :]:
            if str(value).strip():
                return str(value).strip()
    return ""


def _physical_row(sheet: Any, row_index: int) -> int:
    row_indices = getattr(sheet, "row_indices", []) or []
    if row_index < len(row_indices):
        return int(row_indices[row_index])
    return row_index + 1


def _first_index(values: list[str], expected: set[str]) -> int | None:
    for index, value in enumerate(values):
        for candidate in expected:
            if value == candidate:
                return index
            # Templates commonly pair a Chinese label with its English form in
            # one cell (for example, "管脚号 Pin Number").  Accept an embedded
            # multi-word English label or any embedded CJK label, while keeping
            # the short generic "pin" token exact-only so it cannot consume
            # the neighbouring "Pin Definition" column.
            if candidate in value and (not candidate.isascii() or len(candidate) > 3):
                return index
    return None


def _pin_key(connector: str, pin: str) -> str:
    normalized_connector = _normalize(connector)
    normalized_pin = _normalize(pin).removeprefix("&")
    embedded_match = _EMBEDDED_LOCATION_PIN.fullmatch(normalized_pin)
    if embedded_match:
        return (
            f"{embedded_match.group('connector').casefold()}:"
            f"{embedded_match.group('pin').casefold()}"
        )
    return (
        f"{normalized_connector}:{normalized_pin}"
        if normalized_connector
        else normalized_pin
    )


def _cell(row: list[str], index: int | None) -> str:
    return str(row[index]).strip() if index is not None and index < len(row) else ""


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _is_pin_identifier(value: str) -> bool:
    return bool(_PIN_IDENTIFIER.fullmatch(value.strip()))


def _is_example_sheet(name: str) -> bool:
    normalized = _normalize(name)
    return any(token in normalized for token in ("example", "sample", "示例", "样例"))


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _field_quality(
    reference_by_key: dict[str, dict[str, str]],
    generated_by_key: dict[str, dict[str, str]],
    shared_keys: list[str],
    field: str,
) -> dict[str, int | float]:
    reference_nonempty = [
        key for key in shared_keys if reference_by_key[key].get(field, "")
    ]
    covered = [
        key for key in reference_nonempty if generated_by_key[key].get(field, "")
    ]
    exact = [
        key
        for key in reference_nonempty
        if reference_by_key[key].get(field, "") == generated_by_key[key].get(field, "")
    ]
    return {
        "reference_nonempty_count": len(reference_nonempty),
        "generated_nonempty_count": sum(
            bool(generated_by_key[key].get(field, "")) for key in shared_keys
        ),
        "covered_count": len(covered),
        "exact_match_count": len(exact),
        "coverage": _rate(len(covered), len(reference_nonempty)),
        "exact_match_rate": _rate(len(exact), len(reference_nonempty)),
    }
