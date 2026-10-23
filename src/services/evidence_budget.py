"""Source-aware formal-evidence budget coordination.

The coordinator owns the single request-level accounting point for admitted
evidence.  Retrieval adapters only produce candidates; they must ask this
object before a candidate becomes formal evidence.  Source slices never borrow
another source's promised reserve and the global slice never exceeds its hard
cap.

Units in this module are explicit: ``*_bytes`` values are UTF-8 bytes of the
rendered evidence block (plus the small citation margin supplied by the
runtime).  They are never labelled as provider tokens.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.services.evidence_sources import (
    ATTACHMENT,
    COMPARISON,
    KNOWLEDGE_BASE,
    KNOWLEDGE_BASE_DOCUMENT,
    KNOWLEDGE_BASE_STRUCTURED,
    KNOWLEDGE_SOURCE_TYPES,
    normalize_source_type,
    source_type_for_metadata,
)

SOURCE_BUDGET_EXHAUSTED = "source_budget_exhausted"
GLOBAL_BUDGET_EXHAUSTED = "global_budget_exhausted"
ITEM_LIMIT_EXHAUSTED = "item_limit"


@dataclass
class SourceBudgetSlice:
    source_type: str
    hard_bytes: int
    reserved_bytes: int = 0
    used_bytes: int = 0
    item_limit: int = 0
    used_items: int = 0
    required_reserve: int = 0
    status: str = "available"

    def __post_init__(self) -> None:
        self.source_type = normalize_source_type(self.source_type) or str(self.source_type)
        self.hard_bytes = max(0, int(self.hard_bytes or 0))
        self.reserved_bytes = max(0, int(self.reserved_bytes or 0))
        self.used_bytes = max(0, int(self.used_bytes or 0))
        self.item_limit = max(0, int(self.item_limit or 0))
        self.used_items = max(0, int(self.used_items or 0))
        self.required_reserve = max(0, int(self.required_reserve or 0))

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.hard_bytes - self.used_bytes)

    @property
    def remaining_items(self) -> int:
        if self.item_limit <= 0:
            return 10**9
        return max(0, self.item_limit - self.used_items)

    @property
    def reserve_outstanding(self) -> int:
        return max(0, self.required_reserve - self.used_bytes)

    def snapshot(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "hard_bytes": self.hard_bytes,
            "reserved_bytes": self.reserved_bytes,
            "used_bytes": self.used_bytes,
            "item_limit": self.item_limit,
            "used_items": self.used_items,
            "required_reserve": self.required_reserve,
            "remaining_bytes": self.remaining_bytes,
            "status": self.status,
        }

    def refresh_status(self) -> None:
        if self.item_limit > 0 and self.used_items >= self.item_limit:
            self.status = "source_budget_exhausted"
        elif self.used_bytes >= self.hard_bytes:
            self.status = "source_budget_exhausted"
        elif self.reserve_outstanding > 0:
            self.status = "reserved"
        else:
            self.status = "available"


@dataclass(frozen=True)
class BudgetDecision:
    admitted: bool
    source_type: str
    reason: str = ""
    stage: str = "admission"
    cost_bytes: int = 0
    source_remaining_bytes: int = 0
    global_remaining_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "source_type": self.source_type,
            "reason": self.reason,
            "stage": self.stage,
            "cost_bytes": self.cost_bytes,
            "source_remaining_bytes": self.source_remaining_bytes,
            "global_remaining_bytes": self.global_remaining_bytes,
        }


@dataclass
class EvidenceBudgetCoordinator:
    """Request-scoped source slices plus one hard global cap."""

    global_bytes: int
    source_limits: Mapping[str, int] = field(default_factory=dict)
    source_reserves: Mapping[str, int] = field(default_factory=dict)
    system_reserve_bytes: int = 0
    answer_reserve_bytes: int = 0
    item_limit_per_source: int = 0
    # Reserved exclusively for the derived comparison report.  Ordinary
    # retrieval sees it as another source's outstanding reserve and cannot
    # consume it.
    comparison_reserve_bytes: int = 0
    comparison_limit_bytes: int = 0
    enabled: bool = True
    _slices: dict[str, SourceBudgetSlice] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _rejections: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.global_bytes = max(0, int(self.global_bytes or 0))
        self.system_reserve_bytes = max(0, int(self.system_reserve_bytes or 0))
        self.answer_reserve_bytes = max(0, int(self.answer_reserve_bytes or 0))
        self.item_limit_per_source = max(0, int(self.item_limit_per_source or 0))
        self.comparison_reserve_bytes = max(0, int(self.comparison_reserve_bytes or 0))
        self.comparison_limit_bytes = max(
            self.comparison_reserve_bytes, int(self.comparison_limit_bytes or 0),
        )
        if self.comparison_reserve_bytes:
            self._slices[COMPARISON] = SourceBudgetSlice(
                source_type=COMPARISON,
                hard_bytes=self.comparison_limit_bytes or self.comparison_reserve_bytes,
                required_reserve=self.comparison_reserve_bytes,
                item_limit=self.item_limit_per_source,
            )
        for source_type, hard_bytes in dict(self.source_limits or {}).items():
            canonical = normalize_source_type(source_type) or str(source_type)
            reserve = int((self.source_reserves or {}).get(source_type)
                          or (self.source_reserves or {}).get(canonical) or 0)
            self._slices[canonical] = SourceBudgetSlice(
                source_type=canonical,
                hard_bytes=hard_bytes,
                required_reserve=reserve,
                item_limit=self.item_limit_per_source,
            )
        self._refresh_all()

    @property
    def evidence_bytes(self) -> int:
        """Global bytes available to formal evidence after output reserves."""

        return max(0, self.global_bytes - self.system_reserve_bytes - self.answer_reserve_bytes)

    def _canonical_slice(self, source_type: Any) -> SourceBudgetSlice:
        raw = str(source_type or "")
        canonical = normalize_source_type(raw) or raw
        slice_ = self._slices.get(canonical)
        if slice_ is not None:
            return slice_
        if canonical in KNOWLEDGE_SOURCE_TYPES:
            for candidate_type in (KNOWLEDGE_BASE_DOCUMENT, KNOWLEDGE_BASE_STRUCTURED):
                if candidate_type in self._slices:
                    # A concrete document/structured slice gets its own cap.
                    canonical = candidate_type
                    break
            slice_ = self._slices.get(canonical)
            if slice_ is not None:
                return slice_
        if canonical == KNOWLEDGE_BASE and KNOWLEDGE_BASE in self._slices:
            return self._slices[KNOWLEDGE_BASE]
        # Unknown or unconfigured types share the attachment/default slice only
        # when the caller explicitly configured a default; otherwise a zero
        # hard cap would silently admit unbounded evidence.
        if "default" in self._slices:
            return self._slices["default"]
        return SourceBudgetSlice(source_type=canonical or "unknown", hard_bytes=0)

    def bind_requirements(self, requirements: Iterable[Any]) -> None:
        """Apply per-source required reserves from a turn evidence plan."""

        with self._lock:
            for requirement in requirements:
                source_type = normalize_source_type(getattr(requirement, "source_type", ""))
                if not source_type:
                    continue
                target = self._canonical_slice(source_type)
                if target.source_type not in self._slices:
                    continue
                required = max(0, int(getattr(requirement, "required_reserve_bytes", 0) or 0))
                target.required_reserve = max(target.required_reserve, required)
            self._refresh_all()

    def _global_used(self) -> int:
        return sum(item.used_bytes for item in self._slices.values())

    def _other_reserved(self, source_type: str) -> int:
        return sum(
            item.reserve_outstanding
            for key, item in self._slices.items()
            if key != source_type
        )

    def try_admit(self, source_type: Any, cost_bytes: int, *, stage: str = "admission") -> BudgetDecision:
        """Atomically decide whether one candidate may become formal evidence."""

        if not self.enabled:
            return BudgetDecision(True, str(source_type or ""), stage=stage, cost_bytes=cost_bytes)
        cost = max(0, int(cost_bytes or 0))
        with self._lock:
            target = self._canonical_slice(source_type)
            self._refresh_all()
            source_remaining = target.remaining_bytes
            if target.source_type == COMPARISON:
                # The comparison reserve was withheld from ordinary
                # admission.  It may be spent even when other sources still
                # have outstanding reserves, but never above the global cap.
                global_available = max(0, self.evidence_bytes - self._global_used())
            else:
                global_available = max(
                    0,
                    self.evidence_bytes - self._global_used() - self._other_reserved(target.source_type),
                )
            if target.source_type not in self._slices:
                decision = BudgetDecision(
                    False, target.source_type, SOURCE_BUDGET_EXHAUSTED, stage, cost,
                    source_remaining, global_available,
                )
            elif target.remaining_items <= 0 or target.item_limit > 0 and target.used_items >= target.item_limit:
                decision = BudgetDecision(
                    False, target.source_type, ITEM_LIMIT_EXHAUSTED, stage, cost,
                    source_remaining, global_available,
                )
            elif cost > source_remaining:
                decision = BudgetDecision(
                    False, target.source_type, SOURCE_BUDGET_EXHAUSTED, stage, cost,
                    source_remaining, global_available,
                )
            elif cost > global_available:
                decision = BudgetDecision(
                    False, target.source_type, GLOBAL_BUDGET_EXHAUSTED, stage, cost,
                    source_remaining, global_available,
                )
            else:
                decision = BudgetDecision(
                    True, target.source_type, stage=stage, cost_bytes=cost,
                    source_remaining_bytes=source_remaining,
                    global_remaining_bytes=global_available,
                )
            if not decision.admitted:
                self._rejections.append(decision.as_dict())
                if len(self._rejections) > 200:
                    self._rejections = self._rejections[-200:]
            return decision

    def admit(self, source_type: Any, cost_bytes: int, *, stage: str = "admission") -> BudgetDecision:
        """Check and commit atomically.

        The runtime normally holds its own admission lock and calls
        ``try_admit``/``note_admitted`` separately; this convenience method is
        the safe direct API for comparison jobs and tests.
        """

        with self._lock:
            decision = self.try_admit(source_type, cost_bytes, stage=stage)
            if decision.admitted:
                self.note_admitted(decision.source_type, decision.cost_bytes)
            return decision

    def note_admitted(self, source_type: Any, cost_bytes: int) -> None:
        """Commit an admitted candidate exactly once (caller holds runtime lock)."""

        cost = max(0, int(cost_bytes or 0))
        with self._lock:
            target = self._canonical_slice(source_type)
            if target.source_type not in self._slices:
                return
            target.used_bytes += cost
            target.used_items += 1
            target.refresh_status()

    def rebuild(self, evidence: Iterable[Any], cost_fn) -> None:
        """Rebuild accounting from an authoritative evidence list."""

        with self._lock:
            for item in self._slices.values():
                item.used_bytes = 0
                item.used_items = 0
            for item in evidence:
                metadata = dict(getattr(item, "metadata", None) or {})
                source_type = source_type_for_metadata(metadata)
                target = self._canonical_slice(source_type)
                if target.source_type not in self._slices:
                    continue
                target.used_bytes += max(0, int(cost_fn(item) or 0))
                target.used_items += 1
            self._refresh_all()

    def _refresh_all(self) -> None:
        for item in self._slices.values():
            item.refresh_status()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_all()
            return {
                "enabled": self.enabled,
                "global_bytes": self.global_bytes,
                "system_reserve_bytes": self.system_reserve_bytes,
                "answer_reserve_bytes": self.answer_reserve_bytes,
                "evidence_bytes": self.evidence_bytes,
                "global_used_bytes": self._global_used(),
                "global_remaining_bytes": max(0, self.evidence_bytes - self._global_used()),
                "slices": {key: value.snapshot() for key, value in self._slices.items()},
                "rejections": list(self._rejections[-50:]),
            }

    @classmethod
    def from_settings(
        cls,
        *,
        item_limit: int | None = None,
        required_sources: Iterable[str] = (),
        include_comparison_reserve: bool = False,
    ) -> "EvidenceBudgetCoordinator":
        import src.settings as settings

        global_bytes = int(getattr(settings, "EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES", 32000) or 32000)
        system_reserve = int(getattr(settings, "EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES", 4000) or 0)
        answer_reserve = int(getattr(settings, "EVIDENCE_ANSWER_RESERVE_BYTES", 6000) or 0)
        attachment_bytes = int(getattr(settings, "EVIDENCE_ATTACHMENT_MAX_BYTES", 12000) or 0)
        kb_bytes = int(getattr(settings, "EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES", 16000) or 0)
        attachment_reserve = int(
            getattr(settings, "EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES", 4000) or 0
        )
        kb_reserve = int(
            getattr(settings, "EVIDENCE_KNOWLEDGE_BASE_REQUIRED_RESERVE_BYTES", 4000) or 0
        )
        requested = {normalize_source_type(item) for item in required_sources}
        source_reserves = {}
        if ATTACHMENT in requested:
            source_reserves[ATTACHMENT] = attachment_reserve
        if requested & KNOWLEDGE_SOURCE_TYPES:
            source_reserves[KNOWLEDGE_BASE_DOCUMENT] = kb_reserve
            source_reserves[KNOWLEDGE_BASE_STRUCTURED] = kb_reserve
        if not requested:
            source_reserves[ATTACHMENT] = attachment_reserve
            source_reserves[KNOWLEDGE_BASE_DOCUMENT] = kb_reserve
            source_reserves[KNOWLEDGE_BASE_STRUCTURED] = kb_reserve
        comparison_limit = int(
            getattr(settings, "DOCUMENT_COMPARISON_MAX_MODEL_BYTES", 12000) or 12000
        )
        comparison_reserve = comparison_limit if include_comparison_reserve else 0
        return cls(
            global_bytes=global_bytes,
            source_limits={
                ATTACHMENT: attachment_bytes,
                KNOWLEDGE_BASE_DOCUMENT: kb_bytes,
                KNOWLEDGE_BASE_STRUCTURED: kb_bytes,
            },
            source_reserves=source_reserves,
            system_reserve_bytes=system_reserve,
            answer_reserve_bytes=answer_reserve,
            item_limit_per_source=max(0, int(item_limit or 0)),
            comparison_reserve_bytes=comparison_reserve,
            comparison_limit_bytes=comparison_limit if include_comparison_reserve else 0,
            enabled=bool(getattr(settings, "EVIDENCE_SOURCE_AWARE_ENABLED", False)),
        )


__all__ = [
    "BudgetDecision",
    "EvidenceBudgetCoordinator",
    "SourceBudgetSlice",
    "GLOBAL_BUDGET_EXHAUSTED",
    "ITEM_LIMIT_EXHAUSTED",
    "SOURCE_BUDGET_EXHAUSTED",
]
