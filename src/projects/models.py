"""Stable data contracts for project/source governance.

These records deliberately keep business versions separate from processor
outputs.  A parser upgrade can consequently create a new ProcessingArtifact
without silently rewriting the SourceVersion that a reviewed baseline uses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(value: Any) -> str:
    """Return a deterministic SHA-256 digest for an auditable contract."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Project(BaseModel):
    project_id: str
    tenant_id: str = "default"
    department_id: str
    name: str
    product_type: str | None = None
    lifecycle_stage: str | None = None
    current_hardware_revision: str | None = None
    current_bom_revision: str | None = None
    status: Literal["active", "archived", "draft"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectKnowledgeBinding(BaseModel):
    binding_id: str
    project_id: str
    tenant_id: str = "default"
    kb_id: str
    owner_department_id: str
    kb_name_snapshot: str = ""
    binding_type: Literal[
        "project_private", "shared_standard", "component_library", "template_library"
    ]
    priority: int = 0
    allowed_source_roles: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive"] = "active"


class ProjectPrincipalBinding(BaseModel):
    binding_id: str
    tenant_id: str = "default"
    project_id: str
    principal_type: Literal["user", "group", "department", "service_account"]
    principal_id: str
    project_role: Literal["viewer", "author", "reviewer", "approver", "project_admin"]
    capabilities: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: Literal["active", "suspended", "revoked"] = "active"

    def is_valid_at(self, at: datetime | None = None) -> bool:
        at = at or utc_now()
        return (
            self.status == "active"
            and (self.valid_from is None or self.valid_from <= at)
            and (self.valid_to is None or at <= self.valid_to)
        )


class SourceAsset(BaseModel):
    asset_id: str
    tenant_id: str = "default"
    pipeline_record_id: str | None = None
    original_file_name: str
    content_hash: str
    content_kind: str
    parser_kind: str
    processing_status: str
    storage_ref: str | None = None
    data_classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    created_at: datetime = Field(default_factory=utc_now)


class LogicalDocument(BaseModel):
    document_id: str
    tenant_id: str = "default"
    title: str
    document_role: str
    owner_department_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class SourceVersion(BaseModel):
    version_id: str
    tenant_id: str = "default"
    document_id: str
    asset_id: str
    revision: str | None = None
    approval_status: Literal["draft", "reviewing", "approved", "released", "obsolete"] = "draft"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    predecessor_version_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_effective_period(self):
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("effective_from must not be after effective_to")
        return self


class ProcessingArtifact(BaseModel):
    artifact_id: str
    tenant_id: str = "default"
    asset_id: str
    processor_kind: str
    processor_version: str
    backend_locator: dict[str, Any] = Field(default_factory=dict)
    content_fingerprint: str
    status: Literal["processing", "ready", "failed", "superseded"] = "processing"
    created_at: datetime = Field(default_factory=utc_now)


class ProjectSourceBinding(BaseModel):
    binding_id: str
    tenant_id: str = "default"
    project_id: str
    version_id: str
    module_scope: list[str] = Field(default_factory=list)
    usage_type: Literal[
        "project_fact", "shared_reference", "template_only", "historical_reference"
    ]
    status: Literal["active", "inactive", "pending_review"] = "active"


class BaselineItem(BaseModel):
    baseline_item_id: str
    config_item_key: str
    source_role: str
    source_version_id: str
    module_scope: list[str] = Field(default_factory=list)
    product_variant: str | None = None
    required: bool = True


class ProjectBaseline(BaseModel):
    baseline_id: str
    tenant_id: str = "default"
    project_id: str
    name: str
    baseline_version: int = 1
    content_hash: str = ""
    product_variant: str | None = None
    hardware_revisions: dict[str, str] = Field(default_factory=dict)
    items: list[BaselineItem] = Field(default_factory=list)
    effective_at: datetime | None = None
    status: Literal["draft", "approved", "released", "obsolete"] = "draft"
    created_at: datetime = Field(default_factory=utc_now)
    approved_by: str | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def validate_content_hash(self):
        item_ids = [item.baseline_item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("baseline item ids must be unique")
        required_hash = self.computed_content_hash()
        if self.content_hash and self.content_hash != required_hash:
            raise ValueError("baseline content_hash does not match immutable content")
        self.content_hash = required_hash
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(
            {
                "baseline_id": self.baseline_id,
                "tenant_id": self.tenant_id,
                "project_id": self.project_id,
                "name": self.name,
                "baseline_version": self.baseline_version,
                "product_variant": self.product_variant,
                "hardware_revisions": self.hardware_revisions,
                "items": [item.model_dump(mode="json") for item in self.items],
            }
        )


class SourceRegionPolicy(BaseModel):
    region_policy_id: str
    source_version_id: str
    locator: dict[str, Any]
    region_type: Literal[
        "project_fact", "template_instruction", "example", "definition", "change_history",
        "formula_result", "hidden_internal",
    ]
    allowed_evidence_uses: list[str] = Field(default_factory=list)
    decision: Literal["allow", "deny"] = "deny"
    priority: int = 0
    classification_confidence: float | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    processing_artifact_id: str
    policy_version: str = "1"

    @model_validator(mode="after")
    def validate_allow_policy(self):
        if self.decision == "allow" and not self.approved_by:
            raise ValueError("allowing a source region requires an approver")
        return self


class SourceSetSnapshot(BaseModel):
    source_set_snapshot_id: str
    tenant_id: str = "default"
    work_order_id: str
    project_id: str
    baseline_id: str
    baseline_content_hash: str
    baseline_item_ids: list[str]
    source_version_ids: list[str]
    shared_reference_version_ids: list[str] = Field(default_factory=list)
    processing_artifact_ids: list[str] = Field(default_factory=list)
    region_policy_versions: dict[str, str] = Field(default_factory=dict)
    authorization_snapshot_id: str
    content_hash: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_content_hash(self):
        required_hash = canonical_hash(
            self.model_dump(mode="json", exclude={"content_hash", "created_at"})
        )
        if self.content_hash and self.content_hash != required_hash:
            raise ValueError("source-set content_hash does not match frozen inputs")
        self.content_hash = required_hash
        return self


class ProjectSourceCatalogEntry(BaseModel):
    version_id: str
    logical_document: str
    document_role: str
    module_scope: list[str] = Field(default_factory=list)
    revision: str | None = None
    approval_status: str
    effective_from: datetime | None = None
    usage_type: str
    current_for_context: bool
    domains: list[str] = Field(default_factory=list)
    summary: str = ""
