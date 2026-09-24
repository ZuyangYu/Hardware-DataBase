"""Pydantic schemas for semantic memory and consent manifests.

The first group of models is intentionally free of catalog/governance fields.
Those fields belong to the worker and canonical catalog, not to LLM output.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MEMORY_SCHEMA_VERSION = "1"
MemoryType = Literal["decision", "fact", "constraint", "issue", "experience", "todo", "context"]


class _SemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectMemory(_SemanticModel):
    """A self-contained candidate engineering memory."""

    memory_type: MemoryType
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    subject: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None

    @field_validator("title", "content", "subject")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class UserMemory(_SemanticModel):
    """An explicitly expressed personal usage preference, always a candidate."""

    memory_type: Literal["preference"]
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)

    @field_validator("title", "content")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class EngineeringEpisode(_SemanticModel):
    """An engineering problem, approach, outcome, and reusable lesson."""

    problem: str = Field(min_length=1)
    context: str = Field(min_length=1)
    approach: str = Field(min_length=1)
    result: str = Field(min_length=1)
    lessons: list[str] = Field(default_factory=list)

    @field_validator("problem", "context", "approach", "result")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class MemoryConsentSourceItem(BaseModel):
    """One server-derived, ordered item in an immutable consent manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    turn_id: str = Field(min_length=1)
    message_id: int = Field(ge=0)
    role: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)

    @field_validator("turn_id", "role", "content_hash")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()


class MemoryConsentManifest(BaseModel):
    """Frozen source window; later changes may only revoke the event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[MemoryConsentSourceItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order(self) -> MemoryConsentManifest:
        ordinals = [item.ordinal for item in self.items]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("manifest ordinals must be contiguous and ordered")
        if len({(item.turn_id, item.message_id) for item in self.items}) != len(self.items):
            raise ValueError("manifest source items must be unique")
        return self


class MemoryConsentEvent(BaseModel):
    """Server-created consent record and its immutable source manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    consent_event_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    message_id: int = Field(ge=0)
    consent_kind: Literal["user_memory_extract"]
    policy_version: str = Field(min_length=1)
    consent_revoke_generation: int = Field(ge=0)
    granted_at: datetime
    revoked_at: datetime | None = None
    authorized_start_turn_id: str = Field(min_length=1)
    authorized_start_message_id: int = Field(ge=0)
    authorized_end_turn_id: str = Field(min_length=1)
    authorized_end_message_id: int = Field(ge=0)
    authorized_source_hash: str = Field(min_length=1)
    manifest: MemoryConsentManifest

    @field_validator(
        "consent_event_id", "user_id", "session_id", "turn_id", "policy_version",
        "authorized_start_turn_id", "authorized_end_turn_id", "authorized_source_hash",
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def boundaries_match_manifest(self) -> MemoryConsentEvent:
        first, last = self.manifest.items[0], self.manifest.items[-1]
        if (first.turn_id, first.message_id) != (
            self.authorized_start_turn_id,
            self.authorized_start_message_id,
        ) or (last.turn_id, last.message_id) != (
            self.authorized_end_turn_id,
            self.authorized_end_message_id,
        ):
            raise ValueError("authorized boundaries must match manifest")
        if self.authorized_source_hash != manifest_hash(self.manifest):
            raise ValueError("authorized_source_hash does not match manifest")
        return self


def _canonical_value(value: Any) -> Any:
    """Convert values to deterministic JSON-compatible values.

    Nulls are retained, arrays retain order, object keys are sorted, and
    finite numbers use JSON's normal shortest representation (negative zero
    is normalized to zero).
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not support non-finite numbers")
        if value == 0:
            return 0
        return int(value) if value.is_integer() else value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


def canonical_serialize(value: Any, *, schema_version: str = MEMORY_SCHEMA_VERSION) -> bytes:
    """Serialize a value with schema version included in the hashed payload."""
    if not schema_version.strip():
        raise ValueError("schema_version must not be blank")
    payload = {"schema_version": schema_version, "value": _canonical_value(value)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def normalized_content(value: Any, *, schema_version: str = MEMORY_SCHEMA_VERSION) -> bytes:
    """Return the canonical UTF-8 representation used by catalog and store."""
    return canonical_serialize(value, schema_version=schema_version)


def content_hash(value: Any, *, schema_version: str = MEMORY_SCHEMA_VERSION) -> str:
    """Hash canonical content using SHA-256."""
    return hashlib.sha256(normalized_content(value, schema_version=schema_version)).hexdigest()


def manifest_hash(manifest: MemoryConsentManifest, *, schema_version: str = MEMORY_SCHEMA_VERSION) -> str:
    """Hash only the ordered consent source manifest."""
    return content_hash(manifest, schema_version=schema_version)
