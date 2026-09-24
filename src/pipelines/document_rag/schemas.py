from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def kb_scope_key(kb_name: str, department_id: str | int | None = None) -> str:
    if department_id in (None, ""):
        return str(kb_name)
    return f"{department_id}:{kb_name}"


@dataclass
class RequestContext:
    user_id: str = "anonymous"
    session_id: str = ""
    roles: list[str] = field(default_factory=list)
    allowed_kbs: list[str] = field(default_factory=list)
    kb_permissions: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Project scope is deliberately separate from knowledge-base scope. These
    # values are a short-lived authentication snapshot only; project services
    # always re-check the persisted ProjectPrincipalBinding before use.
    tenant_id: str | None = None
    project_id: str | None = None
    allowed_projects: list[str] = field(default_factory=list)
    project_roles: dict[str, str] = field(default_factory=dict)
    project_capabilities: dict[str, list[str]] = field(default_factory=dict)
    baseline_id: str | None = None
    target_revision: str | None = None
    effective_at: datetime | None = None
    module_scope: list[str] = field(default_factory=list)

    def is_system_admin(self) -> bool:
        return "system_admin" in self.roles

    def can_access_kb(self, kb_name: str) -> bool:
        return self.has_kb_permission(kb_name, "read")

    def has_kb_permission(self, kb_name: str, required: str = "read") -> bool:
        if self.is_system_admin():
            return False
        levels = {"read": 1, "write": 2, "admin": 3}
        required_level = levels.get(required, 1)
        department_id = self.metadata.get("resource_department_id")
        if department_id in (None, ""):
            department_id = self.metadata.get("department_id")

        if department_id not in (None, ""):
            scoped_key = kb_scope_key(kb_name, department_id)
            permission = self.kb_permissions.get(scoped_key)
            if permission is None and scoped_key in self.allowed_kbs:
                permission = "read"
            return levels.get(permission or "", 0) >= required_level

        return False


@dataclass
class Evidence:
    id: str
    content: str
    source_name: str = ""
    source_type: str = "document"
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    backend: str = "ragflow"
    retriever: str = ""


@dataclass
class EvidenceEnvelope(Evidence):
    """Stable evidence exchanged at the project/document domain boundary."""

    project_id: str | None = None
    baseline_id: str | None = None
    source_version_id: str | None = None
    processing_artifact_id: str | None = None
    document_role: str | None = None
    module_scope: list[str] = field(default_factory=list)
    revision: str | None = None
    approval_status: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    locator: dict[str, Any] = field(default_factory=dict)
    fact_type: str | None = None
    certainty: str = "retrieved_statement"
    authority_policy_id: str | None = None
    content_hash: str = ""
    quote_span: dict[str, Any] | None = None
    lineage_group_id: str | None = None
    retrieved_at: datetime | None = None


INGEST_STATUS_SUCCESS = "success"
INGEST_STATUS_PARTIAL = "partial"
INGEST_STATUS_FAILED = "failed"

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_PAUSED = "paused"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_UNKNOWN = "unknown"
# Set when retry_count >= WORKER_MAX_RETRIES. The worker never reclaims such a
# row, so callers must treat it as not reusable (re-upload must rebuild it).
TASK_STATUS_DEAD_LETTER = "dead_letter"

_PROCESSOR_SPREADSHEET = "spreadsheet_table"

# Canonical Chinese display labels for normalized parse statuses. Single source
# shared by parse_status_view / parse_status_label (backend) — the frontend
# keeps a mirrored copy in KbFilesPage.tsx STATUS_LABELS; update both together.
TASK_STATUS_LABELS = {
    TASK_STATUS_QUEUED: "排队中",
    TASK_STATUS_RUNNING: "解析中",
    TASK_STATUS_PAUSED: "已暂停",
    TASK_STATUS_COMPLETED: "已解析",
    TASK_STATUS_FAILED: "解析失败",
    TASK_STATUS_CANCELLED: "已停止",
    TASK_STATUS_UNKNOWN: "状态未知",
}


@dataclass
class IngestResult:
    success_count: int
    total_count: int
    messages: list[str] = field(default_factory=list)
    backend: str = "ragflow"
    failed_count: int = 0
    skipped_count: int = 0

    @property
    def ok(self) -> bool:
        return self.total_count > 0 and self.success_count == self.total_count and self.failed_count == 0 and self.skipped_count == 0

    @property
    def status(self) -> str:
        if self.ok:
            return INGEST_STATUS_SUCCESS
        if self.success_count > 0:
            return INGEST_STATUS_PARTIAL
        return INGEST_STATUS_FAILED

    @property
    def partial(self) -> bool:
        return self.status == INGEST_STATUS_PARTIAL

    @property
    def failed(self) -> bool:
        return self.status == INGEST_STATUS_FAILED

    def to_message(self) -> str:
        if self.ok:
            prefix = "全部处理成功"
        elif self.success_count > 0:
            prefix = "部分处理完成"
        else:
            prefix = "未处理成功"
        summary = (
            f"{prefix}: 成功 {self.success_count}, "
            f"失败 {self.failed_count}, 跳过 {self.skipped_count}, 总计 {self.total_count}"
        )
        details = "\n".join(self.messages)
        return f"{summary}\n{details}" if details else summary

    def __str__(self) -> str:
        return self.to_message()


@dataclass(frozen=True)
class ParseStatusView:
    raw: str
    normalized: str
    label: str
    searchability: str
    is_terminal: bool
    is_success: bool
    is_failed: bool
    can_show_chunks: bool
    can_cancel: bool


def normalize_parse_status(raw_status: object, processor_kind: str = "") -> str:
    status = str(raw_status or "").strip().lower()
    if not status:
        return TASK_STATUS_UNKNOWN
    if status in {"0", "queued", "pending", "uploading", "uploaded", "ready", "archived"}:
        return TASK_STATUS_QUEUED
    if status in {"1", "running", "parsing", "processing", "started"}:
        return TASK_STATUS_RUNNING
    if status in {"2", "done", "success", "parsed", "completed", "complete", "finish", "finished", "indexed", "degraded", "已完成"}:
        return TASK_STATUS_COMPLETED
    if status in {"3", "fail", "failed", "error", "exception", "unsupported"}:
        return TASK_STATUS_FAILED
    if status in {"cancel", "cancelled", "canceled", "stopped", "stop", "deleted", "removed"}:
        return TASK_STATUS_CANCELLED
    if status == TASK_STATUS_PAUSED:
        return TASK_STATUS_PAUSED
    return TASK_STATUS_UNKNOWN


def parse_status_label(raw_status: object) -> str:
    """Chinese display label for any raw parse status string."""
    normalized = normalize_parse_status(raw_status)
    return TASK_STATUS_LABELS.get(normalized, str(raw_status or "").strip() or "-")


def parse_status_view(raw_status: object, processor_kind: str = "") -> ParseStatusView:
    raw = str(raw_status or "").strip().lower()
    normalized = normalize_parse_status(raw, processor_kind)
    is_spreadsheet = str(processor_kind or "").strip().lower() == _PROCESSOR_SPREADSHEET
    searchability_map = {
        TASK_STATUS_QUEUED: "等待解析",
        TASK_STATUS_RUNNING: "暂不可检索",
        TASK_STATUS_PAUSED: "暂不可检索",
        TASK_STATUS_COMPLETED: "结构化解析" if is_spreadsheet else "可检索",
        TASK_STATUS_FAILED: "不可检索",
        TASK_STATUS_CANCELLED: "不可检索",
        TASK_STATUS_UNKNOWN: "状态待确认",
    }
    label = TASK_STATUS_LABELS.get(normalized, normalized)
    searchability = searchability_map.get(normalized, "状态待确认")
    is_success = normalized == TASK_STATUS_COMPLETED
    is_failed = normalized == TASK_STATUS_FAILED
    is_terminal = normalized in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED}
    can_show_chunks = is_success and not is_spreadsheet
    can_cancel = normalized in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_FAILED}
    return ParseStatusView(
        raw=raw,
        normalized=normalized,
        label=label,
        searchability=searchability,
        is_terminal=is_terminal,
        is_success=is_success,
        is_failed=is_failed,
        can_show_chunks=can_show_chunks,
        can_cancel=can_cancel,
    )


@dataclass
class BackendResult:
    ok: bool
    message: str
    backend: str = "ragflow"


@dataclass
class DocumentInfo:
    id: str
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    backend: str = "ragflow"
    # 具名显示字段：跨后端共有，供 UI 直接访问，避免在 metadata dict 中翻找。
    processor_kind: str = ""
    status: str = ""
    local_path: str = ""
    ragflow_document_id: str = ""
    dataset_kind: str = ""
    ragflow_error: str = ""
    spreadsheet_profile: dict[str, Any] | None = None
    container_inspection: dict[str, Any] | None = None


@dataclass
class ParsedChunk:
    index: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    document_id: str
    file_name: str
    chunk_count: int
    chunks: list[ParsedChunk] = field(default_factory=list)
    backend: str = "ragflow"


@dataclass
class BackendHealth:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    backend: str = "ragflow"
