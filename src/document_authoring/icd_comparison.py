"""Generic, repeatable comparison of ICD connector pin tables."""

from __future__ import annotations

import re
from typing import Any

from src.pipelines.spreadsheet.xlsx_parser import ParsedWorkbook


_PIN_HEADERS = {
    "pin", "pin no", "pin no.", "pin number", "pin #", "pin num",
    "引脚号", "管脚号", "针脚号", "管脚编号", "引脚编号",
}
_DEFINITION_HEADERS = {
    "pin definition", "signal", "signal definition", "definition",
    "信号", "信号定义", "管脚定义", "引脚定义", "定义",
}
_FUNCTION_HEADERS = {
    "function", "function description", "功能", "功能描述", "功能定义",
}
_NOTICE_HEADERS = {
    "notice", "remark", "remarks", "note", "备注", "说明",
}
_CONNECTOR_HEADERS = {
    "connector", "connector id", "connector name", "connector number",
    "接插件", "连接器", "连接器编号", "接插件编号",
}
_LOCATION_HEADERS = {
    "location", "location number", "connector location",
    "控制器上编号", "位置编号", "安装位置", "接插件位置",
}
_PIN_IDENTIFIER = re.compile(r"^&?[a-z]*\d+[a-z0-9_.-]*$", re.IGNORECASE)
_EMBEDDED_LOCATION_PIN = re.compile(
    r"^(?P<connector>[a-z]+\d+)[\-_/](?P<pin>[a-z0-9_.]+)$",
    re.IGNORECASE,
)


def compare_workbooks(reference: ParsedWorkbook, generated: ParsedWorkbook) -> dict[str, Any]:
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
    return {
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
                reference_by_key, generated_by_key, shared_keys, "function",
            ),
            "notice": _field_quality(
                reference_by_key, generated_by_key, shared_keys, "notice",
            ),
        },
        "warnings": warnings,
    }


def _extract_pin_rows(workbook: ParsedWorkbook) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    seen_keys: set[str] = set()
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
            end_index = headers[header_number + 1][0] if header_number + 1 < len(headers) else len(sheet.rows)
            location = _location_before_header(sheet.rows, header_index)
            for row in sheet.rows[header_index + 1:end_index]:
                pin = _cell(row, pin_column)
                if not _is_pin_identifier(pin):
                    continue
                connector = _cell(row, connector_column) if connector_column is not None else location
                key = _pin_key(connector, pin)
                if key in seen_keys:
                    warnings.append(f"管脚键重复，已忽略后续记录：{key}。")
                    continue
                seen_keys.add(key)
                rows.append({
                    "key": key,
                    "definition": _normalize(_cell(row, definition_column)),
                    "function": _normalize(_cell(row, function_column)),
                    "notice": _normalize(_cell(row, notice_column)),
                    "sheet": _normalize(sheet.name),
                })
    return rows, warnings


def _find_headers(
    rows: list[list[str]],
) -> list[tuple[int, int | None, int, int, int | None, int | None]]:
    headers: list[tuple[int, int | None, int, int, int | None, int | None]] = []
    for row_index, row in enumerate(rows):
        normalized = [_normalize(value) for value in row]
        pin_column = _first_index(normalized, _PIN_HEADERS)
        definition_column = _first_index(normalized, _DEFINITION_HEADERS)
        if pin_column is None or definition_column is None or pin_column == definition_column:
            continue
        headers.append((
            row_index,
            _first_index(normalized, _CONNECTOR_HEADERS),
            pin_column,
            definition_column,
            _first_index(normalized, _FUNCTION_HEADERS),
            _first_index(normalized, _NOTICE_HEADERS),
        ))
    return headers


def _location_before_header(rows: list[list[str]], header_index: int) -> str:
    """Find the closest location scalar preceding a repeated connector table."""

    for row in reversed(rows[:header_index]):
        normalized = [_normalize(value) for value in row]
        location_label_index = _first_index(normalized, _LOCATION_HEADERS)
        if location_label_index is None:
            continue
        for value in row[location_label_index + 1:]:
            if str(value).strip():
                return str(value).strip()
    return ""


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
            if candidate in value and (
                not candidate.isascii() or len(candidate) > 3
            ):
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
    return f"{normalized_connector}:{normalized_pin}" if normalized_connector else normalized_pin


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
        key for key in reference_nonempty
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
