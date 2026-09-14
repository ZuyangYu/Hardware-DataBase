"""Domain-agnostic pending-decision grouping and execution policy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DecisionPolicyMode(str, Enum):
    CONTINUE_WITH_DEFAULTS = "continue_with_defaults"
    ASK_BEFORE_WRITE = "ask_before_write"
    BLOCK = "block"


class PendingDecisionItem(BaseModel):
    """One auditable member of an otherwise grouped pending decision."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    summary: str = ""
    refs: dict[str, Any] = Field(default_factory=dict)


class PendingDecision(BaseModel):
    """One group presented to a user while retaining its member audit trail."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    group_key: str
    kind: str
    title: str
    options: list[str] = Field(default_factory=list)
    default_option: str = ""
    count: int = 1
    blocking: bool = False
    items: list[PendingDecisionItem] = Field(default_factory=list)


def group_pending_decisions(records: Iterable[Mapping[str, Any]]) -> list[PendingDecision]:
    """Group generic records by ``(kind, group_key)`` and deduplicate items."""

    grouped: dict[tuple[str, str], PendingDecision] = {}
    seen_items: set[tuple[str, str, str]] = set()
    for record in records:
        kind = str(record.get("kind") or "").strip()
        group_key = str(record.get("group_key") or "").strip()
        item_id = str(record.get("item_id") or "").strip()
        if not kind or not group_key:
            continue
        key = (kind, group_key)
        decision = grouped.get(key)
        if decision is None:
            decision = PendingDecision(
                decision_id=str(record.get("decision_id") or f"decision:{kind}:{group_key}"),
                group_key=group_key,
                kind=kind,
                title=str(record.get("title") or group_key),
                options=[
                    str(option).strip()
                    for option in record.get("options") or []
                    if str(option).strip()
                ],
                default_option=str(record.get("default_option") or "").strip(),
                blocking=bool(record.get("blocking")),
            )
            grouped[key] = decision
        else:
            # If a re-projection discovers a blocking member, the aggregate
            # must become blocking even if its first item was not.
            decision.blocking = decision.blocking or bool(record.get("blocking"))
        item_key = (kind, group_key, item_id)
        if not item_id or item_key in seen_items:
            continue
        seen_items.add(item_key)
        decision.items.append(PendingDecisionItem(
            item_id=item_id,
            summary=str(record.get("summary") or ""),
            refs=dict(record.get("refs") or {}),
        ))
    result = list(grouped.values())
    for decision in result:
        decision.count = max(1, len(decision.items))
    return sorted(result, key=lambda item: (not item.blocking, item.kind, item.group_key))


def decisions_from_grouped_items(
    items: Iterable[Mapping[str, Any]],
    *,
    kind: str,
    group_field: str,
    title: str,
    options: list[str],
    default_option: str,
    blocking_field: str = "blocking",
    summary_field: str = "user_instruction",
) -> list[PendingDecision]:
    """Turn arbitrary domain items into generic grouped pending decisions."""

    records: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        group_value = str(item.get(group_field) or "").strip() or f"item-{index + 1}"
        records.append({
            "decision_id": f"decision:{kind}:{group_value}",
            "kind": kind,
            "group_key": group_value,
            "title": title.format(group=group_value),
            "options": list(options),
            "default_option": default_option,
            "blocking": bool(item.get(blocking_field)),
            "item_id": str(item.get("exception_id") or item.get("item_id") or f"{group_value}:{index + 1}"),
            "summary": str(item.get(summary_field) or ""),
            "refs": {group_field: group_value},
        })
    return group_pending_decisions(records)


def resolve_decision_policy(
    *,
    document_type: str = "",
    kind: str = "",
    overrides: Mapping[str, str] | None = None,
) -> DecisionPolicyMode:
    """Resolve a narrow, explicit policy override or use safe default flow."""

    table = dict(overrides or {})
    for key in (str(kind).strip(), str(document_type).strip()):
        if not key:
            continue
        raw = str(table.get(key) or "").strip()
        if not raw:
            continue
        try:
            return DecisionPolicyMode(raw)
        except ValueError:
            continue
    return DecisionPolicyMode.CONTINUE_WITH_DEFAULTS


__all__ = [
    "DecisionPolicyMode",
    "PendingDecision",
    "PendingDecisionItem",
    "decisions_from_grouped_items",
    "group_pending_decisions",
    "resolve_decision_policy",
]
