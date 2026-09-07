"""Domain contracts for chat attachments (Phase 0, frozen on purpose).

These shapes are the stable boundary between the API layer, the attachment
worker, the agent tools and the conversation state machine.  They are plain
dataclasses (not pydantic) so both the API and worker processes can share them
without a serialization framework coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Enum-ish string constants (durable schema values; never rename).
# ---------------------------------------------------------------------------

SourceScope = Literal[
    "auto",
    "attachment_only",
    "knowledge_base_only",
    "attachment_and_knowledge_base",
]
SOURCE_SCOPES: tuple[str, ...] = (
    "auto",
    "attachment_only",
    "knowledge_base_only",
    "attachment_and_knowledge_base",
)

ATTACHMENT_STATUS_ACTIVE = "active"
ATTACHMENT_STATUS_DELETED = "deleted"
ATTACHMENT_STATUS_EXPIRED = "expired"

USAGE_HINT_REFERENCE = "reference"
USAGE_HINT_DATA = "data"

PARSE_STATUS_QUEUED = "queued"
PARSE_STATUS_RUNNING = "running"
PARSE_STATUS_READY = "ready"
PARSE_STATUS_DEGRADED = "degraded"
PARSE_STATUS_FAILED = "failed"

JOB_TYPE_PARSE = "parse"

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

PART_TYPE_TEXT = "text"
PART_TYPE_TABLE = "table"
PART_TYPE_IMAGE = "image"
PART_TYPE_CIRCUIT = "circuit"
PART_TYPE_OCR_TEXT = "ocr_text"
PART_TYPE_VISUAL_EVIDENCE = "visual_evidence"

# Current deterministic local parser implementation tag. Bumping this value
# re-enqueues parse jobs for existing assets (parts are rebuilt; the source
# file is never re-uploaded).
PARSER_VERSION = "local_doc_v1"

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".txt", ".md", ".xlsx", ".xlsm", ".edf", ".edif"}
)
# XLS is rejected (legacy binary format); the KB upload path has the same
# policy for structured parsing.
REJECTED_EXTENSIONS: frozenset[str] = frozenset({".xls"})


class AttachmentError(Exception):
    """Base class for attachment domain errors (maps to HTTP 400)."""


class AttachmentNotFound(AttachmentError, KeyError):
    pass


class AttachmentPermissionError(AttachmentError, PermissionError):
    pass


class AttachmentUnsupportedType(AttachmentError, ValueError):
    pass


class AttachmentQuotaExceeded(AttachmentError, ValueError):
    pass


@dataclass(frozen=True)
class AttachmentRef:
    """Server-resolved attachment reference handed to the agent runtime.

    Built only after ACL verification; agents never see local paths or the
    ability to widen scope beyond these frozen references.
    """

    attachment_id: str
    asset_id: str
    session_id: int
    filename: str
    media_type: str
    extension: str
    size_bytes: int
    sha256: str
    usage_hint: str = USAGE_HINT_REFERENCE
    parse_status: str = PARSE_STATUS_QUEUED
    parser_version: str = ""
    degraded_reason: str = ""


@dataclass
class AttachmentRecord:
    """User-visible attachment row (``chat_attachments``)."""

    attachment_id: str
    session_id: int
    user_id: int
    tenant_id: str
    asset_id: str
    client_request_id: str | None
    filename: str
    media_type: str
    extension: str
    size_bytes: int
    sha256: str
    usage_hint: str
    status: str
    created_at: str
    updated_at: str
    expires_at: str | None = None
    deleted_at: str | None = None
    # Denormalized for list views; authoritative copy lives on the asset.
    parse_status: str = PARSE_STATUS_QUEUED
    error_code: str = ""
    error_message: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachmentAsset:
    """Physical file + parse facts (``chat_attachment_assets``)."""

    asset_id: str
    session_id: int
    user_id: int
    tenant_id: str
    sha256: str
    media_type: str
    extension: str
    size_bytes: int
    storage_key: str
    parse_status: str
    parser_version: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class AttachmentPart:
    """Canonical parsed chunk (``chat_attachment_parts``)."""

    part_id: str
    asset_id: str
    ordinal: int
    part_type: str
    text_content: str = ""
    locator: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    parser_version: str = PARSER_VERSION


@dataclass
class AttachmentJob:
    """Background parse job (``chat_attachment_jobs``)."""

    job_id: str
    tenant_id: str
    user_id: int
    session_id: int
    asset_id: str
    job_type: str
    status: str
    attempt: int
    max_attempts: int
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    lease_owner: str = ""
    lease_expires_at: str | None = None
    parser_version: str = ""
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None


@dataclass
class TurnAttachmentSnapshot:
    """Frozen attachment view stored per turn (``turn_attachments``).

    History must render ``design-review.pdf（已删除）`` after the attachment
    is gone; snapshots intentionally duplicate metadata instead of joining
    live rows that may have been soft-deleted or expired.
    """

    turn_id: str
    attachment_id: str
    ordinal: int
    filename_snapshot: str
    media_type_snapshot: str
    usage_hint_snapshot: str
    content_hash_snapshot: str
    parser_version_snapshot: str


def normalize_source_scope(value: Any) -> str:
    value = str(value or "auto").strip()
    return value if value in SOURCE_SCOPES else "auto"


def resolve_source_scope(
    *,
    requested_scope: Any,
    has_attachments: bool,
    is_general_chat: bool,
) -> str:
    """Resolve a requested source scope without ever widening it.

    ``auto`` is expanded from the request/session shape.  An explicit scope
    remains authoritative: attachment ids are ignored when the caller chose
    ``knowledge_base_only`` and a combined scope is narrowed to
    ``attachment_only`` for a general chat.  A caller cannot request an
    attachment-only turn without actually providing an attachment.
    """
    requested = normalize_source_scope(requested_scope)
    if requested == "auto":
        return resolve_auto_scope(
            has_attachments=has_attachments,
            is_general_chat=is_general_chat,
        )
    if requested == "attachment_only" and not has_attachments:
        raise ValueError("attachment_only requires attachment_ids")
    if requested == "attachment_and_knowledge_base":
        if not has_attachments:
            return "knowledge_base_only"
        if is_general_chat:
            return "attachment_only"
    return requested


def resolve_auto_scope(*, has_attachments: bool, is_general_chat: bool) -> str:
    """Deterministic ``auto`` expansion (design §5.2).

    General chat without attachments keeps bypassing the KB agent entirely;
    with attachments it becomes an attachment-scoped agent run. KB chat maps
    to its existing deep agent, optionally widened with attachment tools.
    """
    if is_general_chat:
        return "attachment_only" if has_attachments else "knowledge_base_only"
    return "attachment_and_knowledge_base" if has_attachments else "knowledge_base_only"


def scope_allows_attachments(scope: str) -> bool:
    return scope in {"attachment_only", "attachment_and_knowledge_base"}


def scope_allows_kb(scope: str) -> bool:
    # "knowledge_base_only" also covers the original general chat behaviour:
    # the general toolset has no KB data tools, so the label is safe.
    return scope in {"knowledge_base_only", "attachment_and_knowledge_base"}


def safe_filename(filename: str, max_len: int = 200) -> str:
    """Sanitize a client-supplied filename for storage/display."""
    cleaned = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = cleaned.replace("\x00", "").strip()
    return cleaned[:max_len] or "attachment"


def split_extension(filename: str) -> str:
    name = safe_filename(filename)
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return name[dot:].lower()
