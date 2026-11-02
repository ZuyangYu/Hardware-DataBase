"""Deterministic BOM checks used by the example Skill.

This module intentionally has no network, filesystem-write or process-control
side effects.  The entrypoint owns the bounded `/work/in` and `/work/out`
contract; these helpers only normalize rows and produce review issues.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Iterable


def load_rows_from_text(value: str) -> list[dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("bom_rows"), list):
        parsed = parsed["bom_rows"]
    if isinstance(parsed, list):
        return [dict(item) for item in parsed if isinstance(item, dict)]
    reader = csv.DictReader(io.StringIO(text))
    return [dict(item) for item in reader]


def find_input_rows(assets_root: Path) -> tuple[list[dict[str, Any]], str]:
    """Read the first supported input asset without following symlinks."""

    if not assets_root.is_dir():
        return [], ""
    candidates: list[Path] = []
    for path in assets_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in {".json", ".csv"}:
            continue
        candidates.append(path)
    for path in sorted(candidates):
        try:
            rows = load_rows_from_text(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if rows:
            return rows, path.name
    return [], ""


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def audit_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(normalized_rows, issues)`` with stable issue codes."""

    normalized: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            issues.append({"code": "row_not_object", "message": "BOM 行必须是对象", "row_index": index})
            continue
        reference = str(raw.get("reference") or raw.get("designator") or "").strip()
        part_number = str(raw.get("part_number") or raw.get("mpn") or "").strip()
        quantity_value = raw.get("quantity", raw.get("qty", 1))
        quantity = _number(quantity_value)
        voltage = _number(raw.get("voltage_v"))
        minimum = _number(raw.get("min_voltage_v"))
        maximum = _number(raw.get("max_voltage_v"))

        if not reference:
            issues.append({"code": "missing_reference", "message": "缺少位号", "row_index": index})
        elif reference in seen:
            issues.append({
                "code": "duplicate_reference",
                "message": f"位号重复：{reference}",
                "row_index": index,
                "reference": reference,
            })
        else:
            seen.add(reference)
        if not part_number:
            issues.append({
                "code": "missing_part_number",
                "message": "缺少器件型号",
                "row_index": index,
                "reference": reference,
            })
        if quantity is None or quantity <= 0 or not float(quantity).is_integer():
            issues.append({
                "code": "invalid_quantity",
                "message": "数量必须是正整数",
                "row_index": index,
                "reference": reference,
            })
        if voltage is not None and minimum is not None and voltage < minimum:
            issues.append({
                "code": "voltage_below_minimum",
                "message": f"工作电压 {voltage}V 低于下限 {minimum}V",
                "row_index": index,
                "reference": reference,
            })
        if voltage is not None and maximum is not None and voltage > maximum:
            issues.append({
                "code": "voltage_above_maximum",
                "message": f"工作电压 {voltage}V 高于上限 {maximum}V",
                "row_index": index,
                "reference": reference,
            })
        normalized.append({
            "reference": reference,
            "part_number": part_number,
            "quantity": int(quantity) if quantity is not None and float(quantity).is_integer() else quantity,
            "voltage_v": voltage,
            "min_voltage_v": minimum,
            "max_voltage_v": maximum,
        })
    return normalized, issues


def build_summary(rows: list[dict[str, Any]], issues: list[dict[str, Any]]) -> str:
    references = {str(item.get("reference") or "").strip() for item in rows}
    references.discard("")
    lines = [
        "# BOM 合规审查摘要",
        "",
        f"- BOM 行数：{len(rows)}",
        f"- 唯一位号数：{len(references)}",
        f"- 校验问题数：{len(issues)}",
        "",
    ]
    if issues:
        lines.append("## 需要处理的问题")
        lines.append("")
        for issue in issues[:20]:
            lines.append(f"- `{issue.get('code', 'unknown')}`：{issue.get('message', '')}")
    else:
        lines.append("未发现结构性问题。该结果不替代硬件工程师的电气设计评审。")
    lines.append("")
    return "\n".join(lines)
