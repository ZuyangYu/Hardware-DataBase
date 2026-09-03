"""HTTP-boundary DTOs for the API layer.

Thin transport models only -- business data still crosses layer boundaries as
the dataclasses in src.pipelines.document_rag.schemas / src.core.auth. We do
not redefine business schemas here.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from src.document_authoring.chat_context import DocumentContext, DocumentContextInput
from src.result_exports.models import normalize_export_format


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    username: str
    role: str
    department_id: int | None = None
    department_name: str | None = None


class LoginResponse(BaseModel):
    token: str
    user: UserInfo


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""


# ---------------------------------------------------------------------------
# Knowledge Bases
# ---------------------------------------------------------------------------

class CreateKbRequest(BaseModel):
    name: str


class KbView(BaseModel):
    name: str
    kb_id: int | None = None
    department_id: int | None = None
    department_name: str | None = None
    permission: str | None = None
    registered: bool = True


class FileView(BaseModel):
    id: str
    name: str
    status: str = ""
    processor_kind: str = ""
    dataset_kind: str = ""
    metadata: dict = Field(default_factory=dict)


class UploadAck(BaseModel):
    success_count: int
    total_count: int
    failed_count: int = 0
    skipped_count: int = 0
    status: str
    messages: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Hardware assets and reviewable AI extraction
# ---------------------------------------------------------------------------

class AssetEvidenceView(BaseModel):
    id: int
    file_id: str = ""
    file_name: str = ""
    locator: str = ""
    excerpt: str = ""
    metadata: dict = Field(default_factory=dict)
    created_at: str


class AssetView(BaseModel):
    id: int
    department_id: int
    kb_id: int
    asset_type: str
    name: str
    model: str = ""
    manufacturer: str = ""
    serial_number: str = ""
    version: str = ""
    status: str = "active"
    owner_user_id: int | None = None
    attributes: dict = Field(default_factory=dict)
    evidence_count: int = 0
    created_at: str
    updated_at: str


class AssetDetailView(AssetView):
    evidence: list[AssetEvidenceView] = Field(default_factory=list)


class AssetCandidateView(BaseModel):
    id: int
    kb_name: str
    file_id: str
    file_name: str
    source_kind: str = ""
    extraction_method: str = "rule"
    asset_type: str
    name: str
    model: str = ""
    manufacturer: str = ""
    version: str = ""
    attributes: dict = Field(default_factory=dict)
    evidence_excerpt: str = ""
    evidence_locator: str = ""
    confidence: float = 0
    status: str
    asset_id: int | None = None
    created_at: str
    resolved_at: str | None = None


class GenerateAssetCandidateRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=300)


class AssetSourceLinkView(BaseModel):
    file_id: str
    file_name: str
    file_status: str = ""
    processor_kind: str = ""
    dataset_kind: str = ""
    link_status: Literal["unprocessed", "pending_review", "linked", "ignored"]
    candidate_id: int | None = None
    asset_id: int | None = None
    asset_name: str = ""
    source_category: str = "document_rag"
    extraction_target: str = "document_assets"
    asset_eligible: bool = True


class ConfirmAssetCandidateRequest(BaseModel):
    asset_type: Literal["device", "board", "component", "firmware", "other"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    manufacturer: str | None = Field(default=None, max_length=200)
    serial_number: str | None = Field(default=None, max_length=200)
    version: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=50)
    owner_user_id: int | None = None
    attributes: dict | None = None


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    kb_name: str
    query: str
    # Route also slices [-5:] as defence in depth; the schema cap keeps a
    # misconfigured client from wasting bandwidth on multi-MB history bodies
    # (and blocks DoS-shaped requests before the body is even read).
    history: list[tuple[str, str]] = Field(default_factory=list, max_length=100)
    thread_id: str = ""
    document_context: DocumentContextInput | None = None
    document_flow: bool | None = None


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    kb_name: str
    title: str = "新对话"


class SessionView(BaseModel):
    id: int
    user_id: int
    kb_name: str
    title: str
    created_at: str
    updated_at: str
    document_context: DocumentContext | None = None


class MemoryContextView(BaseModel):
    """A retrieved long-term-memory row shown outside formal evidence."""

    id: str = ""
    scope: str = ""
    status: str = "candidate"
    type: str = ""
    title: str = ""
    content: str = ""
    source_count: int = 0
    has_provenance: bool = False
    score: float | None = None


class MessageView(BaseModel):
    id: int
    session_id: int
    turn_id: str | None = None
    role: str
    content: str
    footer: str = ""
    created_at: str
    edited_at: str | None = None
    redacted: bool = False
    memory_context: list[MemoryContextView] = Field(default_factory=list)
    document_context: DocumentContext | None = None


class AddMessageRequest(BaseModel):
    role: str
    content: str


class EditMessageRequest(BaseModel):
    """Raw-message edit/redaction with §43 provenance protection."""

    content: str | None = Field(default=None, max_length=20_000)
    redact: bool = False
    reason: str = Field(default="", max_length=500)
    request_id: str = Field(default="", max_length=128)


class SessionMemorySummary(BaseModel):
    auto_extract_enabled: bool
    extracted_memories: int


class SessionMemorySettingsUpdate(BaseModel):
    auto_extract: bool


class CreateTurnRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    client_request_id: str | None = Field(default=None, max_length=128)
    # Retained for wire compatibility; the route normalizes KB turns to deep
    # retrieval and general chat bypasses the knowledge-base agent entirely.
    query_mode: Literal["fast", "deep"] = "deep"
    document_context: DocumentContextInput | None = None
    # Explicit document-flow routing: True forces the document flow when the
    # context is valid, False blocks it (and strips document tools from the
    # general toolset); None keeps the legacy intent-keyword fallback.
    document_flow: bool | None = None


class TurnView(BaseModel):
    id: str
    session_id: int
    user_message_id: int
    assistant_message_id: int
    kb_name: str
    query: str
    query_mode: Literal["fast", "deep"] = "fast"
    status: str
    cancel_requested: bool = False
    last_event_seq: int = 0
    answer: str = ""
    summary: dict = Field(default_factory=dict)
    footer: str = ""
    metrics: dict = Field(default_factory=dict)
    error_message: str = ""
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    document_context: DocumentContext | None = None


class TurnStartResponse(BaseModel):
    turn: TurnView
    user_message: MessageView


# ---------------------------------------------------------------------------
# Generic result exports
# ---------------------------------------------------------------------------

ExportFormat = Literal["md", "xlsx", "docx", "pdf", "pptx"]


def _normalize_export_format_input(value: Any) -> str:
    try:
        return normalize_export_format(str(value))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


ExportRequestFormat = Annotated[ExportFormat, BeforeValidator(_normalize_export_format_input)]


class ExportSourceRef(BaseModel):
    kind: Literal["turn", "message", "snapshot"] = "turn"
    id: str = Field(min_length=1, max_length=200)


class CreateExportRequest(BaseModel):
    source_ref: ExportSourceRef
    formats: list[ExportRequestFormat] = Field(min_length=1, max_length=5)
    content_shape: Literal["report", "data", "raw"] = "report"
    title: str | None = Field(default=None, max_length=160)
    client_request_id: str | None = Field(default=None, max_length=128)
    include_citations: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class ExportArtifactView(BaseModel):
    artifact_id: str
    export_job_id: str
    session_id: int
    format: ExportFormat
    filename: str
    mime_type: str
    size: int
    sha256: str
    preview: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    expires_at: str | None = None
    preview_url: str
    download_url: str
    tenant_id: str = "default"
    department_id: str | None = None
    knowledge_base_name: str = ""
    snapshot_id: str = ""
    turn_id: str | None = None
    available: bool = True


class LegacyArtifactView(BaseModel):
    """Unified projection for a template-document artifact."""

    artifact_id: str
    artifact_kind: Literal["document_generation"] = "document_generation"
    work_order_id: str
    stage: str
    format: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    preview: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    preview_url: str
    download_url: str
    tenant_id: str = "default"
    department_id: str | None = None
    knowledge_base_name: str = ""
    available: bool = True


class ExportJobView(BaseModel):
    export_job_id: str
    snapshot_id: str
    session_id: int
    turn_id: str | None = None
    format: ExportFormat
    content_shape: str
    status: str
    attempt: int
    error_message: str = ""
    artifact: ExportArtifactView | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    tenant_id: str = "default"
    department_id: str | None = None
    knowledge_base_name: str = ""


class ExportBatchView(BaseModel):
    snapshot_id: str
    session_id: int
    source_ref: ExportSourceRef
    jobs: list[ExportJobView]


# ---------------------------------------------------------------------------
# Long-term memory governance
# ---------------------------------------------------------------------------

MemoryScope = Literal["project", "user"]
MemoryListScope = Literal["all", "project", "user"]
MemoryStatus = Literal[
    "candidate",
    "verification_pending",
    "supersede_pending",
    "needs_rebuild",
    "verified",
    "superseded",
    "rejected",
    "deleted",
    "provenance_missing",
]

MemoryListStatus = Literal[
    "all",
    "active",
    "candidate",
    "verification_pending",
    "supersede_pending",
    "needs_rebuild",
    "verified",
    "superseded",
    "rejected",
    "deleted",
    "provenance_missing",
]


class MemorySourceView(BaseModel):
    """Sanitized provenance returned by the Catalog-backed service."""

    source_id: str = ""
    source_kind: str = ""
    session_id: int | None = None
    turn_id: str | None = None
    message_id: int | None = None
    content_hash: str = ""
    valid: bool = True


class MemoryView(BaseModel):
    """Public memory representation; namespace and Store keys are excluded."""

    memory_id: str
    revision: int = 0
    status: MemoryStatus
    scope: MemoryScope
    kind: str = ""
    content: dict[str, Any] = Field(default_factory=dict)
    source_count: int = 0
    sources: list[MemorySourceView] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)
    projection_status: str = ""
    created_at: str = ""
    updated_at: str = ""


class MemoryListResponse(BaseModel):
    items: list[MemoryView] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None


class MemoryOperationResponse(BaseModel):
    """Accepted governance operation; projection work may complete async."""

    operation_id: str = ""
    memory_id: str = ""
    status: str = "accepted"
    revision: int | None = None
    message: str = ""


class MemoryActionRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)
    request_id: str = Field(min_length=1, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class MemoryDraftRequest(BaseModel):
    """Validated semantic replacement for an editable Candidate."""

    content: dict[str, Any] = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)
    request_id: str = Field(min_length=1, max_length=128)


class MemoryExtractionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)
    request_id: str = Field(min_length=1, max_length=128)


class VerifyMemoryRequest(MemoryActionRequest):
    evidence_refs: list[str] = Field(min_length=1, max_length=100)


class SupersedeMemoryRequest(MemoryActionRequest):
    successor_memory_id: str | None = Field(default=None, min_length=1, max_length=128)


class UserMemorySettingsView(BaseModel):
    opt_in: bool = False
    policy_version: str = ""
    revoke_generation: int = 0
    updated_at: str = ""


class UserMemorySettingsRequest(BaseModel):
    opt_in: bool
    reason: str = Field(min_length=1, max_length=2_000)
    request_id: str = Field(min_length=1, max_length=128)


class MemoryConsentCreateRequest(BaseModel):
    """Explicit source message selection; identity/scope/policy are server-owned."""

    message_ids: list[int] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2_000)
    request_id: str = Field(min_length=1, max_length=128)


class MemoryConsentView(BaseModel):
    consent_event_id: str
    session_id: int
    source_count: int = 0
    manifest_hash: str = ""
    policy_version: str = ""
    revoke_generation: int = 0
    status: str = "active"
    granted_at: str = ""
    revoked_at: str | None = None


class MemoryConsentListResponse(BaseModel):
    items: list[MemoryConsentView] = Field(default_factory=list)


class RevokeMemoryConsentRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)
    request_id: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# File chunks (parse results)
# ---------------------------------------------------------------------------

class ChunkView(BaseModel):
    index: int
    content: str
    metadata: dict = Field(default_factory=dict)


class ParseResultView(BaseModel):
    document_id: str
    file_name: str
    chunk_count: int
    chunks: list[ChunkView] = Field(default_factory=list)
    backend: str = "ragflow"


# ---------------------------------------------------------------------------
# Parse tasks
# ---------------------------------------------------------------------------

class ParseTaskView(BaseModel):
    id: str
    kb_name: str
    source_path: str = ""
    original_name: str = ""
    source_group: str = ""
    created_by: str = ""
    status: str = ""
    progress: int = 0
    stage: str = ""
    message: str = ""
    result: str = ""
    document_id: str = ""
    created_at: float | None = None
    updated_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None


# ---------------------------------------------------------------------------
# Structured KB data
# ---------------------------------------------------------------------------

class StructuredRowsResponse(BaseModel):
    rows: list[dict] = Field(default_factory=list)


class SpreadsheetLedgerResponse(BaseModel):
    totals: dict = Field(default_factory=dict)
    rows: list[dict] = Field(default_factory=list)


class ExternalConversationListItem(BaseModel):
    conversation_id: str
    title: str = ""
    source_file: str = ""
    origin: str = "upload"
    source_group: str = ""
    turn_count: int = 0
    block_count: int = 0
    status: str = ""
    created_at: str = ""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    summary_generated_at: str = ""


class ExternalConversationsResponse(BaseModel):
    items: list[ExternalConversationListItem] = Field(default_factory=list)
    totals: dict = Field(default_factory=dict)


class ExternalConversationDetailResponse(BaseModel):
    conversation_id: str
    title: str = ""
    source_file: str = ""
    origin: str = "upload"
    source_group: str = ""
    turn_count: int = 0
    block_count: int = 0
    status: str = ""
    created_at: str = ""
    turns: list[dict] = Field(default_factory=list)
    blocks: list[dict] = Field(default_factory=list)
    preview: str = ""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    summary_generated_at: str = ""


class CircuitDesignsResponse(BaseModel):
    designs: list[dict] = Field(default_factory=list)
    failed_logs: list[dict] = Field(default_factory=list)


class CircuitDesignDetailResponse(BaseModel):
    summary: dict = Field(default_factory=dict)
    modules: list[dict] = Field(default_factory=list)
    nets: list[dict] = Field(default_factory=list)
    instances: list[dict] = Field(default_factory=list)
    cross_references: list[dict] = Field(default_factory=list)


class CircuitParseLogResponse(BaseModel):
    exists: bool = False
    path: str = ""
    size: int = 0
    truncated: bool = False
    content: str = ""


class SchematicDesignsResponse(BaseModel):
    designs: list[dict] = Field(default_factory=list)


class SchematicPageResponse(BaseModel):
    design_id: str
    page_number: int
    width: float | None = None
    height: float | None = None
    text: str = ""
    labels: list[dict] = Field(default_factory=list)
    module_regions: list[dict] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    pdf_cache: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Users (admin)
# ---------------------------------------------------------------------------

class AuthUserView(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool = True
    department_id: int | None = None
    department_name: str | None = None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: Literal["user", "dept_admin", "system_admin"] = "user"
    department_id: int | None = None


class SetUserActiveRequest(BaseModel):
    is_active: bool


class ResetPasswordRequest(BaseModel):
    new_password: str


# ---------------------------------------------------------------------------
# Departments (system_admin)
# ---------------------------------------------------------------------------

class DepartmentView(BaseModel):
    id: int
    name: str


class CreateDepartmentRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# KB permissions (admin)
# ---------------------------------------------------------------------------

class KbPermissionView(BaseModel):
    username: str
    role: str
    permission: str
    department_name: str | None = None


class GrantKbPermissionRequest(BaseModel):
    user_id: int
    permission: Literal["read", "write", "admin"] = "read"


class AssignKbRequest(BaseModel):
    department_id: int
    owner_user_id: int | None = None
    source_kb_id: int | None = None


# ---------------------------------------------------------------------------
# Governance (system_admin)
# ---------------------------------------------------------------------------

class KbSummaryView(BaseModel):
    name: str
    kb_id: int | None = None
    department_id: int | None = None
    department_name: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    permission_count: int = 0
    dept_admin_count: int = 0
    registered: bool = False
    physical_exists: bool = False
    created_at: str = ""
    # Document counts joined from governance_stats so the frontend can render
    # the governance panel (and its anomaly flags) from a single endpoint.
    files: int = 0
    failed: int = 0
    parsing: int = 0
    issue_flags: list[str] = Field(default_factory=list)


class KbStatsEntry(BaseModel):
    """Per-KB document counts returned by governance_stats."""
    files: int = 0
    failed: int = 0
    parsing: int = 0


class GovernanceStatsResponse(BaseModel):
    """governance_stats: keyed by KB identity (kb_id:<id> or department:<d>:kb:<name>)."""
    stats: dict[str, KbStatsEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config (system_admin)
# ---------------------------------------------------------------------------

class ConfigResponse(BaseModel):
    """Current runtime config; secrets are redacted."""
    settings: dict[str, object] = Field(default_factory=dict)


class UpdateConfigRequest(BaseModel):
    # Only scalar types are allowed -- config values end up written to .env as
    # strings, so lists/dicts would silently stringify to garbage. Pydantic
    # rejects non-scalar values with 422 before the route sees them.
    settings: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class RagflowHealthResponse(BaseModel):
    reachable: bool
    message: str
    missing_datasets: list[str] = Field(default_factory=list)


class LlmHealthResponse(BaseModel):
    reachable: bool
    message: str
    provider: str = ""


# ---------------------------------------------------------------------------
# Logs (system_admin: global, dept_admin: department-scoped)
# ---------------------------------------------------------------------------

class AuditEventView(BaseModel):
    id: int
    actor_user_id: int | None = None
    actor_username: str = ""
    actor_role: str = ""
    department_id: int | None = None
    action: str = ""
    target_type: str = ""
    target_id: str = ""
    kb_name: str = ""
    success: bool = True
    error_message: str = ""
    metadata_json: str = ""
    created_at: str = ""


class AuditStatsResponse(BaseModel):
    total: int
    breakdown: dict[str, int] = Field(default_factory=dict)
    actions: list[list] = Field(default_factory=list)  # [[action, count], ...]
    daily: list[list] = Field(default_factory=list)   # [[date, count], ...]


class QueryTraceView(BaseModel):
    id: int
    username: str = ""
    department_id: int | None = None
    chat_session_id: int | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    kb_name: str = ""
    original_query: str = ""
    rewritten_query: str = ""
    backend: str = ""
    retriever_type: str = ""
    final_top_k: int | None = None
    latency_ms: int | None = None
    status: str = ""
    error_message: str = ""
    metadata_json: str = ""
    created_at: str = ""
    otel_trace_id: str = ""
    otel_span_id: str = ""
    turn_id: str = ""
    grafana_trace_url: str = ""
    phoenix_trace_url: str = ""


class QueryStatsResponse(BaseModel):
    total: int
    breakdown: dict[str, int] = Field(default_factory=dict)
    failures: list[list] = Field(default_factory=list)  # [[reason, count], ...]


class EvidenceView(BaseModel):
    id: int
    trace_id: int
    rank: int
    file_name: str = ""
    document_id: str = ""
    chunk_id: str = ""
    vector_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    text_preview: str = ""
    metadata_json: str = ""
    created_at: str = ""


# ---------------------------------------------------------------------------
# Evaluation (system_admin only)
# ---------------------------------------------------------------------------

class CreateEvaluationRunRequest(BaseModel):
    """Body for POST /evaluation/runs. Replaces the loose dict so pydantic
    validates types (mode enum, score_enabled bool, sample_ids/tags lists).

    ``kb_id`` is the authoritative binding for new callers. ``kb_name`` is
    retained as a display/redundant consistency field and for the short
    compatibility window where a legacy caller only supplies a unique name.
    """
    dataset_path: str
    kb_id: int | None = None
    kb_name: str | None = None
    mode: Literal["online", "offline"] = "online"
    score_enabled: bool = True
    sample_ids: list[str] | None = None
    tags: list[str] | None = None
    snapshot_path: str | None = None  # required when mode == "offline"


class EvaluationKnowledgeBaseView(BaseModel):
    """A selectable KB identity; no KB content is exposed."""

    kb_id: int
    kb_name: str
    department_id: int | None = None
    department_name: str | None = None
    physical_exists: bool = False
    registered: bool = True


class EvaluationPreflightResponse(BaseModel):
    """Detailed, side-effect-free validation result for a new run."""

    dataset_path: str
    mode: Literal["online", "offline"]
    kb_id: int | None = None
    kb_name: str = ""
    department_id: int | None = None
    dataset_total_count: int = 0
    matched_sample_count: int = 0
    filtered_sample_count: int = 0
    dataset_sample_count: int = 0
    normal_sample_count: int = 0
    expected_denied_sample_count: int = 0
    cohort_fingerprint: str = ""
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    can_create: bool = False


class EvaluationRunListItemView(BaseModel):
    """History row returned without requiring clients to read local files."""

    run_id: str
    status: str = ""
    has_summary: bool = False
    legacy: bool = False
    kb_id: int | None = None
    kb_name: str = ""
    department_id: int | None = None
    created_by: str = ""
    created_at: str = ""
    dataset_path: str = ""
    source_dataset_path: str = ""
    mode: str = ""
    score_enabled: bool = True
    report_path: str = ""
    dataset_sample_count: int = 0
    normal_sample_count: int = 0
    expected_denied_sample_count: int = 0
    cohort_fingerprint: str = ""
    llm_model: str = ""
    embedding_model: str = ""
    snapshot_ownership_verified: bool = False
    validation_warnings: list[str] = Field(default_factory=list)


class EvaluationCompareResponse(BaseModel):
    strict: bool = True
    compatible: bool = False
    warnings: list[str] = Field(default_factory=list)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    current: dict[str, Any] = Field(default_factory=dict)
    baseline: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Document generation
# ---------------------------------------------------------------------------

class TemplateUnitView(BaseModel):
    unit_id: str
    label: str = ""
    writable: bool = False
    blocked_reason: str | None = None


class TemplateSuggestionView(BaseModel):
    semantic_unit_id: str
    label: str
    confidence: float


class TemplateAnalysisView(BaseModel):
    analysis_id: str
    template_version_id: str
    format: str
    status: str
    units: list[TemplateUnitView]
    suggestions: list[TemplateSuggestionView]
    reason_codes: list[str] = Field(default_factory=list)
    auto_activated: bool = False


class TemplateReviewUnitView(TemplateUnitView):
    """Safe unit metadata used only by the human mapping-correction screen."""

    structural_role_hint: str
    candidate_for_auto_fill: bool = False


class TemplateReviewSuggestionView(TemplateSuggestionView):
    target_unit_ids: list[str]
    retrieval_terms: list[str] = Field(default_factory=list)
    value_shape: Literal["scalar", "repeating_table"] = "scalar"
    overwrite_basis: Literal["placeholder", "sample_value"] | None = None


class TemplateAnalysisReviewView(BaseModel):
    analysis_id: str
    template_version_id: str
    content_hash: str
    format: str
    status: str
    units: list[TemplateReviewUnitView]
    suggestions: list[TemplateReviewSuggestionView]
    locked_unit_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class TemplateMappingCorrectionRequest(BaseModel):
    expected_content_hash: str = Field(min_length=64, max_length=64)
    selected_suggestion_ids: list[str] = Field(min_length=1)
    locked_unit_ids: list[str] = Field(default_factory=list)
    comment: str = Field(min_length=1)


class ConfirmTemplateRequest(BaseModel):
    display_name: str
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] | None = None


class CreateWorkOrderRequest(BaseModel):
    template_version_id: str
    document_schema_id: str
    document_schema_version: str
    generation_session_id: str | None = None
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] | None = None


class DeleteDocumentWorkOrderRequest(BaseModel):
    reason: str = ""


class CreateGenerationSessionRequest(BaseModel):
    template_version_id: str = Field(min_length=1)
    purpose: str = ""
    output_policy: dict[str, Any] = Field(default_factory=dict)


class AnswerGenerationSessionRequest(BaseModel):
    question_id: str = Field(min_length=1)
    answer: str = Field(min_length=1)


class IcdResolutionItem(BaseModel):
    exception_id: str
    action: Literal["include", "exclude"]


class IcdResolutionRequest(BaseModel):
    resolutions: list[IcdResolutionItem]
    comment: str = ""


class FeedbackRequest(BaseModel):
    comment: str


class AgentHumanDecisionRequest(BaseModel):
    """One-time approve/reject decision for a pending agent proposal."""

    pending_event_id: str = Field(min_length=1, max_length=200)
    proposal_hash: str = Field(min_length=1, max_length=200)
    decision: Literal["approve", "reject"]
