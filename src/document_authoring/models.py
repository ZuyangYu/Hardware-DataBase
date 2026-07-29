"""Format-neutral P2a document contracts.

The semantic document schema, physical template schema and renderer policy are
kept separate so a review item cannot gain a new write location at runtime.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.agents.claim_evidence import InformationRequirement
from src.document_authoring.icd_scope_decision import IcdScopeDecision
from src.document_authoring.template_analysis import (  # noqa: F401
    DocxRegionSchema,
    workbook_cell_coordinates,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TemplateSecurityReport(BaseModel):
    report_id: str
    content_hash: str
    format: Literal["xlsm", "xlsx", "docx", "markdown"]
    parts: dict[str, str] = Field(default_factory=dict)
    relationship_parts: dict[str, str] = Field(default_factory=dict)
    macro_parts: list[str] = Field(default_factory=list)
    external_links: list[str] = Field(default_factory=list)
    embedded_parts: list[str] = Field(default_factory=list)
    active_content_status: Literal["clean", "requires_approval", "quarantined"] = "clean"
    created_at: datetime = Field(default_factory=utc_now)


class TemplateSanitizationReport(BaseModel):
    """Audit record linking an immutable uploaded source to its safe derivative."""

    template_version_id: str
    source_format: Literal["xlsm", "xlsx", "docx"]
    source_content_hash: str
    source_storage_ref: str
    sanitized_format: Literal["xlsx", "docx"]
    sanitized_content_hash: str
    removed_parts: list[str] = Field(default_factory=list)
    removed_relationships: list[str] = Field(default_factory=list)
    status: Literal["sanitized", "failed"]
    created_at: datetime = Field(default_factory=utc_now)


class RendererPolicy(BaseModel):
    renderer_policy_id: str
    version: str = "1"
    macro_policy: Literal["preserve", "strip", "quarantine"] = "quarantine"
    external_link_policy: Literal["preserve", "strip", "quarantine"] = "strip"
    embedded_object_policy: Literal["preserve", "strip", "quarantine"] = "quarantine"
    allowlisted_template_hashes: list[str] = Field(default_factory=list)
    allowed_changed_parts: list[str] = Field(default_factory=lambda: ["xl/worksheets/", "word/document.xml"])
    reject_formula_like_text: bool = True


class TemplateVersion(BaseModel):
    template_version_id: str
    template_id: str
    format: Literal["xlsm", "xlsx", "docx", "markdown"]
    content_hash: str
    template_schema_id: str
    template_schema_version: str
    renderer_policy_id: str
    status: Literal["draft", "approved", "obsolete"] = "draft"
    security_report_id: str | None = None
    storage_ref: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class LegacyTemplateClaim(BaseModel):
    """A template's old/example value that is forbidden as project evidence."""

    claim_id: str
    text: str
    locator: dict[str, Any] = Field(default_factory=dict)
    detected_entities: list[str] = Field(default_factory=list)
    legacy_value_kind: Literal[
        "project_fact", "review_result", "workflow_state", "person_or_signature", "example_text",
    ] = "project_fact"
    prohibited_as_project_evidence: bool = True


class WorkbookRegionSchema(BaseModel):
    region_id: str
    sheet_name: str
    locator: dict[str, Any]
    role: Literal[
        "locked_template", "project_metadata", "evidence_derived", "semantic_draft",
        "formula", "human_input", "human_approval", "legacy_example",
    ]
    write_policy: Literal["never", "deterministic_only", "validated_draft", "human_only"]
    preserve_formula: bool = False
    value_type: str | None = None
    expected_value_hash: str | None = None
    allow_nonempty_overwrite: bool = False

    @model_validator(mode="after")
    def validate_locator(self):
        cell = self.locator.get("cell")
        if not cell:
            raise ValueError("P2a workbook regions require an explicit cell locator")
        workbook_cell_coordinates(str(cell))
        if self.role == "formula" and self.write_policy == "never":
            return self
        if self.preserve_formula and self.write_policy != "never":
            raise ValueError("formula-preserving regions may not be writable")
        return self


class TemplateUnitBinding(BaseModel):
    binding_id: str
    template_schema_id: str
    template_schema_version: str
    semantic_unit_type: Literal["section", "field", "review_item"]
    semantic_unit_id: str
    target_region_ids: list[str]
    render_transform_id: str | None = None


class DocumentFieldSchema(BaseModel):
    field_id: str
    label: str
    description: str = ""
    required: bool = True
    value_type: str = "text"
    required_capabilities: list[str] = Field(default_factory=list)
    preferred_source_roles: list[str] = Field(default_factory=list)
    retrieval_policy_id: str
    query_terms: list[str] = Field(default_factory=list)
    subject_aliases: list[str] = Field(default_factory=list)
    verification_policy_id: str
    value_normalizer_id: str | None = None
    allow_derivation: bool = False
    missing_policy: Literal["mark_tbd", "block_section", "optional"] = "mark_tbd"
    authoring_policy: Literal["deterministic", "managed_writer", "external_agent_draft", "human_only"] = "managed_writer"


class ReviewItemSchema(BaseModel):
    review_item_id: str
    label: str
    applicability_policy_id: str = "always"
    evaluation_mode: Literal["deterministic_auto", "semantic_assisted", "human_required"]
    required_capabilities: list[str] = Field(default_factory=list)
    required_source_roles: list[str] = Field(default_factory=list)
    retrieval_rule_id: str
    deterministic_rule_id: str | None = None
    pass_policy_id: str
    severity: Literal["info", "warning", "major", "critical"] = "warning"

    @model_validator(mode="after")
    def deterministic_items_need_rule(self):
        if self.evaluation_mode == "deterministic_auto" and not self.deterministic_rule_id:
            raise ValueError("deterministic review items require deterministic_rule_id")
        return self


class DocumentSchema(BaseModel):
    document_schema_id: str
    version: str
    document_type: str
    fields: list[DocumentFieldSchema] = Field(default_factory=list)
    review_items: list[ReviewItemSchema] = Field(default_factory=list)
    status: Literal["draft", "approved", "obsolete"] = "draft"
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] = "deterministic_only"
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_unit_ids(self):
        unit_ids = [item.field_id for item in self.fields] + [item.review_item_id for item in self.review_items]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("document schema unit ids must be unique")
        if self.execution_mode == "deterministic_only":
            unsupported = [item.review_item_id for item in self.review_items if item.evaluation_mode != "deterministic_auto"]
            if unsupported:
                raise ValueError(f"deterministic-only schema has non-deterministic items: {unsupported}")
        return self


class DeterministicRuleSpec(BaseModel):
    rule_id: str
    rule_version: str
    operation: Literal[
        "exact_match", "set_compare", "range_check", "regex_check", "existence_check",
        "count_compare", "derived_calculation",
    ]
    input_requirements: list[str]
    capability: str
    approved_operation_name: str
    parameter_bindings: dict[str, Any] = Field(default_factory=dict)
    expected_value_type: str
    normalizer_id: str | None = None
    unit_policy_id: str | None = None
    tolerance: dict[str, Any] | None = None
    expected_cardinality: dict[str, int] | None = None
    missing_behavior: Literal["tbd", "insufficient_evidence", "block"] = "insufficient_evidence"
    conflict_behavior: Literal["report", "block"] = "block"
    implementation_version: str

    @model_validator(mode="after")
    def validate_operation_allowlist(self):
        if self.approved_operation_name != self.operation:
            raise ValueError("approved operation name must match the registered operation")
        return self


class KnowledgeBaseSourceSnapshot(BaseModel):
    source_set_snapshot_id: str
    tenant_id: str
    knowledge_base_name: str
    source_names: list[str]
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)
    content_hash: str = ""

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        knowledge_base_name: str,
        source_names: list[str],
        created_by: str,
    ) -> KnowledgeBaseSourceSnapshot:
        return cls(
            source_set_snapshot_id=f"kb-source-set-{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            knowledge_base_name=knowledge_base_name,
            source_names=source_names,
            created_by=created_by,
        )

    @model_validator(mode="after")
    def bind_content_hash(self):
        expected = content_hash(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash and self.content_hash != expected:
            raise ValueError("knowledge-base source snapshot hash does not match contents")
        self.content_hash = expected
        return self


class DocumentWorkOrder(BaseModel):
    work_order_id: str
    tenant_id: str = "default"
    scope_type: Literal["project", "knowledge_base"] = "project"
    knowledge_base_name: str | None = None
    project_id: str | None
    baseline_id: str | None
    baseline_content_hash: str
    source_set_snapshot_id: str
    template_version_id: str
    document_schema_id: str
    document_schema_version: str
    template_schema_id: str
    template_schema_version: str
    retrieval_policy_version: str
    renderer_policy_version: str
    target_format: Literal["xlsm", "xlsx", "markdown", "docx"]
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"]
    harness_policy_id: str | None = None
    harness_policy_version: str | None = None
    unit_statuses: dict[str, str] = Field(default_factory=dict)
    status: Literal[
        "planned", "retrieving", "ready_to_draft", "drafting", "waiting_human_input",
        "validating", "waiting_human_approval", "ready_to_render", "rendering", "blocked",
        "complete", "cancelled",
    ] = "planned"
    project_snapshot_version: str | None = None
    evidence_matrix_id: str | None = None
    validation_report_id: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    input_fingerprint: str = ""
    idempotency_key: str | None = None
    run_manifest_id: str | None = None
    lock_version: int = 0

    @model_validator(mode="after")
    def calculate_input_fingerprint(self):
        if self.scope_type == "project":
            if not self.project_id or not self.baseline_id:
                raise ValueError("project-scoped work orders require project_id and baseline_id")
            if self.knowledge_base_name is not None:
                raise ValueError("project-scoped work orders cannot name a knowledge base")
        else:
            if not self.knowledge_base_name:
                raise ValueError("knowledge-base work orders require knowledge_base_name")
            if self.project_id is not None or self.baseline_id is not None:
                raise ValueError("knowledge-base work orders cannot name a project or baseline")
            if self.baseline_content_hash:
                raise ValueError("knowledge-base work orders cannot bind a baseline content hash")
        if self.execution_mode == "internal_harness" and (
            not self.harness_policy_id or not self.harness_policy_version
        ):
            raise ValueError("internal-harness work orders require a frozen HarnessPolicy version")
        excluded = {
            "input_fingerprint", "created_at", "updated_at", "lock_version", "status", "unit_statuses",
            "evidence_matrix_id", "validation_report_id", "run_manifest_id",
        }
        if self.scope_type == "project":
            # Preserve fingerprints from persisted project work orders created
            # before the explicit scope fields existed.
            excluded.update({"scope_type", "knowledge_base_name"})
        expected = content_hash(self.model_dump(mode="json", exclude=excluded))
        if self.input_fingerprint and self.input_fingerprint != expected:
            raise ValueError("work order input_fingerprint does not match frozen inputs")
        self.input_fingerprint = expected
        return self


class IcdScopeResolution(BaseModel):
    """One human action for a persisted ICD scope exception."""

    exception_id: str
    action: str
    actor_id: str
    resolved_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_action(self):
        if not self.exception_id.strip() or not self.action.strip():
            raise ValueError("ICD scope resolutions require an exception id and action")
        return self


class IcdScopeReview(BaseModel):
    """Hash-bound, one-batch review of an ICD scope decision."""

    work_order_id: str
    decision: IcdScopeDecision
    decision_content_hash: str = ""
    source_snapshot_hash: str
    status: Literal["pending", "frozen"] = "pending"
    resolutions: list[IcdScopeResolution] = Field(default_factory=list)
    resolution_comment: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    frozen_at: datetime | None = None

    @property
    def exceptions(self):
        return self.decision.exceptions

    @property
    def pending_count(self) -> int:
        resolved_ids = {resolution.exception_id for resolution in self.resolutions}
        return sum(
            exception.exception_id not in resolved_ids
            for exception in self.decision.exceptions
        )

    @model_validator(mode="after")
    def validate_frozen_scope(self):
        expected_hash = content_hash(self.decision)
        if self.decision_content_hash and self.decision_content_hash != expected_hash:
            raise ValueError("ICD scope decision hash does not match contents")
        self.decision_content_hash = expected_hash
        if not self.source_snapshot_hash:
            raise ValueError("ICD scope review requires a source snapshot hash")
        exception_ids = [exception.exception_id for exception in self.decision.exceptions]
        resolved_ids = [resolution.exception_id for resolution in self.resolutions]
        if len(resolved_ids) != len(set(resolved_ids)):
            raise ValueError("ICD scope exceptions may only be resolved once")
        if set(resolved_ids) - set(exception_ids):
            raise ValueError("ICD scope resolution references an unknown exception")
        if self.status == "pending" and self.resolutions:
            raise ValueError("pending ICD scope review may not contain resolutions")
        if self.status == "frozen":
            if set(resolved_ids) != set(exception_ids):
                raise ValueError("frozen ICD scope review must resolve every exception")
            if self.frozen_at is None:
                self.frozen_at = utc_now()
        return self


class HarnessPolicy(BaseModel):
    """Server-owned budget and allowlist for an internal authoring run."""

    harness_policy_id: str
    version: str
    max_steps: int = 40
    max_retrieval_rounds: int = 2
    max_retrieval_attempts_per_unit: int = 2
    max_units_per_run: int = 20
    max_retries: int = 1
    lease_seconds: int = 60
    max_query_rewrite_rounds: int = 1
    # Adaptive recovery (stage 5): after attempts are exhausted on an empty
    # success, one balanced-route retrieve may recover low-confidence evidence.
    # Zero disables it even when the tool is allowlisted; the tool + budget
    # form a double switch, mirroring rewrite_query + max_query_rewrite_rounds.
    max_adaptive_recovery_rounds: int = 0
    allowed_tools: list[str] = Field(default_factory=lambda: [
        "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
        "detect_template_contamination", "validate_cross_unit", "rewrite_query",
        "rerank_evidence",
    ])
    writer_provider_id: str = "managed"
    prompt_version: str = "1"
    status: Literal["draft", "approved", "obsolete"] = "draft"

    @model_validator(mode="after")
    def validate_budget(self):
        if min(
            self.max_steps,
            self.max_retrieval_rounds,
            self.max_retrieval_attempts_per_unit,
            self.max_units_per_run,
            self.lease_seconds,
            self.max_query_rewrite_rounds,
        ) < 1:
            raise ValueError("harness policy budgets must be positive")
        if self.max_retries < 0:
            raise ValueError("harness policy max_retries cannot be negative")
        if self.max_adaptive_recovery_rounds < 0:
            raise ValueError("harness policy max_adaptive_recovery_rounds cannot be negative")
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("harness policy tools must be unique")
        return self


class AuthoringRunManifest(BaseModel):
    run_manifest_id: str
    work_order_id: str
    harness_policy_id: str
    harness_policy_version: str
    writer_provider_id: str
    prompt_version: str
    source_set_snapshot_id: str
    input_fingerprint: str
    source_set_snapshot_hash: str = ""
    baseline_content_hash: str = ""
    source_version_ids: list[str] = Field(default_factory=list)
    processing_artifact_ids: list[str] = Field(default_factory=list)
    region_policy_versions: dict[str, str] = Field(default_factory=dict)
    evidence_content_hashes: dict[str, str] = Field(default_factory=dict)
    template_content_hash: str = ""
    document_schema_hash: str = ""
    template_schema_hash: str = ""
    retrieval_policy_hash: str = ""
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] = "internal_harness"
    tool_policy_hash: str = ""
    max_steps: int | None = None
    max_retrieval_rounds: int | None = None
    max_retrieval_attempts_per_unit: int | None = None
    validator_version: str = "p2b-1"
    renderer_version: str = "p2a-1"
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class HarnessRun(BaseModel):
    harness_run_id: str
    work_order_id: str
    run_manifest_id: str
    status: Literal[
        "planned", "queued", "running", "paused", "waiting_human", "retrying", "failed", "completed", "cancelled",
    ] = "planned"
    checkpoint_id: str | None = None
    current_node: str = "initialize"
    step_count: int = 0
    retrieval_round_count: int = 0
    retry_count: int = 0
    max_retries: int = 0
    lease_owner: str | None = None
    fencing_token: int = 0
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    last_error_code: str | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HarnessCheckpoint(BaseModel):
    """Durable progress marker used by P2c pause/retry/recovery paths."""

    checkpoint_id: str
    harness_run_id: str
    work_order_id: str
    input_fingerprint: str
    source_set_snapshot_id: str
    fencing_token: int
    status: Literal["active", "paused", "waiting_human", "failed", "completed", "cancelled"] = "active"
    current_node: str = "initialize"
    step_count: int = 0
    retrieval_round_count: int = 0
    unit_statuses: dict[str, str] = Field(default_factory=dict)
    evidence_matrix_hash: str | None = None
    draft_ids: list[str] = Field(default_factory=list)
    pending_human_event: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NodeExecutionReceipt(BaseModel):
    """Idempotency record for a side-effecting Harness node."""

    receipt_id: str
    harness_run_id: str
    node_name: str
    unit_id: str
    input_fingerprint: str
    fencing_token: int
    status: Literal["started", "committed", "failed"] = "started"
    output_hash: str | None = None
    output_payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    committed_at: datetime | None = None


class DraftAssertion(BaseModel):
    assertion_id: str
    text: str
    claim_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    value: Any | None = None
    consistency_key: str | None = None
    assertion_kind: Literal[
        "confirmed_fact", "document_statement", "derived_observation", "inference",
        "missing_information", "conflict",
    ] = "document_statement"


class DocumentUnitDraft(BaseModel):
    unit_id: str
    run_id: str
    generated_by: Literal["managed_writer", "external_agent", "deterministic_rule"]
    content: str | None = None
    proposed_value: Any | None = None
    assertions: list[DraftAssertion] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    proposed_status: str | None = None
    validation_status: Literal["pending", "supported", "partial", "unsupported", "requires_human"] = "pending"
    validation_notes: list[str] = Field(default_factory=list)


class EvidenceMatrixRow(BaseModel):
    section_id: str = ""
    field_id: str | None = None
    review_item_id: str | None = None
    requirement: InformationRequirement
    evidence_ids: list[str] = Field(default_factory=list)
    coverage_status: str = "unsearched"
    normalized_value: Any | None = None
    display_value: str | None = None
    derivation: dict[str, Any] | None = None


class DeterministicReviewResult(BaseModel):
    review_item_id: str
    rule_id: str
    status: Literal[
        "passed", "failed", "not_applicable_pending_approval", "insufficient_evidence",
        "requires_human", "retrieval_failed", "conflicting",
    ]
    display_value: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class WorkbookFill(BaseModel):
    region_id: str
    value: str
    semantic_unit_id: str


class WorkbookFillPlan(BaseModel):
    template_version_id: str
    fills: list[WorkbookFill] = Field(default_factory=list)


class DocxFill(BaseModel):
    """A value destined for one pre-registered DOCX region."""

    region_id: str
    value: str
    semantic_unit_id: str


class DocxFillPlan(BaseModel):
    """A hash-bound allowlist of DOCX paragraph, table-cell, or control fills."""

    template_version_id: str
    fills: list[DocxFill] = Field(default_factory=list)


class ValidationReport(BaseModel):
    validation_report_id: str
    work_order_id: str
    status: Literal["passed", "failed", "requires_human"]
    issues: list[dict[str, Any]] = Field(default_factory=list)
    evidence_matrix_hash: str
    renderer_manifest_hash: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def content_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class DocumentArtifact(BaseModel):
    artifact_id: str
    tenant_id: str = "default"
    work_order_id: str
    run_id: str
    stage: Literal["draft_preview", "review_candidate", "approved_release"]
    validity_status: Literal["current", "artifact_stale", "revalidation_required"] = "current"
    policy_status: Literal["active", "policy_obsolete"] = "active"
    access_status: Literal["granted", "access_revoked"] = "granted"
    regeneration_status: Literal["not_needed", "recommended"] = "not_needed"
    status_reasons: list[dict[str, Any]] = Field(default_factory=list)
    content_hash: str
    approval_subject_hash: str | None = None
    parent_artifact_id: str | None = None
    validation_report_id: str
    approval_event_ids: list[str] = Field(default_factory=list)
    integrity_manifest_id: str
    idempotency_fingerprint: str = ""
    storage_ref: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    released_at: datetime | None = None


class DocumentHumanEvent(BaseModel):
    event_id: str
    work_order_id: str
    run_id: str
    artifact_id: str
    unit_id: str
    event_type: Literal[
        "provide_value", "approve_na", "confirm_result", "assign_owner", "close_action", "approve", "sign", "feedback",
    ]
    event_schema_version: str = "1"
    previous_value_hash: str | None = None
    subject_artifact_content_hash: str
    approval_subject_hash: str | None = None
    value: Any = None
    actor_id: str
    actor_role: str
    comment: str = ""
    idempotency_fingerprint: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class DocumentOutboxEvent(BaseModel):
    """Delivery record for document-domain state transitions.

    Payloads deliberately contain IDs, hashes and statuses only. Source text,
    templates and model prompts remain in their governed stores.
    """

    event_id: str
    event_key: str
    aggregate_type: Literal["work_order", "artifact", "human_event"]
    aggregate_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "delivered", "failed"] = "pending"
    delivery_attempts: int = 0
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    delivered_at: datetime | None = None
