"""Validation of generated ICD pin tables against a frozen circuit scope."""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from src.document_authoring.icd_comparison import _extract_pin_rows, _pin_key
from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx


_DUPLICATE_KEY = re.compile(r"管脚键重复，已忽略后续记录：(?P<key>.+)。")


def validate_icd_pin_set(
    expected_mappings: list[dict[str, Any]],
    artifact_content: bytes,
    target_format: str,
) -> list[dict]:
    """Return blocking discrepancies between frozen EDF pins and an ICD workbook."""
    if target_format.casefold() not in {"xlsx", "xlsm"}:
        return []
    generated_rows, warnings = _generated_pin_rows(artifact_content, target_format)
    generated_by_key = {row["key"]: row for row in generated_rows}
    duplicate_keys = {
        match.group("key")
        for warning in warnings
        if (match := _DUPLICATE_KEY.fullmatch(warning))
    }
    issues: list[dict] = []
    for expected in _expected_pin_mappings(expected_mappings):
        key = expected["key"]
        generated = generated_by_key.get(key)
        if generated is None:
            issues.append({
                "code": "icd_pin_missing",
                "severity": "blocking",
                "key": key,
            })
            continue
        if key in duplicate_keys:
            issues.append({
                "code": "icd_pin_duplicate",
                "severity": "blocking",
                "key": key,
            })
        if generated["definition"] != expected["net_name"]:
            issues.append({
                "code": "icd_pin_net_mismatch",
                "severity": "blocking",
                "key": key,
            })
    return issues


def _generated_pin_rows(
    artifact_content: bytes,
    target_format: str,
) -> tuple[list[dict[str, str]], list[str]]:
    suffix = ".xlsm" if target_format.casefold() == "xlsm" else ".xlsx"
    descriptor, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(artifact_content)
        return _extract_pin_rows(parse_xlsx(path))
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _expected_pin_mappings(
    mappings: list[dict[str, Any]],
) -> list[dict[str, str]]:
    expected: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for mapping in mappings:
        refdes = str(mapping.get("refdes") or "").strip()
        pin_name = str(mapping.get("pin_name") or "").strip()
        if not (refdes and pin_name):
            continue
        key = _pin_key(refdes, pin_name)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        expected.append({
            "key": key,
            "net_name": " ".join(
                str(mapping.get("net_name") or "NC").strip().casefold().split()
            ),
        })
    return expected
