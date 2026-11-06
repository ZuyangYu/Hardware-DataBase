"""Typed agent-turn event protocol.

The durable turn stream is the source of truth for both live SSE consumers and
history replay.  Every event type has one payload model here; emitters build
through :func:`build_agent_event` and the persistence boundary validates with
:func:`validate_agent_event`.  That keeps field drift and missing correlation
IDs out of ``chat_turn_events``.

Wire shape stays ``{"type": <event_type>, "payload": {...}}`` so the existing
SSE event name remains the discriminator.  The payload owns the stable
``item_id``/``call_id`` correlation fields and the schema version.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVENT_SCHEMA_VERSION = 1

TOOL_LABELS = {
    "document_search": "文档检索",
    "document_search_batch": "文档检索",
    "kb_grep": "文本精确检索",
    "evidence_read": "读取候选证据",
    "circuit_search": "电路检索",
    "spreadsheet_row_search": "表格行检索",
    "spreadsheet_cell_lookup": "单元格检索",
    "memory_search": "长期记忆检索",
    "spreadsheet_schema_lookup": "表格SQL结构",
    "spreadsheet_sql_query": "表格SQL查询",
    "list_kb_sources": "读取知识库目录",
    "attachment_list": "附件清单",
    "attachment_search": "附件检索",
    "attachment_read": "附件精读",
    "attachment_table_query": "附件表格查询",
    "attachment_circuit_search": "附件电路查询",
    "attachment_visual_analyze": "附件视觉分析",
    "request_attachment_release": "附件发布申请",
    "attachment_revision_history": "附件修订历史",
    "list_knowledge_sources": "多库目录",
    "wiki_search": "Wiki 检索",
}


def tool_label(tool_name: str) -> str:
    """Human label for a tool; external MCP tools get a server-qualified name."""

    name = str(tool_name or "")
    if name.startswith("mcp__") and "__" in name[5:]:
        slug, tool = name[5:].split("__", 1)
        if slug and tool:
            return f"外部插件 · {slug} · {tool}"
    if name.startswith("skill__") or name == "skill_task_status":
        return "沙箱 Skill"
    return TOOL_LABELS.get(name, name)


class UnknownAgentEventError(ValueError):
    """Raised when code emits an event type that has no contract."""


class AgentEventPayload(BaseModel):
    """Common envelope fields carried in ``chat_turn_events.payload_json``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: int = EVENT_SCHEMA_VERSION
    # Stable logical item identity.  Tool items use ``tool:<call_id>``;
    # stage/plan/chunk items use their own stable keys.
    item_id: str = ""


class _TextPayload(AgentEventPayload):
    text: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "_TextPayload":
        if not self.item_id:
            self.item_id = "answer"
        return self


class DeltaPayload(_TextPayload):
    pass


class NarrationPayload(_TextPayload):
    pass


class StagePayload(AgentEventPayload):
    key: str = Field(min_length=1)
    label: str = ""
    status: str = "running"
    detail: str = ""
    error_code: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "StagePayload":
        if not self.item_id:
            self.item_id = f"stage:{self.key}"
        if not self.label:
            self.label = self.key
        return self


class DegradedPayload(AgentEventPayload):
    stage: str = ""
    reason: str = ""
    component: str = ""
    failure_type: str = ""
    capability: str = ""
    server: str = ""
    tool: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "DegradedPayload":
        if not self.item_id:
            parts = [part for part in (self.stage, self.server, self.tool) if part]
            self.item_id = "degraded:" + ":".join(parts) if parts else "degraded"
        return self


class ToolStartedPayload(AgentEventPayload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    tool_kind: Literal["retrieval", "mcp", "sandbox", "workspace"] = "retrieval"
    label: str = ""
    query: str = ""
    detail: str = ""
    server: str = ""
    tool: str = ""
    transport: str = ""
    skill_ref: str = ""
    task_id: str = ""
    automatic: bool = False

    @model_validator(mode="after")
    def _fill_identity(self) -> "ToolStartedPayload":
        if not self.item_id:
            self.item_id = f"tool:{self.call_id}"
        if not self.label:
            self.label = tool_label(self.tool_name)
        if not self.detail:
            if self.tool_kind == "sandbox":
                self.detail = f"任务 {self.task_id}".strip()
            elif self.tool_kind == "workspace":
                self.detail = f"工作区 {self.query}".strip()
            elif self.tool_kind == "mcp":
                self.detail = f"{self.server}/{self.tool}".strip("/")
            else:
                self.detail = self.query
        return self


class ToolCompletedPayload(AgentEventPayload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    tool_kind: Literal["retrieval", "mcp", "sandbox", "workspace"] = "retrieval"
    label: str = ""
    status: str = "done"
    detail: str = ""
    hit_count: int = 0
    evidence_count: int = 0
    candidate_count: int = 0
    sources: list[str] = Field(default_factory=list)
    citation_numbers: list[int] = Field(default_factory=list)
    empty_reason: str = ""
    latency_ms: int | None = None
    result_bytes: int = 0
    truncated: bool = False
    error: str = ""
    server: str = ""
    tool: str = ""
    transport: str = ""
    skill_ref: str = ""
    task_id: str = ""
    automatic: bool = False

    @model_validator(mode="after")
    def _fill_identity(self) -> "ToolCompletedPayload":
        if not self.item_id:
            self.item_id = f"tool:{self.call_id}"
        if not self.label:
            self.label = tool_label(self.tool_name)
        if not self.detail:
            if self.tool_kind == "sandbox":
                detail = f"任务 {self.task_id} · {self.status}".strip()
            elif self.tool_kind == "workspace":
                detail = f"工作区 {self.status}"
            elif self.tool_kind == "mcp":
                detail = f"{self.server}/{self.tool} · {self.status}".strip("/ ")
            else:
                detail = f"{self.hit_count} 条命中"
            if self.latency_ms is not None:
                detail += f" · {int(self.latency_ms)}ms"
            self.detail = detail
        return self


class BudgetPayload(AgentEventPayload):
    status: str = ""
    exhausted: bool = False
    stopped_reason: str = ""
    tool_calls: int = 0
    evidence_count: int = 0
    partial_answer: bool = False
    synthesis: str = ""
    context_window: dict[str, Any] | None = None
    context_compressed: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _fill_identity(self) -> "BudgetPayload":
        if not self.item_id:
            self.item_id = "budget"
        return self


class VerificationPayload(AgentEventPayload):
    status: str = "unknown"
    removed_citations: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_identity(self) -> "VerificationPayload":
        if not self.item_id:
            self.item_id = "verification"
        return self


class PartialResultPayload(AgentEventPayload):
    reason: str = ""
    synthesized: bool = False
    fallback: str = ""
    evidence_count: int = 0
    answer_chars: int = 0

    @model_validator(mode="after")
    def _fill_identity(self) -> "PartialResultPayload":
        if not self.item_id:
            self.item_id = "partial_result"
        return self


class PartialSynthesisFailedPayload(AgentEventPayload):
    reason: str = ""
    error: str = ""
    evidence_count: int = 0

    @model_validator(mode="after")
    def _fill_identity(self) -> "PartialSynthesisFailedPayload":
        if not self.item_id:
            self.item_id = "partial_result"
        return self


class EvidenceCompressedPayload(AgentEventPayload):
    stage: str = "partial_synthesis"
    evidence_count: int = 0
    compressed_bytes: int = 0
    max_bytes: int = 0
    note: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "EvidenceCompressedPayload":
        if not self.item_id:
            self.item_id = "evidence_compressed"
        return self


class ContextPressurePayload(AgentEventPayload):
    max_prompt_tokens: int = 0
    window_tokens: int = 0
    threshold_tokens: int = 0
    note: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "ContextPressurePayload":
        if not self.item_id:
            self.item_id = "context_pressure"
        return self


class ContextCompactedPayload(AgentEventPayload):
    stage: str = "summarization"
    trigger: str = "auto"
    cutoff_index: int = 0
    dropped_messages: int = 0
    retained_messages: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    max_prompt_tokens: int = 0
    window_tokens: int = 0
    threshold_tokens: int = 0
    note: str = ""
    evidence_count: int = 0
    compressed_bytes: int = 0
    max_bytes: int = 0
    summary_chars: int = 0
    summary_sha256: str = ""
    offload_path: str = ""
    window_id: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "ContextCompactedPayload":
        if not self.item_id:
            identity = self.window_id or self.summary_sha256[:12] or "current"
            self.item_id = f"context_compaction:{identity}"
        return self


class SubagentPayload(AgentEventPayload):
    name: str = "research"
    description: str = ""
    status: str = "running"
    tool_call_id: str = ""
    result_chars: int = 0
    duration_ms: int = 0

    @model_validator(mode="after")
    def _fill_identity(self) -> "SubagentPayload":
        if not self.item_id:
            identity = self.tool_call_id or self.name
            self.item_id = f"subagent:{identity}"
        return self


class DocumentCardPayload(AgentEventPayload):
    card: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_identity(self) -> "DocumentCardPayload":
        if not self.item_id:
            kind = str(self.card.get("kind") or "card")
            reference = str(
                self.card.get("proposal_id")
                or self.card.get("revision_id")
                or self.card.get("artifact_id")
                or ""
            )
            self.item_id = f"document_card:{kind}" + (f":{reference}" if reference else "")
        return self


class AttachmentRevisionPayload(AgentEventPayload):
    status: str = ""
    revision_id: str = ""
    parent_attachment_id: str = ""
    child_attachment_id: str = ""
    parent_content_hash: str = ""
    child_content_hash: str = ""
    operation: str = ""
    stage: str = ""
    release_state: str = ""
    change_summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    download_url: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "AttachmentRevisionPayload":
        if not self.item_id:
            self.item_id = f"attachment_revision:{self.revision_id or self.child_attachment_id}"
        return self


class AttachmentReleasePayload(AgentEventPayload):
    status: str = ""
    revision_id: str = ""
    parent_attachment_id: str = ""
    child_attachment_id: str = ""
    operation: str = ""
    stage: str = ""
    release_state: str = ""
    release_comment: str = ""
    download_url: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "AttachmentReleasePayload":
        if not self.item_id:
            self.item_id = (
                f"attachment_release:{self.child_attachment_id or self.revision_id}"
            )
        return self


class McpWriteProposalPayload(AgentEventPayload):
    card: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_identity(self) -> "McpWriteProposalPayload":
        if not self.item_id:
            proposal_id = str(self.card.get("proposal_id") or "")
            self.item_id = f"mcp_write_proposal:{proposal_id}" if proposal_id else "mcp_write_proposal"
        return self


class TurnStartPayload(AgentEventPayload):
    query: str = ""
    query_mode: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "TurnStartPayload":
        if not self.item_id:
            self.item_id = "turn"
        return self


class TurnEndPayload(AgentEventPayload):
    status: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "TurnEndPayload":
        if not self.item_id:
            self.item_id = "turn"
        return self


class AttachmentStatePayload(AgentEventPayload):
    attachment_states: dict[str, str] = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)
    error_code: str = ""

    @model_validator(mode="after")
    def _fill_identity(self) -> "AttachmentStatePayload":
        if not self.item_id:
            state = self.error_code or "ready"
            self.item_id = f"attachment:{state}"
        return self


class TimeoutPayload(AgentEventPayload):
    error_code: str = ""
    wait_seconds: int = 0

    @model_validator(mode="after")
    def _fill_identity(self) -> "TimeoutPayload":
        if not self.item_id:
            self.item_id = f"timeout:{self.error_code or 'turn'}"
        return self


class ErrorPayload(AgentEventPayload):
    message: str = ""
    cancelled: bool = False

    @model_validator(mode="after")
    def _fill_identity(self) -> "ErrorPayload":
        if not self.item_id:
            self.item_id = "turn"
        return self


class DonePayload(AgentEventPayload):
    answer: str = ""
    summary: dict[str, Any] | None = None
    footer: str = ""
    turn: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _fill_identity(self) -> "DonePayload":
        if not self.item_id:
            self.item_id = "turn"
        return self


EVENT_MODELS: dict[str, type[AgentEventPayload]] = {
    "delta": DeltaPayload,
    "narration": NarrationPayload,
    "stage": StagePayload,
    "degraded": DegradedPayload,
    "tool_started": ToolStartedPayload,
    "tool_result": ToolCompletedPayload,
    "budget": BudgetPayload,
    "budget_exhausted": BudgetPayload,
    "verification": VerificationPayload,
    "partial_result": PartialResultPayload,
    "partial_synthesis_failed": PartialSynthesisFailedPayload,
    "evidence_compressed": EvidenceCompressedPayload,
    "context_pressure": ContextPressurePayload,
    "context_compacted": ContextCompactedPayload,
    "subagent_started": SubagentPayload,
    "subagent_completed": SubagentPayload,
    "document_card": DocumentCardPayload,
    "attachment_revision": AttachmentRevisionPayload,
    "attachment_release": AttachmentReleasePayload,
    "mcp_write_proposal": McpWriteProposalPayload,
    "turn_start": TurnStartPayload,
    "turn_end": TurnEndPayload,
    "attachment_wait_timeout": TimeoutPayload,
    "attachment_ready": AttachmentStatePayload,
    "attachment_failed": AttachmentStatePayload,
    "chat_queue_timeout": TimeoutPayload,
    "error": ErrorPayload,
    "done": DonePayload,
}


def validate_agent_event(
    event_type: str,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate and normalize one payload for persistence/broadcast.

    Unknown event types fail closed: adding an event means adding a contract,
    not silently writing an untyped dict into the durable stream.
    """

    normalized_type = str(event_type or "").strip()
    model = EVENT_MODELS.get(normalized_type)
    if model is None:
        raise UnknownAgentEventError(
            f"unknown agent event type {normalized_type!r}; add a payload contract"
        )
    event = model.model_validate(dict(payload or {}))
    return event.model_dump(mode="json", exclude_none=True)


def build_agent_event(event_type: str, **payload: Any) -> dict[str, Any]:
    """Build the canonical ``{"type", "payload"}`` envelope."""

    return {
        "type": str(event_type or "").strip(),
        "payload": validate_agent_event(event_type, payload),
    }


def validate_event_envelope(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a runner-style ``{"type", "payload"}`` dict."""

    event_type = str(event.get("type") or "").strip()
    payload = event.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        raise ValueError(f"{event_type or 'agent'} payload must be an object")
    return {
        "type": event_type,
        "payload": validate_agent_event(event_type, payload),
    }


__all__ = [
    "EVENT_MODELS",
    "EVENT_SCHEMA_VERSION",
    "AgentEventPayload",
    "UnknownAgentEventError",
    "build_agent_event",
    "tool_label",
    "validate_agent_event",
    "validate_event_envelope",
]
