"""Strongly-typed contracts for the external agent loop and graph state.

Tool implementations return these Pydantic models internally; only the
deepagents/LangChain ToolMessage boundary may serialize them once. All models
use ``extra="forbid"``, Literal statuses and stable error codes so illegal
states and undeclared fields are rejected at the boundary instead of leaking
into coordinator logic.

This module is also the single source for the clarification policy enums
(表 1) and the brief/field missing-policy equivalence (表 2); clarifier,
writer and agent consumers must share these, never re-approximate them.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

GRAPH_STATE_VERSION = 1

STATE_PAYLOAD_SOFT_LIMIT_BYTES = 256 * 1024
STATE_PAYLOAD_HARD_LIMIT_BYTES = 1024 * 1024

ERROR_STATE_SIZE_EXCEEDED = "graph_state_size_exceeded"
ERROR_STATE_VERSION_INCOMPATIBLE = "graph_state_version_incompatible"

MissingDataPolicy = Literal["mark_tbd", "keep_blank", "block_generation"]
InferencePolicy = Literal["forbid", "allow_labeled", "allow_limited"]

CLARIFICATION_POLICY_MAP: dict[str, dict[str, str]] = {
    "missing_data_policy": {
        "标记未提供": "mark_tbd",
        "保留空白": "keep_blank",
        "停止并提示": "block_generation",
    },
    "inference_policy": {
        "禁止推断": "forbid",
        "允许但必须标注": "allow_labeled",
        "允许有限推断": "allow_limited",
    },
}

BRIEF_TO_FIELD_MISSING_POLICY: dict[str, str] = {
    "block_generation": "block_section",
    "mark_tbd": "mark_tbd",
    "keep_blank": "optional",
}

FIELD_MISSING_POLICY_STRICTNESS: dict[str, int] = {
    "optional": 1,
    "mark_tbd": 2,
    "block_section": 3,
}

INFERENCE_TO_DERIVATION: dict[str, str] = {
    "forbid": "forbid",
    "allow_labeled": "allow_labeled",
    "allow_limited": "allow_limited",
}


def normalize_clarification_policy(question_id: str, raw: Any) -> str | None:
    """Map one clarification answer to its canonical enum; ``None`` when unknown.

    Legacy persisted briefs may hold the original Chinese option text; unknown
    or empty values must never be forwarded to a Writer as a policy.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    mapping = CLARIFICATION_POLICY_MAP.get(str(question_id or "").strip())
    if mapping is None:
        return text
    if text in mapping:
        return mapping[text]
    canonical = {value: value for value in mapping.values()}
    return canonical.get(text)


def effective_missing_policy(brief_missing: str | None, field_missing: str | None) -> str | None:
    """Merge brief and field missing policies; the brief is the global ceiling.

    The effective policy is the stricter of the brief-mapped policy and the
    field policy (表 2): a field may never be looser than the confirmed brief.
    Unknown values cannot widen the result.
    """
    candidates: list[str] = []
    brief_canonical = BRIEF_TO_FIELD_MISSING_POLICY.get(
        str(brief_missing).strip() if brief_missing else ""
    )
    if brief_canonical:
        candidates.append(brief_canonical)
    if field_missing:
        field_text = str(field_missing).strip()
        if field_text in FIELD_MISSING_POLICY_STRICTNESS:
            candidates.append(field_text)
    if not candidates:
        return None
    return max(candidates, key=lambda value: FIELD_MISSING_POLICY_STRICTNESS[value])


class ToolIssue(BaseModel):
    """One structured validation failure returned to the agent."""

    model_config = {"extra": "forbid"}

    code: str
    message: str
    field_id: str | None = None
    retryable: bool = False


class EvidenceRef(BaseModel):
    """Registry-scoped evidence reference; never a raw evidence object."""

    model_config = {"extra": "forbid"}

    evidence_id: str
    registry_run_id: str
    snapshot_id: str
    content_hash: str


class AgentToolResult(BaseModel):
    """Base-shaped result every agent tool returns."""

    model_config = {"extra": "forbid"}

    status: Literal["succeeded", "rejected", "unavailable", "waiting_human"]
    field_id: str
    error_code: str | None = None
    issues: list[ToolIssue] = Field(default_factory=list)


class FieldBriefResult(AgentToolResult):
    """Read-only field contract exposed to a bounded semantic agent."""

    model_config = {"extra": "forbid"}

    field_contract: dict[str, Any] = Field(default_factory=dict)
    evidence_summaries: list[dict[str, Any]] = Field(default_factory=list)
    brief_constraints: dict[str, Any] = Field(default_factory=dict)


class EvidenceRetrievalResult(AgentToolResult):
    model_config = {"extra": "forbid"}

    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    truncated_summary: str = ""


class FieldProposalResult(AgentToolResult):
    model_config = {"extra": "forbid"}

    proposal_hash: str = ""
    validation_status: Literal["supported", "partial", "unsupported", "requires_human"] | None = None
    waiting_human: bool = False
    accepted: bool = False


class MissingFieldResult(AgentToolResult):
    model_config = {"extra": "forbid"}

    missing_policy_applied: Literal["mark_tbd", "keep_blank", "block_generation"] | None = None


def validate_state_payload_size(payload: bytes | str) -> None:
    """Reject graph state that exceeds the serialized size contract."""

    size = len(payload.encode("utf-8") if isinstance(payload, str) else payload)
    if size > STATE_PAYLOAD_HARD_LIMIT_BYTES:
        raise ValueError(f"{ERROR_STATE_SIZE_EXCEEDED}: {size} bytes exceeds hard limit "
                         f"{STATE_PAYLOAD_HARD_LIMIT_BYTES}")
    if size > STATE_PAYLOAD_SOFT_LIMIT_BYTES:
        raise ValueError(f"{ERROR_STATE_SIZE_EXCEEDED}: {size} bytes exceeds soft limit "
                         f"{STATE_PAYLOAD_SOFT_LIMIT_BYTES}")


def validate_graph_state_version(state: dict[str, Any]) -> None:
    version = state.get("graph_state_version")
    if version != GRAPH_STATE_VERSION:
        raise ValueError(f"{ERROR_STATE_VERSION_INCOMPATIBLE}: got {version!r}, "
                         f"expected {GRAPH_STATE_VERSION}")


def serialize_graph_state(state: dict[str, Any]) -> bytes:
    """Serialize with version + size enforcement; used before every checkpoint write."""

    validate_graph_state_version(state)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    validate_state_payload_size(payload)
    return payload
