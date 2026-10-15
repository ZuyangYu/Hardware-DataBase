"""Serializable contracts used by the R3 conflict decision boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class NormalizedClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1)
    field_key: str = Field(min_length=1)
    subject: str = ""
    predicate: str = ""
    value: Any
    display_value: str = ""
    normalized_value: str = Field(min_length=1)
    normalized_unit: str = ""
    condition: str = ""
    scope_key: str = ""
    source_kb_id: int = Field(gt=0)
    source_role: Literal["primary", "peer", "reference", "standard"]
    approval_status: str = ""
    evidence_ids: tuple[str, ...] = ()
    source_content_hash: str = ""
    source_version_id: str = ""
    locator: dict[str, Any] = Field(default_factory=dict)


class ConflictCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    field_key: str = Field(min_length=1)
    subject: str = ""
    predicate: str = ""
    condition: str = ""
    scope_key: str = ""
    claims: tuple[NormalizedClaim, ...] = Field(min_length=2)
    severity: Literal["S0", "S1", "S2", "S3"] = "S2"
    status: Literal["open", "resolved", "dismissed", "blocked"] = "open"
    snapshot_hash: str = ""
    evidence_set_hash: str = ""
    policy_version: str = "conflict-policy-v1"


class ConflictDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: str
    action: Literal["retain_primary", "choose_claim", "report_difference", "integrity_block"]
    requires_human: bool
    blocking: bool
    selected_claim_id: str | None = None
    rationale: str = ""
    policy_version: str = "conflict-policy-v1"
