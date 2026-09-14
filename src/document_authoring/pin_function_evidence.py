"""Evidence-grounded pin function extraction for ICD connector tables.

The frozen EDF only defines connector identity and net names.  A function
description must come from the document sources and must be tied to the same
connector identity, so a pin table describing another product can never leak
into the deliverable.  Net-only matches are rejected on purpose: the query is
scoped per connector, but a wrong-product table can still share a net name.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable


_FUNCTION_HEADER_RE = re.compile(r"功能|function", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SENTENCE_SPLIT_RE = re.compile(r"[。；;\n]")
_UNCONNECTED_NETS = {"nc", "n/c", "no_connect", "no connect"}
_MAX_FUNCTION_CHARS = 120


def pin_function_queries(refdes: str) -> list[str]:
    """Return the bounded second-stage retrieval queries for one connector."""

    normalized = str(refdes or "").strip()
    if not normalized:
        return []
    return [f"{normalized} 管脚 定义 功能 描述"]


def extract_pin_function_candidates(
    content: str,
    mappings: Iterable[dict[str, Any]],
) -> dict[str, str]:
    """Return ``row_key -> function text`` anchored in one evidence chunk.

    Only text that sits in the same table row (or sentence) as the connector
    identity or its net is eligible, and the candidate is a literal substring
    of the evidence so downstream lexical-anchor validation holds.
    """

    text = str(content or "")
    items = [item for item in mappings if isinstance(item, dict)]
    if not text or not items:
        return {}
    found = _extract_from_table(text, items)
    remaining = [
        item
        for item in items
        if f"{item.get('refdes')}-{item.get('pin_name')}" not in found
    ]
    if remaining:
        found.update(_extract_from_text(text, remaining))
    return found


def extract_spreadsheet_pin_function_candidates(
    values: dict[str, Any],
    raw_text: str,
    mappings: Iterable[dict[str, Any]],
) -> dict[str, str]:
    """Extract per-pin functions from one structured spreadsheet row.

    Test/definition sheets carry the tested signal in a ``负载``/``Load`` cell
    and contextual detail in adjacent cells.  Only the row's own cell text is
    used; test-configuration parentheses are stripped so the filled value reads
    as a connector function instead of a test setup.
    """

    value_map = {
        str(key).strip(): str(value).strip()
        for key, value in (values or {}).items()
    }
    text = str(raw_text or "")
    items = [item for item in mappings if isinstance(item, dict)]
    if not items or not (value_map or text):
        return {}
    load = next(
        (
            value for key, value in value_map.items()
            if any(token in key.casefold() for token in ("负载", "load", "测试项", "项目"))
            and value
        ),
        "",
    )
    descriptive = _first_descriptive_cell(value_map, exclude={load})
    found: dict[str, str] = {}
    for item in items:
        refdes = str(item.get("refdes") or "").strip()
        pin = str(item.get("pin_name") or "").strip()
        net = str(item.get("net_name") or "").strip()
        if not (refdes and pin):
            continue
        identity_tokens = (
            _token_pattern(f"{refdes}-{pin}"),
            _token_pattern(f"{refdes}.&{pin}"),
        )
        row_blob = " ".join([text, *value_map.values()])
        if not any(token.search(row_blob) for token in identity_tokens):
            continue
        row_key = f"{refdes}-{pin}"
        candidate = _clean_load(load) or descriptive
        described = _describe(candidate, pin=pin, net=net, refdes=refdes)
        if described:
            found[row_key] = described
    return found


def compose_function_evidence_text(
    hits: dict[str, str],
    raw_text: str,
) -> str:
    """Build synthetic evidence that literally contains every function text."""

    parts = [f"{key} {value}" for key, value in hits.items() if key and value]
    if raw_text:
        parts.append(str(raw_text))
    return " | ".join(parts)


def _extract_from_table(content: str, mappings: list[dict[str, Any]]) -> dict[str, str]:
    rows = _table_rows(content)
    if not rows:
        return {}
    function_column: int | None = None
    for row in rows[:3]:
        for index, cell in enumerate(row):
            if _FUNCTION_HEADER_RE.search(cell):
                function_column = index
                break
        if function_column is not None:
            break
    content_has_refdes: dict[str, bool] = {}
    found: dict[str, str] = {}
    for mapping in mappings:
        refdes = str(mapping.get("refdes") or "").strip()
        pin = str(mapping.get("pin_name") or "").strip()
        net = str(mapping.get("net_name") or "").strip()
        if not (refdes and pin):
            continue
        row_key = f"{refdes}-{pin}"
        has_refdes = content_has_refdes.setdefault(
            refdes, bool(_token_pattern(refdes).search(content)),
        )
        pin_pattern = _token_pattern(pin)
        net_pattern = (
            _token_pattern(net)
            if net and net.casefold() not in _UNCONNECTED_NETS
            else None
        )
        for row in rows:
            row_text = " ".join(row)
            has_identity = bool(
                _token_pattern(f"{refdes}-{pin}").search(row_text)
                or _token_pattern(f"{refdes}.&{pin}").search(row_text)
            )
            has_pin = bool(pin_pattern.search(row_text))
            has_net = bool(net_pattern and net_pattern.search(row_text))
            if not has_identity and not (
                has_refdes and has_pin and (has_net or function_column is not None)
            ):
                continue
            candidate = ""
            if function_column is not None and function_column < len(row):
                candidate = row[function_column]
            else:
                for index, cell in enumerate(row):
                    if (
                        pin_pattern.search(cell)
                        or f"{refdes}-{pin}".casefold() in cell.casefold()
                        or (net_pattern and net_pattern.search(cell))
                    ):
                        if index + 1 < len(row):
                            candidate = row[index + 1]
                        break
            described = _describe(candidate, pin=pin, net=net, refdes=refdes)
            if described:
                found[row_key] = described
                break
    return found


def _extract_from_text(content: str, mappings: list[dict[str, Any]]) -> dict[str, str]:
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(content)
        if sentence.strip()
    ]
    if not sentences:
        return {}
    found: dict[str, str] = {}
    for mapping in mappings:
        refdes = str(mapping.get("refdes") or "").strip()
        pin = str(mapping.get("pin_name") or "").strip()
        net = str(mapping.get("net_name") or "").strip()
        if not (refdes and pin):
            continue
        row_key = f"{refdes}-{pin}"
        identity = [
            _token_pattern(f"{refdes}-{pin}"),
            _token_pattern(f"{refdes}.&{pin}"),
        ]
        net_pattern = (
            _token_pattern(net)
            if net and net.casefold() not in _UNCONNECTED_NETS
            else None
        )
        for sentence in sentences:
            has_identity = any(pattern.search(sentence) for pattern in identity)
            has_context = bool(
                has_identity
                or (
                    _token_pattern(refdes).search(sentence)
                    and net_pattern
                    and net_pattern.search(sentence)
                )
            )
            if not has_context:
                continue
            described = _describe(sentence, pin=pin, net=net, refdes=refdes)
            if described:
                found[row_key] = described
                break
    return found


def _table_rows(content: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw_row in _ROW_RE.findall(content):
        cells = [_clean_cell(cell) for cell in _CELL_RE.findall(raw_row)]
        if cells:
            rows.append(cells)
    return rows


def _clean_cell(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _token_pattern(token: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![A-Za-z0-9_.])" + re.escape(token) + r"(?![A-Za-z0-9_.])",
        re.IGNORECASE,
    )


def _clean_load(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.upper() in {"NA", "N/A", "-", "/"}:
        return ""
    text = re.sub(r"^\d+[.、]\s*", "", text)
    text = re.sub(r"[（(][^）)]*[）)]", "", text).strip()
    return text


def _first_descriptive_cell(value_map: dict[str, str], *, exclude: set[str]) -> str:
    preferred = ("描述", "说明", "功能", "function", "remark", "备注", "测试步骤", "测试目的")
    for key, value in value_map.items():
        if value in exclude or not value:
            continue
        if any(token in key.casefold() for token in preferred):
            return value
    return ""


def _describe(candidate: str, *, pin: str, net: str, refdes: str) -> str:
    text = str(candidate or "").strip()
    if not text:
        return ""
    normalized = text.casefold()
    if normalized in {
        pin.casefold(), net.casefold(), refdes.casefold(), f"{refdes}-{pin}".casefold(),
    }:
        return ""
    if len(text) > _MAX_FUNCTION_CHARS:
        text = text[:_MAX_FUNCTION_CHARS].rstrip()
    if (
        not _CJK_RE.search(text)
        and len(text.split()) < 2
        and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-/]{1,39}", text)
    ):
        return ""
    return text
