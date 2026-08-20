"""HTTP-boundary DTOs for the API layer.

Thin transport models only -- business data still crosses layer boundaries as
the dataclasses in src.pipelines.document_rag.schemas / src.core.auth. We do
not redefine business schemas here.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class MessageView(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    footer: str = ""
    created_at: str


class AddMessageRequest(BaseModel):
    role: str
    content: str


class CreateTurnRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    client_request_id: str | None = Field(default=None, max_length=128)
    # Retained for wire compatibility; the route normalizes KB turns to deep
    # retrieval and general chat bypasses the knowledge-base agent entirely.
    query_mode: Literal["fast", "deep"] = "deep"


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


class TurnStartResponse(BaseModel):
    turn: TurnView
    user_message: MessageView


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
    validates types (mode enum, score_enabled bool, sample_ids/tags lists)."""
    dataset_path: str
    mode: Literal["online", "offline"] = "online"
    score_enabled: bool = True
    sample_ids: list[str] | None = None
    tags: list[str] | None = None
    snapshot_path: str | None = None  # required when mode == "offline"


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


class CreateWorkOrderRequest(BaseModel):
    template_version_id: str
    document_schema_id: str
    document_schema_version: str
    generation_session_id: str | None = None


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
