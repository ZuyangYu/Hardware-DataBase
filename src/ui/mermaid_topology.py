"""Utilities for turning Mermaid flowcharts into topology edges."""

from __future__ import annotations

import re

_EDGE_PATTERNS = (
    re.compile(
        r"^(?P<source>.+?)\s*(?:-->|==>|-\.->)\s*\|(?P<label>.*?)\|\s*(?P<target>.+)$"
    ),
    re.compile(
        r"^(?P<source>.+?)\s*--\s*\|(?P<label>.*?)\|\s*-->\s*(?P<target>.+)$"
    ),
    re.compile(r"^(?P<source>.+?)\s*--\s*(?P<label>.+?)\s*-->\s*(?P<target>.+)$"),
    re.compile(r"^(?P<source>.+?)\s*==\s*(?P<label>.+?)\s*==>\s*(?P<target>.+)$"),
    re.compile(r"^(?P<source>.+?)\s*-\.\s*(?P<label>.+?)\s*\.->\s*(?P<target>.+)$"),
    re.compile(r"^(?P<source>.+?)\s*(?:-->|==>|-\.->)\s*(?P<target>.+)$"),
)

_SKIP_PREFIXES = (
    "graph ",
    "flowchart ",
    "classdef ",
    "class ",
    "subgraph ",
    "direction ",
    "style ",
    "linkstyle ",
    "click ",
)

_SHAPE_OPENERS = "[({<"
_SHAPE_CLOSERS = {"[": "]", "(": ")", "{": "}", "<": ">"}
_WRAPPER_PAIRS = {("[", "]"), ("(", ")"), ("{", "}"), ("<", ">")}


def parse_mermaid_edges(source: str) -> list[dict[str, str]]:
    """Parse directed Mermaid flowchart edges into SVG-friendly edge rows.

    The app receives Mermaid from both deterministic renderers and LLM-written
    answers. Mermaid allows several valid edge spellings, so the parser accepts
    quoted and unquoted node labels, pipe labels, text labels, dotted arrows,
    thick arrows and trailing semicolons.
    """
    edges: list[dict[str, str]] = []
    for statement in _iter_mermaid_statements(source):
        match = _match_edge_statement(statement)
        if not match:
            continue
        source_id, source_label = _parse_endpoint(match.group("source"))
        target_id, target_label = _parse_endpoint(match.group("target"))
        if not source_id or not target_id:
            continue
        edges.append(
            {
                "source": source_id,
                "source_label": source_label or source_id,
                "target": target_id,
                "target_label": target_label or target_id,
                "label": _clean_label(match.groupdict().get("label") or ""),
            }
        )
    return edges


def _iter_mermaid_statements(source: str):
    for raw_line in str(source or "").splitlines():
        line = _strip_mermaid_comment(raw_line).strip()
        if not line:
            continue
        for statement in line.split(";"):
            statement = statement.strip()
            lowered = statement.lower()
            if not statement or lowered == "end" or lowered.startswith(_SKIP_PREFIXES):
                continue
            yield statement


def _match_edge_statement(statement: str):
    for pattern in _EDGE_PATTERNS:
        match = pattern.match(statement)
        if match:
            return match
    return None


def _parse_endpoint(value: str) -> tuple[str, str]:
    text = _strip_class_suffix(str(value or "").strip().rstrip(";").strip())
    if not text:
        return "", ""

    node_id = ""
    label = ""
    shape_index = _first_shape_index(text)
    if shape_index > 0:
        node_id = text[:shape_index].strip()
        label = _extract_shape_label(text[shape_index:].strip())
    elif text[0] in _SHAPE_OPENERS:
        label = _extract_shape_label(text)
        node_id = label
    elif text.startswith('"'):
        node_id, remainder = _read_quoted(text)
        label = _extract_shape_label(remainder.strip())
    else:
        match = re.match(r"(?P<id>\S+)(?P<rest>.*)$", text)
        if match:
            node_id = match.group("id")
            label = _extract_shape_label(match.group("rest").strip())

    node_id = _strip_class_suffix(node_id.strip())
    label = _clean_label(label) or _clean_label(node_id)
    return node_id, label


def _strip_mermaid_comment(value: str) -> str:
    return str(value or "").split("%%", 1)[0]


def _strip_class_suffix(value: str) -> str:
    return re.sub(r"\s*:::[A-Za-z_][A-Za-z0-9_-]*\s*$", "", str(value or "")).strip()


def _first_shape_index(text: str) -> int:
    indexes = [text.find(opener) for opener in _SHAPE_OPENERS if text.find(opener) > 0]
    return min(indexes) if indexes else -1


def _extract_shape_label(value: str) -> str:
    text = str(value or "").strip()
    if not text or text[0] not in _SHAPE_OPENERS:
        return ""
    closer = _SHAPE_CLOSERS[text[0]]
    end = text.rfind(closer)
    if end <= 0:
        return ""
    return _clean_label(text[1:end])


def _read_quoted(text: str) -> tuple[str, str]:
    escaped = False
    for index, char in enumerate(text[1:], start=1):
        if char == "\\" and not escaped:
            escaped = True
            continue
        if char == '"' and not escaped:
            return text[1:index], text[index + 1 :]
        escaped = False
    return text.strip('"'), ""


def _clean_label(value: str) -> str:
    text = str(value or "").strip()
    while len(text) >= 2 and (text[0], text[-1]) in _WRAPPER_PAIRS:
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text.strip("/\\")
