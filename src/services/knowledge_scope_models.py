"""Immutable, versioned selection contracts; no client supplied identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as data_field
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.pipelines.document_rag.schemas import Evidence, ScopedRetrievalStatus

import src.settings as settings


KnowledgePurpose = Literal["answer", "compare", "compose"]
KnowledgeRole = Literal["primary", "peer", "reference", "standard"]
KnowledgeId = Annotated[int, Field(strict=True, gt=0)]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class KnowledgeSourceInput(_FrozenContract):
    kb_id: KnowledgeId
    role: KnowledgeRole


class KnowledgeSelectionInput(_FrozenContract):
    version: Literal[1] = 1
    purpose: KnowledgePurpose
    sources: tuple[KnowledgeSourceInput, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        if len(self.sources) > settings.MULTI_KB_MAX_SOURCES:
            raise ValueError("knowledge_scope_too_many_sources")
        if len({source.kb_id for source in self.sources}) != len(self.sources):
            raise ValueError("knowledge_scope_duplicate_source")
        roles = [source.role for source in self.sources]
        if self.purpose == "compare":
            if len(roles) < 2 or any(role != "peer" for role in roles):
                raise ValueError("knowledge_scope_compare_requires_peers")
        elif roles.count("primary") != 1 or "peer" in roles:
            raise ValueError("knowledge_scope_requires_one_primary")
        return self


class AuthorizedKnowledgeSource(_FrozenContract):
    kb_id: KnowledgeId
    kb_name: str = Field(min_length=1)
    department_id: str = Field(min_length=1)
    role: KnowledgeRole


class KnowledgeScopeSnapshot(_FrozenContract):
    version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    tenant_key: str = Field(min_length=1)
    actor_user_id: KnowledgeId
    anchor_kb_id: KnowledgeId
    selection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: KnowledgePurpose
    sources: tuple[AuthorizedKnowledgeSource, ...] = Field(min_length=1)
    authorized_at: str = Field(min_length=1)

    def selection(self) -> KnowledgeSelectionInput:
        return KnowledgeSelectionInput(
            version=self.version,
            purpose=self.purpose,
            sources=tuple(KnowledgeSourceInput(kb_id=s.kb_id, role=s.role) for s in self.sources),
        )


def selection_hash(
    selection: KnowledgeSelectionInput,
    *,
    tenant_key: str,
    actor_user_id: int,
    anchor_kb_id: int,
) -> str:
    """Bind stable IDs/roles to the actor, tenant and navigation anchor."""
    payload = {
        "version": selection.version,
        "tenant_key": tenant_key,
        "actor_user_id": actor_user_id,
        "anchor_kb_id": anchor_kb_id,
        "purpose": selection.purpose,
        "sources": [(s.kb_id, s.role) for s in sorted(selection.sources, key=lambda s: s.kb_id)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class KnowledgeSourceRetrievalOutcome:
    kb_id: int
    query: str
    retriever_kind: str
    status: ScopedRetrievalStatus
    evidence: list[Evidence] = data_field(default_factory=list)
    error_code: str | None = None
    diagnostics: dict = data_field(default_factory=dict)
    work_id: str = ""
    target_ids: tuple[str, ...] = ()
    query_id: str = ""


@dataclass
class MultiKnowledgeRetrievalOutcome:
    evidence: list[Evidence]
    source_outcomes: list[KnowledgeSourceRetrievalOutcome]
    selection_hash: str
    coverage: dict[int, bool] = data_field(default_factory=dict)
    partial: bool = False
