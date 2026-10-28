"""HTTP-boundary DTOs for the API layer.

Thin transport models only -- business data still crosses layer boundaries as
the dataclasses in src.pipelines.document_rag.schemas / src.core.auth. We do
not redefine business schemas here.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from src.result_exports.models import normalize_export_format
from src.services.knowledge_scope_models import KnowledgeSelectionInput, KnowledgeScopeSnapshot


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


class UpdateKbSharingRequest(BaseModel):
    is_shared: bool


class KbView(BaseModel):
    name: str
    kb_id: int | None = None
    department_id: int | None = None
    department_name: str | None = None
    permission: str | None = None
    registered: bool = True
    owner_user_id: int | None = None
    is_shared: bool = False
    is_owner: bool = False


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
# Document assets: logical documents with version chain, lifecycle and links
# ---------------------------------------------------------------------------

DocCategory = Literal[
    "design_doc", "schematic", "bom", "netlist", "test_report", "requirement", "standard", "other"
]
DocRelationType = Literal["derived_from", "companion", "references", "verified_by"]


class DocAssetVersionView(BaseModel):
    id: int
    asset_id: int
    version_no: int
    file_id: str = ""
    file_name: str = ""
    content_hash: str = ""
    parse_status: str = ""
    note: str = ""
    state: str = "draft"
    uploaded_by_user_id: int | None = None
    uploaded_at: str


class DocAssetEventView(BaseModel):
    id: int
    asset_id: int
    version_id: int | None = None
    event: str
    actor_user_id: int | None = None
    comment: str = ""
    created_at: str


class DocAssetLinkView(BaseModel):
    id: int
    from_asset_id: int
    to_asset_id: int
    rel_type: str
    note: str = ""
    source: str = "manual"
    status: str = "confirmed"
    created_at: str
    to_title: str = ""
    to_category: str = ""
    to_kb_name: str = ""


class DocAssetView(BaseModel):
    id: int
    department_id: int
    kb_id: int
    kb_name: str = ""
    project: str = ""
    doc_no: str = ""
    title: str
    category: str = "other"
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    lifecycle_status: str = "draft"
    current_version_id: int | None = None
    owner_user_id: int | None = None
    version_count: int = 0
    effective_version_no: int | None = None
    created_at: str
    updated_at: str


class DocAssetDetailView(DocAssetView):
    versions: list[DocAssetVersionView] = Field(default_factory=list)
    events: list[DocAssetEventView] = Field(default_factory=list)
    links_out: list[DocAssetLinkView] = Field(default_factory=list)
    links_in: list[DocAssetLinkView] = Field(default_factory=list)


class UpdateDocAssetRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    doc_no: str | None = Field(default=None, max_length=100)
    category: DocCategory | None = None
    project: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=20)


class AddDocAssetVersionRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=500)


class CreateDocAssetLinkRequest(BaseModel):
    to_asset_id: int
    rel_type: DocRelationType
    note: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# Wiki knowledge layer (WeKnora-style distillation)
# ---------------------------------------------------------------------------

WikiPageType = Literal["summary", "entity", "concept", "index"]
WikiPageStatus = Literal["draft", "published", "archived"]


class WikiPageView(BaseModel):
    id: int
    kb_id: int
    slug: str
    title: str
    page_type: str = "entity"
    status: str = "published"
    summary: str = ""
    aliases: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    chunk_refs: list[str] = Field(default_factory=list)
    in_links: list[str] = Field(default_factory=list)
    out_links: list[str] = Field(default_factory=list)
    version: int = 1
    last_edit_source: str = "pipeline"
    updated_at: str


class WikiPageDetailView(WikiPageView):
    content: str = ""
    backlinks: list[dict] = Field(default_factory=list)
    revisions: list[dict] = Field(default_factory=list)


class WikiIngestRequest(BaseModel):
    granularity: Literal["focused", "standard", "exhaustive"] = "standard"
    max_pages_per_ingest: int = Field(default=0, ge=0, le=2000)
    file_ids: list[str] = Field(default_factory=list)


class WikiPageUpsertRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    page_type: WikiPageType = "concept"
    slug: str = Field(default="", max_length=300)
    content: str = Field(default="", max_length=100000)
    summary: str = Field(default="", max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    status: WikiPageStatus = "published"


class WikiPageUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = Field(default=None, max_length=100000)
    summary: str | None = Field(default=None, max_length=2000)
    aliases: list[str] | None = Field(default=None, max_length=20)
    status: WikiPageStatus | None = None
    version: int | None = Field(default=None, ge=1)


class WikiGraphView(BaseModel):
    view: str = "pages"
    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    kb_name: str
    title: str = "新对话"
    knowledge_scope: KnowledgeSelectionInput | None = None


class SessionView(BaseModel):
    id: int
    user_id: int
    kb_name: str
    title: str
    created_at: str
    updated_at: str
    knowledge_scope: KnowledgeScopeSnapshot | None = None
    knowledge_selection_hash: str = ""
    source_access_restricted: bool = False


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


class AttachmentSnapshotView(BaseModel):
    """Frozen attachment view rendered with historical messages."""

    attachment_id: str
    filename: str
    media_type: str = ""
    usage_hint: str = "reference"
    content_hash: str = ""
    ordinal: int = 0
    parse_status: str = "queued"
    deleted: bool = False


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
    # Attachment snapshots of the owning turn (user messages only, optional).
    attachments: list[AttachmentSnapshotView] = Field(default_factory=list)
    # 紧凑引用列表（assistant 消息；来自 turn summary，内容为预览长度），
    # 刷新页面后"参考来源"面板仍可用，不依赖 done 事件重放。
    citations: list[dict] = Field(default_factory=list)
    # 该 turn 持久化的 document_card 事件（历史回填用；刷新/重开会话后
    # 候选卡片与文档状态卡不丢失，前端按引用去重合并）。
    cards: list[dict] = Field(default_factory=list)
    # 该 turn 持久化的执行轨迹（stage/tool 事件折叠；刷新后轨迹面板不消失）。
    trace_steps: list[dict] = Field(default_factory=list)
    # 复杂任务的计划/预算/校验投影（来自 turn summary；刷新后不清空）。
    plan: dict | None = None
    budget: dict | None = None
    verification: dict | None = None
    subagents: list[dict] = Field(default_factory=list)
    source_access_restricted: bool = False


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
    knowledge_scope: KnowledgeSelectionInput | None = None
    client_request_id: str | None = Field(default=None, max_length=128)
    # Retained for wire compatibility; the route normalizes KB turns to deep
    # retrieval and general chat bypasses the knowledge-base agent entirely.
    query_mode: Literal["fast", "deep"] = "deep"
    # Wiki 辅助开关(每轮): False 时不注册 wiki_search 工具。
    wiki_assist: bool = False
    # Chat attachments (design §5.1): session-scoped references; the server
    # re-verifies ownership and parse state before persisting the turn.
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    # "auto" expands deterministically server-side; explicit values may only
    # narrow the resolved scope, never widen it.
    source_scope: Literal[
        "auto",
        "attachment_only",
        "knowledge_base_only",
        "attachment_and_knowledge_base",
    ] = "auto"


class TurnView(BaseModel):
    id: str
    session_id: int
    user_message_id: int
    assistant_message_id: int
    kb_name: str
    query: str
    query_mode: Literal["fast", "deep"] = "fast"
    wiki_assist: bool = False
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
    source_scope: str = "auto"
    attachments: list[AttachmentSnapshotView] = Field(default_factory=list)
    # Queue visibility: which earlier turn blocks this one, why, and when a
    # waiting-for-attachments turn will time out.
    blocked_by_turn_id: str | None = None
    queue_reason: str = ""
    wait_deadline: str | None = None
    knowledge_scope: KnowledgeScopeSnapshot | None = None
    knowledge_selection_hash: str = ""
    source_access_restricted: bool = False


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
    # None = automatic: organized exports exclude the source list unless the
    # caller explicitly asks for it; as-is exports keep the legacy default.
    include_citations: bool | None = None
    # Arrange-before-export: one model pass turns the chat answer into
    # deliverable content following the original user request.
    organize: bool = True
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
    # 主动触发项目记忆：精确指定某个已完成轮次的 user/assistant 消息；
    # 为空时保持原有"重新提炼最新完成轮"的行为。
    message_ids: list[int] = Field(default_factory=list, max_length=20)


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
    role: Literal["employee", "system_admin"] = "employee"
    department_id: int | None = None


class SetUserActiveRequest(BaseModel):
    is_active: bool


class ResetPasswordRequest(BaseModel):
    new_password: str


class ChangeOwnPasswordRequest(BaseModel):
    current_password: str
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
# KB mounting (system_admin)
# ---------------------------------------------------------------------------

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
    employee_count: int = 0
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
# Logs (system_admin: global, employee: department-scoped)
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
    """Body for POST /evaluation/runs.

    ``kb_id`` is the authoritative binding for new callers. ``kb_name`` is
    retained as a display/redundant consistency field and for the short
    compatibility window where a legacy caller only supplies a unique name.
    """
    dataset_path: str
    kb_id: int | None = None
    kb_name: str | None = None
    mode: Literal["online", "offline"] = "online"
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
    owner_user_id: int | None = None
    owner_username: str | None = None
    # Empty when the KB has an active owner; otherwise the preflight reason
    # shown before the system admin submits a run.
    evaluation_blocked_reason: str = ""


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
# External MCP plugins (governed external tool federation)


class McpServerCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    transport: str = "streamable_http"
    url: str = Field(min_length=1, max_length=2048)
    description: str = Field(default="", max_length=1000)
    headers: dict[str, str] | None = None
    timeout_s: int | None = None
    sse_read_timeout_s: int | None = None
    department_scope: str = "none"
    department_ids: list[int] = Field(default_factory=list)
    disabled_tools: list[str] = Field(default_factory=list)
    write_tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    icon: str = Field(default="", max_length=16)


class McpServerUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    transport: str | None = None
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] | None = None
    clear_headers: bool = False
    timeout_s: int | None = None
    sse_read_timeout_s: int | None = None
    department_scope: str | None = None
    department_ids: list[int] | None = None
    disabled_tools: list[str] | None = None
    write_tools: list[str] | None = None
    tags: list[str] | None = None
    icon: str | None = Field(default=None, max_length=16)


class McpServerStatusRequest(BaseModel):
    enabled: bool


class McpSubscribeRequest(BaseModel):
    allow_write: bool = False


class McpWriteConsentRequest(BaseModel):
    allow_write: bool


class McpWriteToolsRequest(BaseModel):
    write_tools: list[str] = Field(default_factory=list)


class McpListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = False
    write_enabled: bool = False
    prerequisites_met: bool = False
    prerequisites_reason: str = ""


class McpServerResponse(BaseModel):
    server: dict[str, Any]


class McpTestResponse(BaseModel):
    ok: bool
    tool_count: int = 0
    read_only_tool_count: int = 0
    blocked_count: int = 0
    latency_ms: int = 0
    message: str = ""


class McpToolsResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class McpSubscriptionResponse(BaseModel):
    subscription: dict[str, Any]


class McpSubscriptionListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class McpWriteProposalListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class McpWriteProposalResponse(BaseModel):
    proposal: dict[str, Any]


# ---------------------------------------------------------------------------
# Governed sandbox task admission
# ---------------------------------------------------------------------------


class SandboxTaskCreateRequest(BaseModel):
    """Declaration-only task input; runtime-control fields are forbidden."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    bundle_ref: str = Field(min_length=1, max_length=256)
    bundle_digest: str = Field(min_length=64, max_length=71)
    profile_ref: str = Field(min_length=1, max_length=256)
    # The server derives the immutable snapshot from the enabled catalog entry.
    # A caller may echo it for optimistic concurrency, but cannot invent a
    # policy hash that widens the bundle's authorization.
    policy_snapshot_hash: str = Field(default="", max_length=256)
    kind: Literal["task", "session"] = "task"
    network_policy_ref: str = Field(default="", max_length=256)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)
    deadline_at: str = Field(default="", max_length=80)


class SandboxCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=300)


class SandboxTaskResponse(BaseModel):
    task: dict[str, Any]


class SandboxTaskListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class SandboxTaskEventsResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class SandboxRunnerResponse(BaseModel):
    response: dict[str, Any] = Field(default_factory=dict)


class SandboxBundleRegisterRequest(BaseModel):
    """Declaration-only bundle metadata; executable recipes are out of band."""

    model_config = ConfigDict(extra="forbid")

    bundle_ref: str = Field(min_length=1, max_length=256)
    bundle_digest: str = Field(min_length=64, max_length=71)
    profile_ref: str = Field(min_length=1, max_length=256)
    required_capabilities: list[str] = Field(default_factory=lambda: ["sandbox.execute"], max_length=32)
    network_policy_ref: str = Field(default="", max_length=256)
    sbom_ref: str = Field(default="", max_length=256)
    signature_ref: str = Field(default="", max_length=256)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    tool_allowlist: list[str] = Field(default_factory=list, max_length=256)
    runtime_policy_ref: str = Field(default="", max_length=256)
    runtime_policy_digest: str = Field(default="", min_length=0, max_length=71)
    catalog_source: str = Field(default="curated", max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SandboxBundleResponse(BaseModel):
    bundle: dict[str, Any]


class SandboxBundleListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class SandboxGatewayRegisterRequest(BaseModel):
    """Declaration-only Gateway metadata; no stdio recipe is accepted."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str = Field(min_length=1, max_length=256)
    profile_ref: str = Field(min_length=1, max_length=256)
    endpoint: str = Field(min_length=1, max_length=2048)
    digest: str = Field(min_length=64, max_length=71)
    network_policy_ref: str = Field(default="", max_length=256)
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    sbom_ref: str = Field(default="", max_length=256)
    # Mapping is declaration-only: each key is a reviewed tool name and each
    # value is a bounded input schema.  No endpoint/command recipe is accepted.
    tool_allowlist: dict[str, dict[str, Any]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SandboxGatewayResponse(BaseModel):
    gateway: dict[str, Any]


class SandboxGatewayListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class SandboxGatewayInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class SandboxGatewaySubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_write: bool = False


class SandboxGatewaySubscriptionResponse(BaseModel):
    subscription: dict[str, Any]


class SandboxCapabilityGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_type: Literal["user", "department"]
    principal_id: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list, max_length=16)


class SandboxCapabilityGrantResponse(BaseModel):
    principal_type: str
    principal_id: str
    capabilities: list[str] = Field(default_factory=list)


class SandboxArtifactStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(min_length=1, max_length=75_000_000)
    sha256: str = Field(min_length=64, max_length=71)
    media_type: str = Field(default="", max_length=120)
    role: Literal["input", "output", "log"] = "input"
    execution_id: str = Field(default="", max_length=256)
    expires_at: str = Field(default="", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SandboxArtifactResponse(BaseModel):
    artifact: dict[str, Any]


class SandboxArtifactListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class SandboxSkillRegisterRequest(BaseModel):
    """Text-only Skill declaration; executable extension fields are forbidden."""

    model_config = ConfigDict(extra="forbid")

    skill_ref: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=131_072)
    source_ref: str = Field(default="", max_length=512)
    catalog_source: str = Field(default="curated", max_length=50)
    department_id: str = Field(default="", max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SandboxSkillPackageUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_ref: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    department_id: str = Field(min_length=1, max_length=128)
    source_ref: str = Field(default="", max_length=512)
    catalog_source: str = Field(default="curated", max_length=50)
    package_base64: str = Field(min_length=1, max_length=12_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class SandboxTextSkillPackageUploadRequest(BaseModel):
    """Text-only ZIP: name/version come from SKILL.md, department from admin."""

    model_config = ConfigDict(extra="forbid")

    department_id: str = Field(min_length=1, max_length=128)
    package_base64: str = Field(min_length=1, max_length=12_000_000)


class SandboxPersonalTextSkillUploadRequest(BaseModel):
    """Employee upload: scope, owner and Skill ref are assigned by the server."""

    model_config = ConfigDict(extra="forbid")

    package_base64: str = Field(min_length=1, max_length=12_000_000)


class SandboxPersonalExecutableSkillUploadRequest(BaseModel):
    """Employee source submission; building and approval remain admin-only."""

    model_config = ConfigDict(extra="forbid")

    skill_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    package_base64: str = Field(min_length=1, max_length=12_000_000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)


class SandboxPersonalSkillShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    share_to_department: bool


class SandboxSkillBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_bundle_ref: str = Field(default="bundle/report", min_length=1, max_length=256)
    profile_ref: str = Field(min_length=1, max_length=256)
    runtime_policy_ref: str = Field(default="", max_length=256)
    runtime_policy_digest: str = Field(default="", max_length=71)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(
        default_factory=lambda: ["sandbox.execute", "sandbox.write_artifact"], max_length=8
    )


class SandboxSkillPipSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index_url: str = Field(default="", max_length=512)


class SandboxSkillExecutionBindingRequest(BaseModel):
    """References to an operator-deployed bundle, never a script upload."""

    model_config = ConfigDict(extra="forbid")

    bundle_ref: str = Field(min_length=1, max_length=256)
    bundle_digest: str = Field(min_length=64, max_length=71)
    profile_ref: str = Field(min_length=1, max_length=256)


class SandboxSkillRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)


class SandboxSkillResponse(BaseModel):
    skill: dict[str, Any]


class SandboxSkillListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
