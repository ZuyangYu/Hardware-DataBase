"""Per-turn target/query evidence obligations and admission state.

P0 intentionally covers only (target, query) obligations. It never claims that
every numeric/unit/condition in the original question has been verified.

The second revision adds a server-owned ``source_type`` to every obligation.
A knowledge-base obligation can no longer be satisfied by attachment text and
a missing identity is no longer a wildcard.  Target-level obligations keep
their original record/locator semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from src.services.evidence_sources import (
    normalize_source_type,
    source_matches,
    source_type_for_metadata,
    source_type_for_processor,
)

PENDING = "pending"
CANDIDATE = "candidate"
ADMITTED = "admitted"
MISSING = "missing"
BLOCKED = "blocked"


@dataclass(frozen=True)
class EvidenceRequirement:
    requirement_id: str
    target_id: str
    query_id: str
    kb_id: int
    kb_name: str
    record_id: int
    question: str = ""
    sheet_name: str | None = None
    row_index: int | None = None
    cell_ref: str | None = None
    pin_value: str | None = None
    # Canonical origin.  Derived from the resolved processor kind, never from
    # a model argument.
    source_type: str = ""


@dataclass
class RequirementState:
    requirement: EvidenceRequirement
    state: str = PENDING
    evidence_refs: set[str] = field(default_factory=set)
    reason: str = ""


class TurnEvidencePlan:
    coverage_mode = "source_query"

    def __init__(self, *, turn_id: str, required: Iterable[EvidenceRequirement],
                 targeted_kb_ids: Iterable[int], selected_kb_ids: Iterable[int] = ()):
        self.turn_id = turn_id
        self.required = tuple(required)
        self.states: dict[str, RequirementState] = {
            requirement.requirement_id: RequirementState(requirement) for requirement in self.required
        }
        self.evidence_refs: dict[str, set[str]] = {}
        self.targeted_kb_ids = tuple(sorted({int(kb_id) for kb_id in targeted_kb_ids}))
        self.selected_kb_ids = tuple(sorted({int(kb_id) for kb_id in selected_kb_ids}))
        self.revision = 0
        self.purpose = "qa"
        # Injected by the runtime; must not depend on the budget module here.
        self.source_budget_provider: Callable[[], Mapping] | None = None
        self._false_match_seen: set[tuple[str, str]] = set()
        self._false_match_rejected = 0

    @classmethod
    def from_targets(cls, targets, *, turn_id: str = "", selected_kb_ids: Iterable[int] = ()):
        required = []
        for target in targets:
            source_type = source_type_for_processor(getattr(target, "processor_kind", ""))
            for index, locator in enumerate(target.locators, 1):
                required.append(EvidenceRequirement(
                    requirement_id=f"{target.target_id}:{target.query_id}:{index}",
                    target_id=target.target_id, query_id=target.query_id,
                    kb_id=target.kb_id, kb_name=target.kb_name, record_id=target.record_id,
                    question=target.question, sheet_name=locator.sheet_name,
                    row_index=locator.row_index, cell_ref=locator.cell_ref,
                    pin_value=locator.pin_value, source_type=source_type,
                ))
        targeted = sorted({target.kb_id for target in targets})
        return cls(turn_id=turn_id, required=required, targeted_kb_ids=targeted,
                   selected_kb_ids=selected_kb_ids)

    def _add_requirement(self, requirement: EvidenceRequirement) -> None:
        if requirement.requirement_id in self.states:
            return
        self.required = (*self.required, requirement)
        self.states[requirement.requirement_id] = RequirementState(requirement)

    def required_for(self, target_id: str) -> tuple[EvidenceRequirement, ...]:
        return tuple(state.requirement for state in self.states.values()
                     if state.requirement.target_id == target_id)

    def replace_targets(self, targets) -> None:
        replacement = TurnEvidencePlan.from_targets(
            targets, turn_id=self.turn_id, selected_kb_ids=self.selected_kb_ids,
        )
        self.required = replacement.required
        self.states = replacement.states
        self.targeted_kb_ids = replacement.targeted_kb_ids
        self.revision += 1

    def evaluate(self, candidate) -> tuple[str, ...]:
        """Return requirements whose source/record/locator/query are satisfied."""

        metadata = dict(getattr(candidate, "metadata", None) or {})
        locator = dict(getattr(candidate, "locator", None) or {})
        actual_source_type = source_type_for_metadata(metadata)
        kb_id = metadata.get("kb_id")
        record_id = metadata.get("local_record_id") or metadata.get("record_id")
        target_ids = {str(value) for value in (metadata.get("target_ids") or ())}
        query_id = str(metadata.get("query_id") or "")
        candidate_ref = str(getattr(candidate, "id", "") or "")
        matched = []
        for state in self.states.values():
            requirement = state.requirement
            if not source_matches(requirement.source_type, actual_source_type):
                self._note_false_match(candidate_ref, requirement)
                continue
            if requirement.kb_id and kb_id != requirement.kb_id:
                self._note_false_match(candidate_ref, requirement)
                continue
            if requirement.record_id and record_id != requirement.record_id:
                self._note_false_match(candidate_ref, requirement)
                continue
            if requirement.target_id not in target_ids:
                self._note_false_match(candidate_ref, requirement)
                continue
            if requirement.query_id and query_id != requirement.query_id:
                self._note_false_match(candidate_ref, requirement)
                continue
            if requirement.sheet_name and locator.get("sheet_name") not in (None, "", requirement.sheet_name):
                continue
            if requirement.cell_ref and str(locator.get("cell_ref") or "").upper() != requirement.cell_ref.upper():
                continue
            if requirement.row_index is not None and locator.get("row_index") not in (None, requirement.row_index):
                continue
            if requirement.pin_value and not _candidate_has_pin(candidate, requirement.pin_value):
                continue
            matched.append(requirement.requirement_id)
        return tuple(matched)

    def _note_false_match(self, candidate_ref: str, requirement: EvidenceRequirement) -> None:
        if not candidate_ref:
            return
        key = (candidate_ref, requirement.requirement_id)
        if key in self._false_match_seen:
            return
        self._false_match_seen.add(key)
        self._false_match_rejected += 1

    def mark_candidate(self, requirement_ids: Iterable[str], evidence_ref: str) -> None:
        for requirement_id in requirement_ids:
            state = self.states.get(requirement_id)
            if state is None or state.state == ADMITTED:
                continue
            state.state = CANDIDATE
            state.evidence_refs.add(evidence_ref)

    def mark_admitted(self, requirement_ids: Iterable[str], evidence_ref: str) -> None:
        for requirement_id in requirement_ids:
            state = self.states.get(requirement_id)
            if state is None:
                continue
            state.state = ADMITTED
            state.evidence_refs.add(evidence_ref)
            state.reason = ""

    def mark_missing(self, query_id: str | None = None, *, target_id: str | None = None) -> None:
        for state in self.states.values():
            if state.state == ADMITTED:
                continue
            if query_id is not None and state.requirement.query_id != query_id:
                continue
            if target_id is not None and state.requirement.target_id != target_id:
                continue
            if state.state in (PENDING, CANDIDATE):
                state.state = MISSING
                state.reason = "no_qualified_evidence"

    def mark_blocked(
        self,
        reason: str,
        *,
        query_id: str | None = None,
        target_id: str | None = None,
        source_type: str | None = None,
    ) -> None:
        normalized_source = normalize_source_type(source_type)
        for state in self.states.values():
            if state.state == ADMITTED:
                continue
            if normalized_source and state.requirement.source_type and not source_matches(
                state.requirement.source_type, normalized_source,
            ):
                continue
            if query_id is not None and state.requirement.query_id != query_id:
                continue
            if target_id is not None and state.requirement.target_id != target_id:
                continue
            state.state = BLOCKED
            state.reason = reason

    def coverage_snapshot(self) -> dict:
        counts = {name: 0 for name in (PENDING, CANDIDATE, ADMITTED, MISSING, BLOCKED)}
        for state in self.states.values():
            counts[state.state] = counts.get(state.state, 0) + 1
        selected = set(self.selected_kb_ids) or set(self.targeted_kb_ids)
        by_source: dict[str, dict[str, int]] = {}
        blocked_by_source: dict[str, int] = {}
        evidence_by_source: dict[str, int] = {}
        for state in self.states.values():
            source = normalize_source_type(state.requirement.source_type) or "unknown"
            row = by_source.setdefault(source, {name: 0 for name in counts})
            row[state.state] = row.get(state.state, 0) + 1
            if state.state == BLOCKED:
                blocked_by_source[source] = blocked_by_source.get(source, 0) + 1
            if state.state == ADMITTED:
                evidence_by_source[source] = evidence_by_source.get(source, 0) + 1
        source_budget: Mapping = {}
        if callable(self.source_budget_provider):
            try:
                source_budget = dict(self.source_budget_provider() or {})
            except Exception:
                source_budget = {}
        source_coverage = {
            source: {
                "required": sum(row.values()),
                "admitted": evidence_by_source.get(source, 0),
                "complete": bool(sum(row.values())) and evidence_by_source.get(source, 0) == sum(row.values()),
            }
            for source, row in by_source.items()
        }
        return {
            "coverage_mode": self.coverage_mode,
            "purpose": self.purpose,
            "required": len(self.required),
            "states": counts,
            "complete": bool(self.required) and counts[ADMITTED] == len(self.required),
            "source_coverage": source_coverage,
            "by_source": by_source,
            "source_budget_status": source_budget,
            "blocked_by_source": blocked_by_source,
            "false_match_rejected": self._false_match_rejected,
            "targeted_kb_ids": list(self.targeted_kb_ids),
            "not_targeted_kb_ids": sorted(selected - set(self.targeted_kb_ids)),
            "requirements": [
                {
                    "requirement_id": state.requirement.requirement_id,
                    "target_id": state.requirement.target_id,
                    "query_id": state.requirement.query_id,
                    "source_type": state.requirement.source_type,
                    "kb_id": state.requirement.kb_id,
                    "record_id": state.requirement.record_id,
                    "state": state.state,
                    "reason": state.reason,
                    "evidence_refs": sorted(state.evidence_refs),
                }
                for state in self.states.values()
            ],
        }


def _candidate_has_pin(candidate, pin_value: str) -> bool:
    metadata = dict(getattr(candidate, "metadata", None) or {})
    locator = dict(getattr(candidate, "locator", None) or {})
    if str(locator.get("pin_value") or "") == str(pin_value):
        return True
    values = metadata.get("values")
    if isinstance(values, Mapping):
        return any(str(value) == str(pin_value) for value in values.values() if value not in (None, ""))
    text = " ".join([
        str(getattr(candidate, "content", "") or ""),
        str(metadata.get("raw_text") or ""),
    ])
    import re

    return bool(re.search(rf"(?<!\d){re.escape(str(pin_value))}(?!\d)", text))
