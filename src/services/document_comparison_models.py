"""Stable data model for whole-document comparison.

The report is intentionally independent of the agent prompt format.  It is
serializable, immutable by content hash, and can be persisted/exported without
re-reading a model answer.  Every comparison item keeps both source identities
and locators even when only one side changed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

COMPARISON_SCHEMA_VERSION = "document-comparison-v1"

ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"
MOVED = "moved"
TABLE_ADDED = "table_added"
TABLE_REMOVED = "table_removed"
TABLE_MODIFIED = "table_modified"
UNCHANGED = "unchanged"
UNCERTAIN = "uncertain"

ITEM_KINDS = (
    ADDED, REMOVED, MODIFIED, MOVED,
    TABLE_ADDED, TABLE_REMOVED, TABLE_MODIFIED, UNCHANGED, UNCERTAIN,
)


@dataclass(frozen=True)
class CanonicalTable:
    table_id: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    locator: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CanonicalSection:
    section_id: str
    heading: str
    level: int
    path: tuple[str, ...]
    order: int
    content: str
    tables: tuple[CanonicalTable, ...] = ()
    locator: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "tables": [table.to_dict() for table in self.tables],
        }


@dataclass(frozen=True)
class ComparisonSourceSnapshot:
    source_type: str
    source_id: str
    source_name: str
    document_version: str = ""
    content_hash: str = ""
    parser_version: str = ""
    frozen_at: str = ""
    owner: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    status: str = "complete"
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CanonicalDocument:
    snapshot: ComparisonSourceSnapshot
    sections: tuple[CanonicalSection, ...]
    normalized_hash: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "sections": [section.to_dict() for section in self.sections],
            "normalized_hash": self.normalized_hash,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class AlignedSection:
    alignment_id: str
    left_section_id: str
    right_section_id: str
    method: str
    section_key: str
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComparisonItem:
    item_id: str
    kind: str
    section_key: str
    left_section_id: str = ""
    right_section_id: str = ""
    left_ref: str = ""
    right_ref: str = ""
    left_locator: dict[str, Any] = field(default_factory=dict)
    right_locator: dict[str, Any] = field(default_factory=dict)
    left_text: str = ""
    right_text: str = ""
    summary: str = ""
    semantic_judgement: str = "exact"
    confidence: float = 1.0
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComparisonReport:
    report_id: str
    created_at: str
    left: ComparisonSourceSnapshot
    right: ComparisonSourceSnapshot
    alignments: tuple[AlignedSection, ...] = ()
    items: tuple[ComparisonItem, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    status: str = "complete"
    warnings: tuple[str, ...] = ()
    immutable_hash: str = ""
    schema_version: str = COMPARISON_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "created_at": self.created_at,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "alignments": [item.to_dict() for item in self.alignments],
            "items": [item.to_dict() for item in self.items],
            "counts": dict(self.counts),
            "status": self.status,
            "warnings": list(self.warnings),
            "immutable_hash": self.immutable_hash,
        }

    def changed_items(self) -> list[ComparisonItem]:
        return [item for item in self.items if item.kind != UNCHANGED]

    def summary_text(self) -> str:
        changed = self.changed_items()
        if not changed:
            return "两份来源在可解析范围内未发现差异。"
        rows = [
            f"{item.kind}: {item.summary}"
            for item in changed[:20]
        ]
        more = f"\n另有 {len(changed) - 20} 项变化未展开。" if len(changed) > 20 else ""
        return f"共发现 {len(changed)} 项变化。\n" + "\n".join(rows) + more


def report_hash(payload: dict[str, Any]) -> str:
    """Stable semantic hash.

    ``created_at`` is intentionally excluded: re-running the same frozen
    sources must be idempotent, not produce a new hash and a false
    ``comparison_report_conflict``.  The source hashes, versions, alignment and
    items remain covered.
    """

    body = {
        key: value
        for key, value in payload.items()
        if key not in {"immutable_hash", "created_at"}
    }
    serialized = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def comparison_report_from_dict(payload: dict[str, Any]) -> ComparisonReport:
    """Rehydrate a stored report without trusting derived defaults."""

    return ComparisonReport(
        report_id=str(payload["report_id"]),
        created_at=str(payload["created_at"]),
        left=ComparisonSourceSnapshot(**payload["left"]),
        right=ComparisonSourceSnapshot(**payload["right"]),
        alignments=tuple(AlignedSection(**item) for item in payload.get("alignments", ())),
        items=tuple(ComparisonItem(**item) for item in payload.get("items", ())),
        counts=dict(payload.get("counts") or {}),
        status=str(payload.get("status") or "complete"),
        warnings=tuple(payload.get("warnings") or ()),
        immutable_hash=str(payload.get("immutable_hash") or ""),
        schema_version=str(payload.get("schema_version") or COMPARISON_SCHEMA_VERSION),
    )


def legacy_report_hash(payload: dict[str, Any]) -> str:
    """Hash used by reports written before created_at was excluded."""

    body = {key: value for key, value in payload.items() if key != "immutable_hash"}
    serialized = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "ADDED",
    "AlignedSection",
    "COMPARISON_SCHEMA_VERSION",
    "CanonicalDocument",
    "CanonicalSection",
    "CanonicalTable",
    "ComparisonItem",
    "ComparisonReport",
    "ComparisonSourceSnapshot",
    "ITEM_KINDS",
    "MODIFIED",
    "MOVED",
    "REMOVED",
    "TABLE_ADDED",
    "TABLE_MODIFIED",
    "TABLE_REMOVED",
    "UNCHANGED",
    "UNCERTAIN",
    "comparison_report_from_dict",
    "legacy_report_hash",
    "report_hash",
]
