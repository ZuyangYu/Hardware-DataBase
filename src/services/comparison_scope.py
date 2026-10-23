"""Map a user-facing DeliveryScope onto a frozen comparison report.

The comparison report is the source of truth for item identity and section
keys.  This module deliberately performs exact/prefix matching; it never
re-runs the comparison or relies on the model's ``focus`` string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from src.services.document_comparison_models import ComparisonItem, ComparisonReport, UNCHANGED
from src.services.task_planning import DeliveryScope

_NUMERIC_SECTION_RE = re.compile(r"^num:(\d+(?:\.\d+)*)")
_TABLE_SUFFIX_RE = re.compile(r":table-(\d+)$")


def chapter_key_for_item(item: ComparisonItem) -> str:
    base = _TABLE_SUFFIX_RE.sub("", str(item.section_key or ""))
    match = _NUMERIC_SECTION_RE.match(base)
    if match:
        number = match.group(1)
        return f"num:{number.split('.')[0]}"
    return base or "unknown"


def table_key_for_item(item: ComparisonItem) -> str:
    match = _TABLE_SUFFIX_RE.search(str(item.section_key or ""))
    return f"table-{match.group(1)}" if match else ""


def normalize_section_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("num:", "title:", "key:", "content:")):
        return text
    chapter = re.fullmatch(r"第?\s*(\d+)\s*(?:章|部分|篇)?", text)
    if chapter:
        return f"num:{chapter.group(1)}"
    section = re.fullmatch(r"第?\s*(\d+(?:\.\d+)+)\s*(?:节|小节)?", text)
    if section:
        return f"num:{section.group(1)}"
    return text


def chapter_groups(report: ComparisonReport) -> dict[str, tuple[ComparisonItem, ...]]:
    groups: dict[str, list[ComparisonItem]] = {}
    for item in report.items:
        groups.setdefault(chapter_key_for_item(item), []).append(item)
    return {key: tuple(value) for key, value in groups.items()}


def _matches_section(item: ComparisonItem, section_keys: Iterable[str]) -> bool:
    item_key = _TABLE_SUFFIX_RE.sub("", str(item.section_key or ""))
    for raw in section_keys:
        key = normalize_section_key(raw)
        if not key:
            continue
        if item_key == key:
            return True
        if item_key.startswith(key + "."):
            return True
    return False


def _matches_chapter(item: ComparisonItem, chapter_keys: Iterable[str]) -> bool:
    item_chapter = chapter_key_for_item(item)
    item_key = _TABLE_SUFFIX_RE.sub("", str(item.section_key or ""))
    for raw in chapter_keys:
        key = normalize_section_key(raw)
        if not key:
            continue
        if item_chapter == key or item_key == key:
            return True
        if item_key.startswith(key + "."):
            return True
    return False


def select_scope_items(report: ComparisonReport, scope: DeliveryScope) -> tuple[ComparisonItem, ...]:
    """Return changed items in the requested scope, preserving report order."""

    changed = report.changed_items()
    if scope.scope_kind == "item_detail":
        selected = changed
        if scope.item_ids:
            wanted = set(scope.item_ids)
            selected = [item for item in selected if item.item_id in wanted]
        if scope.table_keys:
            wanted_tables = {
                normalize_section_key(value).removeprefix("table-")
                for value in scope.table_keys
            }
            selected = [
                item for item in selected
                if table_key_for_item(item).removeprefix("table-") in wanted_tables
            ]
        if scope.section_keys:
            selected = [item for item in selected if _matches_section(item, scope.section_keys)]
        return tuple(selected)
    if scope.scope_kind in {"chapter_detail", "chapter_summary"}:
        selected = changed
        if scope.chapter_keys:
            selected = [item for item in selected if _matches_chapter(item, scope.chapter_keys)]
        if scope.section_keys:
            selected = [item for item in selected if _matches_section(item, scope.section_keys)]
        return tuple(selected)
    return tuple(changed)


def scope_chapter_groups(
    report: ComparisonReport,
    scope: DeliveryScope,
) -> dict[str, tuple[ComparisonItem, ...]]:
    groups = chapter_groups(report)
    if not scope.chapter_keys:
        return groups
    return {
        key: value
        for key, value in groups.items()
        if any(
            key == normalize_section_key(raw)
            or key.startswith(normalize_section_key(raw) + ".")
            for raw in scope.chapter_keys
        )
    }


def scope_required_item_ids(report: ComparisonReport, scope: DeliveryScope) -> tuple[str, ...]:
    return tuple(item.item_id for item in select_scope_items(report, scope))


def scope_chapter_item_ids(
    report: ComparisonReport,
    scope: DeliveryScope,
) -> dict[str, tuple[str, ...]]:
    groups = scope_chapter_groups(report, scope)
    return {
        key: tuple(item.item_id for item in items if item.kind != UNCHANGED)
        for key, items in groups.items()
    }


def estimate_item_bytes(item: ComparisonItem) -> int:
    inline_diff = str(item.metadata.get("inline_diff") or "")
    return max(
        300,
        len(str(item.summary or "").encode("utf-8"))
        + len(str(item.left_text or "")[:1200].encode("utf-8"))
        + len(str(item.right_text or "")[:1200].encode("utf-8"))
        + len(inline_diff[:1800].encode("utf-8"))
        + 300,
    )


@dataclass(frozen=True)
class ScopeSelection:
    scope_key: str
    items: tuple[ComparisonItem, ...]
    chapters: dict[str, tuple[ComparisonItem, ...]]
    required_item_ids: tuple[str, ...]
    estimated_bytes: int
    report_status: str
    warnings: tuple[str, ...]


def resolve_scope(report: ComparisonReport, scope: DeliveryScope) -> ScopeSelection:
    items = select_scope_items(report, scope)
    chapters = scope_chapter_groups(report, scope)
    return ScopeSelection(
        scope_key=scope.scope_key,
        items=items,
        chapters=chapters,
        required_item_ids=tuple(item.item_id for item in items),
        estimated_bytes=sum(estimate_item_bytes(item) for item in items),
        report_status=str(report.status or ""),
        warnings=tuple(report.warnings or ()),
    )


__all__ = [
    "ScopeSelection",
    "chapter_groups",
    "chapter_key_for_item",
    "estimate_item_bytes",
    "normalize_section_key",
    "resolve_scope",
    "scope_chapter_groups",
    "scope_chapter_item_ids",
    "scope_required_item_ids",
    "select_scope_items",
    "table_key_for_item",
]
