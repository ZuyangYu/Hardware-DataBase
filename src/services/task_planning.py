"""Deterministic task contracts for one conversation turn.

The planner does not classify intent again.  It consumes the server-owned
``ConversationPlan.evidence_purpose`` and compiles a scoped delivery contract:
what the user asked for, which report/branch scope is required, which
deliverables are mandatory, and which bounded recovery actions are allowed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services.task_budget import TurnBudgetProfile

TaskKind = Literal["qa", "summarize", "extract", "compare", "author", "visual"]
ScopeKind = Literal[
    "full_report",
    "chapter_summary",
    "chapter_detail",
    "item_detail",
    "qa",
    "summarize",
    "extract",
    "author",
]
DeliveryPreference = Literal["inline", "artifact", "any"]

_TASK_KINDS: tuple[str, ...] = ("qa", "summarize", "extract", "compare", "author", "visual")
_CHAPTER_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百两]+)\s*(?:章|部分|篇)")
_SECTION_RE = re.compile(r"第?\s*(\d+(?:\.\d+)+)\s*(?:节|小节)?")
_CHAPTER_EN_RE = re.compile(r"\bchapter\s*(\d+)\b", re.IGNORECASE)
_SECTION_EN_RE = re.compile(r"\bsection\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE)
_TABLE_RE = re.compile(r"(?:表|table)\s*[-_]?(\d+)", re.IGNORECASE)
_ALL_DETAIL_RE = re.compile(
    r"(?:所有|全部|每一?|逐)\s*(?:章节|章|条|项|个)?\s*(?:差异|变化|内容|条目)?"
    r"|全文|整篇|全部差异|所有差异|每一条",
    re.IGNORECASE,
)
_CHAPTER_SUMMARY_RE = re.compile(
    r"按\s*章节|章节\s*(?:概览|汇总|级|差异)|各章|每章|逐章|chapter\s*summary",
    re.IGNORECASE,
)
_DETAIL_RE = re.compile(r"详细|具体|逐条|逐项|展开|列出|每条|每项", re.IGNORECASE)


def normalize_task_kind(value: Any) -> TaskKind:
    text = str(value or "").strip().lower()
    if text in _TASK_KINDS:
        return text  # type: ignore[return-value]
    return "qa"


def _chinese_number(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return str(int(text))
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return "10"
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return str(tens * 10 + ones)
    if "百" in text:
        left, _, right = text.partition("百")
        hundreds = digits.get(left, 1) if left else 1
        rest = _chinese_number(right) if right else "0"
        return str(hundreds * 100 + int(rest))
    return str(digits.get(text, 0))


class DeliveryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_kind: ScopeKind = "qa"
    delivery_preference: DeliveryPreference = "any"
    chapter_keys: tuple[str, ...] = ()
    section_keys: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    table_keys: tuple[str, ...] = ()
    max_inline_items: int = Field(default=0, ge=0)
    page_size: int = Field(default=0, ge=0)
    cursor: str = ""

    @property
    def scope_key(self) -> str:
        if self.scope_kind == "full_report":
            return "full"
        if self.chapter_keys:
            return "chapters:" + ",".join(self.chapter_keys)
        if self.section_keys:
            return "sections:" + ",".join(self.section_keys)
        if self.item_ids:
            return "items:" + ",".join(self.item_ids)
        if self.table_keys:
            return "tables:" + ",".join(self.table_keys)
        return self.scope_kind


class CoverageRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_kind: Literal[
        "all_scope_items",
        "one_per_chapter",
        "top_n_items",
        "artifact_complete",
        "citation_coverage",
        "answer_non_empty",
    ]
    item_ids: tuple[str, ...] = ()
    chapter_keys: tuple[str, ...] = ()
    max_items: int = Field(default=0, ge=0)
    require_both_sides: bool = False
    require_diff: bool = False
    require_citation: bool = False


class DeliverableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deliverable_id: str
    kind: Literal[
        "chapter_summary",
        "item_body",
        "inline_diff",
        "index",
        "citation",
        "warning",
        "artifact",
        "answer",
    ]
    required: bool = True
    coverage_rule: CoverageRule
    source_constraints: dict[str, Any] = Field(default_factory=dict)


class EvidenceRequirementSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    source_type: str
    source_ids: tuple[str, ...] = ()
    document_versions: tuple[str, ...] = ()
    locator_constraints: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    scope_item_ids: tuple[str, ...] = ()


class TaskStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    action: Literal[
        "freeze_sources",
        "compare_sources",
        "expand_sections",
        "retrieve_source",
        "read_evidence",
        "verify_coverage",
        "compose_answer",
    ]
    depends_on: tuple[str, ...] = ()
    max_attempts: int = Field(default=1, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    idempotency_key: str = ""
    on_failure: Literal["retry", "recover", "block", "skip"] = "block"


class RecoveryRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    when_missing: tuple[str, ...] = ()
    when_failure: tuple[str, ...] = ()
    action: Literal[
        "compose_chapter_summary",
        "comparison_expand",
        "load_frozen_report",
        "retrieve_other_side",
        "promote_from_report",
        "stop",
    ]
    max_attempts: int = Field(default=1, ge=1)
    estimated_tool_calls: int = Field(default=1, ge=0)
    estimated_bytes: int = Field(default=0, ge=0)


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal[
        "compose_chapter_summary",
        "comparison_expand",
        "load_frozen_report",
        "retrieve_other_side",
        "promote_from_report",
        "stop",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    estimated_cost: dict[str, int] = Field(default_factory=dict)


class CompletionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    require_all_required: bool = True
    allow_artifact_completion: bool = True
    allow_partial_for_inline: bool = True
    require_citation_for_facts: bool = True
    optional_missing_is_complete: bool = True
    max_inline_items: int = Field(default=0, ge=0)


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "task-plan-v1"
    plan_id: str
    turn_id: str = ""
    goal: str
    task_kind: TaskKind
    delivery_scope: DeliveryScope
    deliverables: tuple[DeliverableSpec, ...] = ()
    evidence_requirements: tuple[EvidenceRequirementSpec, ...] = ()
    steps: tuple[TaskStep, ...] = ()
    recovery_rules: tuple[RecoveryRule, ...] = ()
    budget_profile: TurnBudgetProfile
    completion_policy: CompletionPolicy
    source_scope_fingerprint: str = ""
    compile_errors: tuple[str, ...] = ()
    plan_hash: str = ""

    @model_validator(mode="after")
    def _validate_contract(self) -> "TaskPlan":
        ids = [item.deliverable_id for item in self.deliverables]
        if len(ids) != len(set(ids)):
            raise ValueError("deliverable IDs must be unique")
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step IDs must be unique")
        known_steps = set(step_ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known_steps
            if unknown:
                raise ValueError(f"unknown step dependencies: {sorted(unknown)}")
        return self


def _scope_base(scope_kind: ScopeKind, **kwargs: Any) -> DeliveryScope:
    return DeliveryScope(scope_kind=scope_kind, **kwargs)


def scope_from_query(
    query: str,
    *,
    task_kind: TaskKind = "compare",
    default: ScopeKind | None = None,
) -> DeliveryScope:
    """Parse the user's delivery scope without consulting model output."""

    text = str(query or "").strip()
    if task_kind == "qa":
        return _scope_base("qa")
    if task_kind == "summarize":
        return _scope_base("summarize", delivery_preference="any")
    if task_kind == "extract":
        return _scope_base("extract", delivery_preference="any")
    if task_kind == "author":
        return _scope_base("author", delivery_preference="artifact")
    if task_kind != "compare":
        return _scope_base("chapter_summary" if default is None else default)

    # Explicit table/item scope wins over chapter scope.
    table_match = _TABLE_RE.search(text)
    if table_match:
        return _scope_base(
            "item_detail",
            table_keys=(f"table-{int(table_match.group(1))}",),
            delivery_preference="inline",
        )

    section_match = _SECTION_RE.search(text) or _SECTION_EN_RE.search(text)
    if section_match:
        key = section_match.group(1)
        return _scope_base(
            "chapter_detail",
            section_keys=(f"num:{key}",),
            delivery_preference="inline",
        )

    chapter_match = _CHAPTER_RE.search(text) or _CHAPTER_EN_RE.search(text)
    if chapter_match:
        key = _chinese_number(chapter_match.group(1))
        return _scope_base(
            "chapter_detail",
            chapter_keys=(f"num:{key}",),
            delivery_preference="inline",
        )

    if _ALL_DETAIL_RE.search(text) and not _CHAPTER_SUMMARY_RE.search(text):
        return _scope_base("full_report", delivery_preference="artifact")
    if _CHAPTER_SUMMARY_RE.search(text):
        return _scope_base("chapter_summary", delivery_preference="inline")
    if default is not None:
        return _scope_base(default)
    # A plain "compare these documents" request is a chapter-level overview,
    # not an implicit promise to inline every changed item.
    return _scope_base("chapter_summary", delivery_preference="inline")


def _rule(
    rule_kind: str,
    *,
    require_diff: bool = False,
    require_both_sides: bool = False,
    require_citation: bool = False,
) -> CoverageRule:
    return CoverageRule(
        rule_kind=rule_kind,  # type: ignore[arg-type]
        require_diff=require_diff,
        require_both_sides=require_both_sides,
        require_citation=require_citation,
    )


def _deliverable(
    deliverable_id: str,
    kind: str,
    rule: CoverageRule,
    *,
    required: bool = True,
) -> DeliverableSpec:
    return DeliverableSpec(
        deliverable_id=deliverable_id,
        kind=kind,  # type: ignore[arg-type]
        required=required,
        coverage_rule=rule,
    )


def build_deliverables(task_kind: TaskKind, scope: DeliveryScope) -> tuple[DeliverableSpec, ...]:
    if task_kind == "compare":
        if scope.scope_kind == "chapter_summary":
            return (
                _deliverable("compare.chapter_summary", "chapter_summary", _rule("one_per_chapter")),
                _deliverable("compare.index", "index", _rule("one_per_chapter")),
                _deliverable("compare.warning", "warning", _rule("answer_non_empty"), required=False),
                _deliverable("compare.answer", "answer", _rule("answer_non_empty")),
            )
        if scope.scope_kind == "full_report":
            return (
                _deliverable("compare.artifact", "artifact", _rule("artifact_complete")),
                _deliverable("compare.index", "index", _rule("one_per_chapter")),
                _deliverable("compare.answer", "answer", _rule("answer_non_empty")),
                _deliverable("compare.warning", "warning", _rule("answer_non_empty"), required=False),
            )
        # chapter_detail / item_detail
        return (
            _deliverable("compare.item_body", "item_body", _rule("all_scope_items", require_both_sides=False)),
            _deliverable("compare.inline_diff", "inline_diff", _rule("all_scope_items", require_diff=True)),
            _deliverable("compare.citation", "citation", _rule("citation_coverage", require_citation=True)),
            _deliverable("compare.answer", "answer", _rule("answer_non_empty")),
            _deliverable("compare.warning", "warning", _rule("answer_non_empty"), required=False),
        )
    if task_kind == "qa":
        return (
            _deliverable("qa.answer", "answer", _rule("answer_non_empty")),
            _deliverable("qa.citation", "citation", _rule("citation_coverage", require_citation=True)),
        )
    if task_kind in {"summarize", "extract", "visual"}:
        return (
            _deliverable(f"{task_kind}.answer", "answer", _rule("answer_non_empty")),
            _deliverable(f"{task_kind}.citation", "citation", _rule("citation_coverage", require_citation=True)),
            _deliverable(f"{task_kind}.warning", "warning", _rule("answer_non_empty"), required=False),
        )
    # authoring keeps its existing Work Order/approval state machine; the task
    # plan only records the expected artifact and answer boundary.
    return (
        _deliverable("author.artifact", "artifact", _rule("artifact_complete")),
        _deliverable("author.answer", "answer", _rule("answer_non_empty")),
    )


def _steps_for(task_kind: TaskKind, scope: DeliveryScope) -> tuple[TaskStep, ...]:
    if task_kind == "compare":
        action = "expand_sections" if scope.scope_kind in {"chapter_detail", "item_detail"} else "compare_sources"
        return (
            TaskStep(step_id="freeze", action="freeze_sources", idempotency_key="freeze"),
            TaskStep(step_id="compare", action=action, depends_on=("freeze",), idempotency_key="compare"),
            TaskStep(step_id="verify", action="verify_coverage", depends_on=("compare",)),
            TaskStep(step_id="compose", action="compose_answer", depends_on=("verify",)),
        )
    if task_kind == "qa":
        return (
            TaskStep(step_id="retrieve", action="retrieve_source", on_failure="recover"),
            TaskStep(step_id="verify", action="verify_coverage", depends_on=("retrieve",)),
            TaskStep(step_id="compose", action="compose_answer", depends_on=("verify",)),
        )
    return (
        TaskStep(step_id="retrieve", action="retrieve_source", on_failure="recover"),
        TaskStep(step_id="verify", action="verify_coverage", depends_on=("retrieve",)),
        TaskStep(step_id="compose", action="compose_answer", depends_on=("verify",)),
    )


def _recovery_rules(task_kind: TaskKind, scope: DeliveryScope) -> tuple[RecoveryRule, ...]:
    if task_kind != "compare":
        return (
            RecoveryRule(
                rule_id="retry-retrieval",
                when_failure=("retryable",),
                action="retrieve_other_side",
                max_attempts=1,
            ),
        )
    if scope.scope_kind == "chapter_summary":
        return (
            RecoveryRule(
                rule_id="fill-chapter-summary",
                when_missing=("compare.chapter_summary", "compare.index"),
                action="compose_chapter_summary",
                max_attempts=2,
            ),
        )
    if scope.scope_kind == "full_report":
        return (
            RecoveryRule(
                rule_id="materialize-artifact",
                when_missing=("compare.artifact",),
                action="promote_from_report",
                max_attempts=1,
                estimated_bytes=0,
            ),
        )
    return (
        RecoveryRule(
            rule_id="expand-scope",
            when_missing=("compare.item_body", "compare.inline_diff"),
            action="comparison_expand",
            max_attempts=2,
            estimated_tool_calls=1,
        ),
        RecoveryRule(
            rule_id="reload-frozen",
            when_failure=("comparison_report_conflict",),
            action="load_frozen_report",
            max_attempts=1,
            estimated_tool_calls=1,
        ),
    )


def _stable_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class TaskPlanCompiler:
    """Compile a server-owned task contract from the existing route plan."""

    schema_version = "task-plan-v1"

    def compile(
        self,
        *,
        query: str,
        turn_id: str = "",
        evidence_purpose: Any = "qa",
        source_scope: str = "auto",
        source_scope_fingerprint: str = "",
        has_attachments: bool = False,
        has_kb: bool = False,
        comparison_available: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> TaskPlan:
        task_kind = normalize_task_kind(evidence_purpose)
        scope = scope_from_query(query, task_kind=task_kind)
        errors: list[str] = []
        if task_kind == "compare" and not comparison_available:
            errors.append("comparison_capability_unavailable")
        if task_kind == "compare" and not (
            (has_attachments and has_kb) or (not has_kb and has_attachments)
        ):
            errors.append("comparison_requires_two_sources")
        if task_kind == "author" and "document_authoring" not in capabilities:
            errors.append("document_authoring_capability_unavailable")

        deliverables = build_deliverables(task_kind, scope)
        plan_payload = {
            "schema_version": self.schema_version,
            "plan_id": f"task-{_stable_hash({'turn': turn_id, 'query': query, 'scope': scope.scope_key})}",
            "turn_id": str(turn_id or ""),
            "goal": str(query or "").strip(),
            "task_kind": task_kind,
            "delivery_scope": scope.model_dump(),
            "deliverables": [item.model_dump() for item in deliverables],
            "evidence_requirements": [],
            "steps": [step.model_dump() for step in _steps_for(task_kind, scope)],
            "recovery_rules": [rule.model_dump() for rule in _recovery_rules(task_kind, scope)],
            "budget_profile": TurnBudgetProfile.from_settings().model_dump(),
            "completion_policy": CompletionPolicy().model_dump(),
            "source_scope_fingerprint": str(source_scope_fingerprint or ""),
            "compile_errors": errors,
        }
        plan_hash = _stable_hash(plan_payload)
        return TaskPlan.model_validate({**plan_payload, "plan_hash": plan_hash})


__all__ = [
    "CompletionPolicy",
    "CoverageRule",
    "DeliverableSpec",
    "DeliveryScope",
    "EvidenceRequirementSpec",
    "RecoveryAction",
    "RecoveryRule",
    "TaskKind",
    "TaskPlan",
    "TaskPlanCompiler",
    "TaskStep",
    "TurnBudgetProfile",
    "build_deliverables",
    "normalize_task_kind",
    "scope_from_query",
]
