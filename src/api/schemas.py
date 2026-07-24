"""HTTP-boundary DTOs for the API layer.

Thin transport models only -- business data still crosses layer boundaries as
the dataclasses in src.pipelines.document_rag.schemas / src.core.auth. We do
not redefine business schemas here.
"""
from __future__ import annotations

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


class ErrorResponse(BaseModel):
    """Unified error envelope for agent-parsable responses."""
    error: dict = Field(default_factory=dict)


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


class UploadAck(BaseModel):
    success_count: int
    total_count: int
    failed_count: int = 0
    skipped_count: int = 0
    status: str
    messages: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    kb_name: str
    query: str
    history: list[list[str]] = Field(default_factory=list)
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
    created_at: str


class AddMessageRequest(BaseModel):
    role: str
    content: str


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
    role: str = "user"
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
    permission: str = "read"  # read | write | admin


class AssignKbRequest(BaseModel):
    department_id: int
    owner_user_id: int | None = None


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


# ---------------------------------------------------------------------------
# Config (system_admin)
# ---------------------------------------------------------------------------

class ConfigResponse(BaseModel):
    """Current runtime config; secrets are redacted."""
    settings: dict[str, object] = Field(default_factory=dict)


class UpdateConfigRequest(BaseModel):
    settings: dict[str, object] = Field(default_factory=dict)


class RagflowHealthResponse(BaseModel):
    reachable: bool
    message: str
    missing_datasets: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Logs (system_admin: global, dept_admin: department-scoped)
# ---------------------------------------------------------------------------

class AuditEventView(BaseModel):
    id: int
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
