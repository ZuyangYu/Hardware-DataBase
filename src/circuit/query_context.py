from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal


@dataclass
class CircuitIntent:
    intent: str
    target_entity_type: str | None = None
    entity_text: str | None = None
    required_fields: list[str] = field(default_factory=list)
    is_global_query: bool = False
    is_single_entity_detail: bool = False
    pre_intent: str = "ambiguous"
    ordinal: int | None = None
    confidence: float = 0.0
    # Signal-path queries carry two endpoints ("信号从 U1 到 U3 经过哪些模块").
    from_entity: str | None = None
    to_entity: str | None = None
    # Planner provenance: "rule" | "llm_controlled" | "upstream_hint".
    planner_source: str = "rule"


@dataclass
class CircuitScope:
    scope_type: Literal["single_circuit", "multiple_circuits", "all_circuits", "unresolved"]
    kb_name: str
    circuit_ids: list[str] = field(default_factory=list)
    matched_files: list[str] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0
    clarification_options: list["ClarificationOption"] = field(default_factory=list)


@dataclass
class ResolvedEntity:
    entity_type: str
    entity_id: str
    display_name: str
    source: str = "structured"
    confidence: float = 1.0
    circuit_id: str | None = None
    ordinal: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClarificationOption:
    label: str
    value: str
    option_type: Literal["circuit", "module", "instance", "net", "action"]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



@dataclass
class QueryEvidence:
    source_type: str
    circuit_id: str | None = None
    source_file: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    field_path: str | None = None
    parser: str | None = None
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceAvailability:
    source_type: str
    status: Literal["available", "missing", "partial", "not_used", "not_applicable"]
    circuit_id: str | None = None
    source_files: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CircuitSourceStatus:
    """Structured source availability for one circuit-domain response.

    ``sources`` is intentionally a list so multi-circuit answers can report
    per-circuit EDF/PDF/graph availability without flattening provenance into
    a loose dict. ``to_dict`` keeps the public JSON surface backward-friendly.
    """

    kb_name: str
    circuit_ids: list[str] = field(default_factory=list)
    scope_type: str = "unresolved"
    used_sources: list[str] = field(default_factory=list)
    sources: list[SourceAvailability] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kb_name": self.kb_name,
            "circuit_ids": list(self.circuit_ids),
            "scope_type": self.scope_type,
            "used_sources": list(self.used_sources),
            "sources": [source.to_dict() for source in self.sources],
            "warnings": list(self.warnings),
        }


@dataclass
class CircuitTraceEvent:
    step: str
    status: str = "ok"
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CircuitQueryTrace:
    kb_name: str
    session_id: str
    question: str
    events: list[CircuitTraceEvent] = field(default_factory=list)
    tool_calls: int = 0
    max_tool_calls: int = 0
    max_steps: int = 0
    answer_mode: str | None = None

    def add(self, step: str, status: str = "ok", detail: str = "", **metadata: Any) -> None:
        self.events.append(CircuitTraceEvent(step=step, status=status, detail=detail, metadata=metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kb_name": self.kb_name,
            "session_id": self.session_id,
            "question": self.question,
            "events": [event.to_dict() for event in self.events],
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_steps": self.max_steps,
            "answer_mode": self.answer_mode,
        }


@dataclass
class CircuitQueryEvaluation:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CircuitQueryPlan:
    question: str
    kb_name: str
    operation: str
    intent: CircuitIntent
    scope: CircuitScope
    parameters: dict[str, Any] = field(default_factory=dict)
    resolved_entities: list[ResolvedEntity] = field(default_factory=list)
    status: Literal["ready", "unsupported", "needs_scope_clarification", "needs_entity_clarification"] = "ready"
    entity_candidates: list[ResolvedEntity] = field(default_factory=list)
    reason: str | None = None
    # Bounded Plan-and-Execute fields (plan §3.6 / §6.2).
    steps: list[dict[str, Any]] = field(default_factory=list)
    map_step: dict[str, Any] | None = None
    reduce_strategy: Literal["group_by_circuit", "merge"] = "group_by_circuit"
    complexity: Literal["simple", "complex"] = "simple"
    max_steps: int = 3
    max_tool_calls: int = 6
    max_recovery_rounds: int = 1
    stop_when: list[str] = field(default_factory=list)
    # Which execution-context key feeds the answer formatter.
    answer_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CircuitToolResponse:
    answer: str
    answer_mode: Literal[
        "direct_answer",
        "grouped_by_circuit",
        "needs_clarification",
        "partial_answer",
        "unsupported",
    ]
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    missing_info: list[str] = field(default_factory=list)
    follow_up_suggestions: list[str] = field(default_factory=list)
    clarification_options: list[ClarificationOption] = field(default_factory=list)
    source_status: CircuitSourceStatus | dict[str, Any] | None = None
    resolved_entities: list[ResolvedEntity] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [
            item.to_dict() if hasattr(item, "to_dict") else asdict(item) if is_dataclass(item) else item
            for item in self.evidence
        ]
        data["clarification_options"] = [opt.to_dict() for opt in self.clarification_options]
        data["resolved_entities"] = [entity.to_dict() for entity in self.resolved_entities]
        if hasattr(self.source_status, "to_dict"):
            data["source_status"] = self.source_status.to_dict()
        return data


@dataclass
class CircuitSessionContext:
    session_id: str
    kb_name: str
    current_circuit_id: str | None = None
    last_entities: list[ResolvedEntity] = field(default_factory=list)
    last_query_intent: str | None = None
    last_answer_summary: str | None = None
    updated_at: str | None = None
    # Content version of current_circuit_id when it was set, so a re-parse
    # can be detected and stale last_entities cleared (plan §4.6).
    current_circuit_version: str | None = None


@dataclass
class CircuitQueryContext:
    """Single-query scratch state for one ``query_circuit_data`` call (plan §4.1.4).

    Lives only for the duration of one query — it is NOT persisted. It carries
    the parsed intent, resolved entities, executed tool-call records, evidence
    and missing-info so they don't have to be threaded through every helper as
    loose arguments. After the response is built only the necessary
    ``last_entities`` / ``current_circuit_id`` / ``last_answer_summary`` are
    written back to the session context.
    """

    question: str
    kb_name: str
    session_id: str
    circuit_id: str | None = None
    circuit_scope: CircuitScope | None = None
    session_context: CircuitSessionContext | None = None
    intent: CircuitIntent | None = None
    resolved_entities: list[ResolvedEntity] = field(default_factory=list)
    candidate_entities: list[ResolvedEntity] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    source_status: CircuitSourceStatus | dict[str, Any] | None = None
    # Bounded-agent budgets (plan §6.2); filled from the planned CircuitQueryPlan.
    max_steps: int = 3
    max_tool_calls: int = 6
