"""Mutable progress, coverage and answer-manifest state for a TaskPlan.

``TaskPlan`` is the immutable contract.  Everything in this module is the
mutable side: item-level coverage, deliverable states, tool observations and
the server-built answer manifest used by the completion gate.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DeliverableStateName = Literal["pending", "candidate", "satisfied", "partial", "blocked"]
FailureClass = Literal["", "retryable", "recoverable", "terminal"]


class ChapterCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_key: str
    item_ids: tuple[str, ...] = ()
    required_count: int = 0
    covered_count: int = 0
    state: DeliverableStateName = "pending"


class CoverageLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_key: str = ""
    report_id: str = ""
    report_hash: str = ""
    required_item_ids: tuple[str, ...] = ()
    rendered_item_ids: tuple[str, ...] = ()
    admitted_item_ids: tuple[str, ...] = ()
    cited_item_ids: tuple[str, ...] = ()
    missing_item_ids: tuple[str, ...] = ()
    chapters: dict[str, ChapterCoverage] = Field(default_factory=dict)
    next_cursor: str = ""
    warnings: tuple[str, ...] = ()

    def recompute(self) -> None:
        rendered = set(self.rendered_item_ids)
        required = set(self.required_item_ids)
        self.missing_item_ids = tuple(item for item in self.required_item_ids if item not in rendered)
        for chapter in self.chapters.values():
            if chapter.item_ids:
                chapter.covered_count = len(
                    [item for item in rendered if item in set(chapter.item_ids)]
                )
            chapter.covered_count = min(chapter.required_count, chapter.covered_count)
            if chapter.required_count <= 0:
                chapter.state = "satisfied"
            elif chapter.covered_count >= chapter.required_count:
                chapter.state = "satisfied"
            elif chapter.covered_count > 0:
                chapter.state = "partial"
            else:
                chapter.state = "pending"
        if required and not self.missing_item_ids:
            self.missing_item_ids = ()

    def mark_rendered(self, item_ids: tuple[str, ...] | list[str]) -> None:
        values = tuple(dict.fromkeys(str(item) for item in item_ids if str(item)))
        if not values:
            return
        self.rendered_item_ids = tuple(dict.fromkeys((*self.rendered_item_ids, *values)))
        self.recompute()

    def mark_admitted(self, item_ids: tuple[str, ...] | list[str]) -> None:
        values = tuple(dict.fromkeys(str(item) for item in item_ids if str(item)))
        if not values:
            return
        self.admitted_item_ids = tuple(dict.fromkeys((*self.admitted_item_ids, *values)))
        self.recompute()

    def mark_cited(self, item_ids: tuple[str, ...] | list[str]) -> None:
        values = tuple(dict.fromkeys(str(item) for item in item_ids if str(item)))
        if not values:
            return
        self.cited_item_ids = tuple(dict.fromkeys((*self.cited_item_ids, *values)))

    def set_chapters(self, chapters: dict[str, tuple[str, ...]]) -> None:
        self.chapters = {
            key: ChapterCoverage(
                chapter_key=key,
                item_ids=tuple(str(item) for item in items),
                required_count=len(items),
            )
            for key, items in chapters.items()
        }
        self.recompute()

    def snapshot(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "report_id": self.report_id,
            "report_hash": self.report_hash,
            "required": len(self.required_item_ids),
            "rendered": len(self.rendered_item_ids),
            "admitted": len(self.admitted_item_ids),
            "cited": len(self.cited_item_ids),
            "missing": list(self.missing_item_ids),
            "next_cursor": self.next_cursor,
            "chapters": {key: value.model_dump() for key, value in self.chapters.items()},
            "warnings": list(self.warnings),
        }


class DeliverableState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: DeliverableStateName = "pending"
    required_count: int = 0
    satisfied_count: int = 0
    missing_item_ids: tuple[str, ...] = ()
    reason: str = ""


class DeliverableUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deliverable_id: str
    item_ids: tuple[str, ...] = ()
    state: DeliverableStateName = "candidate"
    covered_count: int = 0
    missing_count: int = 0
    reason: str = ""


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    invocation_id: str = ""
    status: Literal["ok", "empty", "partial", "failed", "blocked"] = "ok"
    failure_class: FailureClass = ""
    error_code: str = ""
    admitted_evidence_ids: tuple[str, ...] = ()
    deliverable_updates: tuple[DeliverableUpdate, ...] = ()
    coverage_delta: dict[str, Any] = Field(default_factory=dict)
    budget_delta: dict[str, Any] = Field(default_factory=dict)
    next_cursor: str = ""
    warnings: tuple[str, ...] = ()


class AnswerManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_key: str = ""
    delivery_mode: Literal["inline", "artifact", "hybrid"] = "inline"
    required_item_ids: tuple[str, ...] = ()
    covered_item_ids: tuple[str, ...] = ()
    cited_item_ids: tuple[str, ...] = ()
    missing_item_ids: tuple[str, ...] = ()
    citation_numbers: tuple[int, ...] = ()
    artifact_id: str = ""
    page_cursor: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class TaskProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    status: Literal[
        "planned",
        "running",
        "verifying",
        "complete",
        "complete_with_warnings",
        "partial",
        "blocked",
        "cancelled",
    ] = "planned"
    revision: int = 0
    deliverable_states: dict[str, DeliverableState] = Field(default_factory=dict)
    coverage: CoverageLedger = Field(default_factory=CoverageLedger)
    attempts: dict[str, int] = Field(default_factory=dict)
    budget_usage: dict[str, Any] = Field(default_factory=dict)
    answer_manifest: AnswerManifest | None = None
    stop_reason: str = ""
    missing_item_ids: tuple[str, ...] = ()
    next_cursor: str = ""
    updated_at: str = ""

    @classmethod
    def new(cls, plan_id: str) -> "TaskProgress":
        return cls(plan_id=plan_id, status="planned")

    def ensure_deliverables(self, deliverable_ids: tuple[str, ...] | list[str]) -> None:
        for deliverable_id in deliverable_ids:
            self.deliverable_states.setdefault(deliverable_id, DeliverableState())

    def apply_observation(self, observation: ToolObservation) -> None:
        for update in observation.deliverable_updates:
            state = self.deliverable_states.setdefault(update.deliverable_id, DeliverableState())
            state.state = update.state
            state.required_count = max(state.required_count, update.covered_count + update.missing_count)
            state.satisfied_count = max(state.satisfied_count, update.covered_count)
            state.missing_item_ids = tuple(
                item for item in state.missing_item_ids if item not in update.item_ids
            )
            if update.reason:
                state.reason = update.reason
        coverage = observation.coverage_delta or {}
        if coverage.get("rendered_item_ids"):
            self.coverage.mark_rendered(tuple(coverage["rendered_item_ids"]))
        if coverage.get("admitted_item_ids"):
            self.coverage.mark_admitted(tuple(coverage["admitted_item_ids"]))
        if observation.next_cursor:
            self.next_cursor = observation.next_cursor
            self.coverage.next_cursor = observation.next_cursor

    def snapshot(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "revision": self.revision,
            "deliverables": {
                key: value.model_dump() for key, value in self.deliverable_states.items()
            },
            "coverage": self.coverage.snapshot(),
            "attempts": dict(self.attempts),
            "budget_usage": dict(self.budget_usage),
            "answer_manifest": self.answer_manifest.to_dict() if self.answer_manifest else None,
            "stop_reason": self.stop_reason,
            "missing_item_ids": list(self.missing_item_ids),
            "next_cursor": self.next_cursor,
        }


_CITATION_RE = re.compile(r"\[(\d+)\]")


def build_answer_manifest(
    *,
    scope_key: str,
    required_item_ids: tuple[str, ...] | list[str],
    coverage: CoverageLedger,
    answer_text: str,
    evidence: list[Any],
    artifact_id: str = "",
    warnings: tuple[str, ...] = (),
) -> AnswerManifest:
    """Build a server-owned answer manifest from citations and coverage."""

    cited_numbers: list[int] = []
    for match in _CITATION_RE.finditer(str(answer_text or "")):
        number = int(match.group(1))
        if number not in cited_numbers:
            cited_numbers.append(number)
    by_number: dict[int, str] = {}
    for item in evidence or []:
        metadata = dict(getattr(item, "metadata", None) or {})
        try:
            number = int(metadata.get("citation_number") or 0)
        except (TypeError, ValueError):
            number = 0
        item_id = str(metadata.get("comparison_item_id") or "")
        if number and item_id:
            by_number[number] = item_id
    cited_item_ids = tuple(
        dict.fromkeys(by_number[number] for number in cited_numbers if number in by_number)
    )
    required = tuple(str(item) for item in required_item_ids if str(item))
    covered = tuple(
        item for item in required if item in set(cited_item_ids) or item in set(coverage.rendered_item_ids)
    )
    missing = tuple(item for item in required if item not in set(covered))
    return AnswerManifest(
        scope_key=scope_key,
        delivery_mode="artifact" if artifact_id else "inline",
        required_item_ids=required,
        covered_item_ids=covered,
        cited_item_ids=cited_item_ids,
        missing_item_ids=missing,
        citation_numbers=tuple(cited_numbers),
        artifact_id=artifact_id,
        page_cursor=coverage.next_cursor,
        warnings=tuple(warnings),
    )


__all__ = [
    "AnswerManifest",
    "ChapterCoverage",
    "CoverageLedger",
    "DeliverableState",
    "DeliverableUpdate",
    "TaskProgress",
    "ToolObservation",
    "build_answer_manifest",
]
