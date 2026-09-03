"""Small domain objects shared by the export API, worker and renderers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
import math
from typing import Any


EXPORT_FORMATS = frozenset({"md", "xlsx", "docx", "pdf", "pptx"})
EXPORT_FORMAT_ALIASES = {
    "markdown": "md",
    "md": "md",
    "excel": "xlsx",
    "xlsx": "xlsx",
    "word": "docx",
    "woed": "docx",
    "docx": "docx",
    "pdf": "pdf",
    "powerpoint": "pptx",
    "power-point": "pptx",
    "ppt": "pptx",
    "pptx": "pptx",
}
EXPORT_JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "dead_letter"})
EXPORT_CONTENT_SHAPES = frozenset({"report", "data", "raw"})


def normalize_export_format(value: str) -> str:
    normalized = str(value or "").strip().lower().lstrip(".")
    result = EXPORT_FORMAT_ALIASES.get(normalized, normalized)
    if result not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format: {value}")
    return result


def is_export_format_enabled(value: str) -> bool:
    """Return whether a format is enabled by the current server rollout flags."""

    try:
        normalized = normalize_export_format(value)
    except ValueError:
        return False
    try:
        import src.settings as settings

        return bool(getattr(settings, "RESULT_EXPORT_ENABLED", True)) and bool(
            getattr(settings, f"RESULT_EXPORT_{normalized.upper()}_ENABLED", True)
        )
    except Exception:
        # A settings/bootstrap problem must not make an already-running worker
        # silently accept a format that cannot be governed. Fail closed.
        return False


def enabled_export_formats() -> tuple[str, ...]:
    """Return enabled formats in the stable order used by API and UI menus."""

    return tuple(format_name for format_name in ("md", "xlsx", "docx", "pdf", "pptx") if is_export_format_enabled(format_name))


def normalize_content_shape(value: str) -> str:
    normalized = str(value or "report").strip().lower()
    if normalized not in EXPORT_CONTENT_SHAPES:
        raise ValueError(f"unsupported export content shape: {value}")
    return normalized


@dataclass(frozen=True)
class ResultEnvelope:
    """Immutable, renderer-independent representation of one completed result."""

    title: str = "导出结果"
    query: str = ""
    answer: str = ""
    footer: str = ""
    tables: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "v1"
    blocks: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    language: str = "zh-CN"

    def to_dict(self) -> dict[str, Any]:
        return deepcopy({
            "title": self.title,
            "query": self.query,
            "answer": self.answer,
            "footer": self.footer,
            "tables": self.tables,
            "citations": self.citations,
            "metadata": self.metadata,
            "schema_version": self.schema_version,
            "blocks": self.blocks,
            "assets": self.assets,
            "language": self.language,
        })

    def normalized(self) -> "ResultEnvelope":
        """Return a detached envelope with stable table value-type metadata."""

        tables: list[dict[str, Any]] = []
        for raw_table in self.tables:
            table = deepcopy(raw_table)
            columns = list(table.get("columns") or [])
            rows = table.get("rows") or []
            supplied_types = table.get("value_types")
            if isinstance(supplied_types, list) and len(supplied_types) >= len(columns):
                value_types = [str(item) for item in supplied_types[:len(columns)]]
            else:
                value_types = []
                for column_index in range(len(columns)):
                    values = [
                        row[column_index]
                        if isinstance(row, (list, tuple)) and column_index < len(row)
                        else ""
                        for row in rows
                    ]
                    value_types.append(_infer_value_type(values))
            table["value_types"] = value_types
            tables.append(table)
        return ResultEnvelope(
            title=self.title,
            query=self.query,
            answer=self.answer,
            footer=self.footer,
            tables=tables,
            citations=deepcopy(self.citations),
            metadata=deepcopy(self.metadata),
            schema_version=self.schema_version or "v1",
            blocks=deepcopy(self.blocks),
            assets=deepcopy(self.assets),
            language=self.language or "zh-CN",
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResultEnvelope":
        if not isinstance(value, dict):
            raise ValueError("result envelope must be an object")
        return cls(
            title=str(value.get("title") or "导出结果"),
            query=str(value.get("query") or ""),
            answer=str(value.get("answer") or ""),
            footer=str(value.get("footer") or ""),
            tables=[item for item in (value.get("tables") or []) if isinstance(item, dict)],
            citations=[item for item in (value.get("citations") or []) if isinstance(item, dict)],
            metadata=dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), dict) else {},
            schema_version=str(value.get("schema_version") or "v1"),
            blocks=[item for item in (value.get("blocks") or []) if isinstance(item, dict)],
            assets=[item for item in (value.get("assets") or []) if isinstance(item, dict)],
            language=str(value.get("language") or "zh-CN"),
        )


def _infer_value_type(values: list[Any]) -> str:
    """Infer an export type without coercing the original source values."""

    non_empty = [value for value in values if value not in (None, "")]
    if not non_empty:
        return "text"
    if all(isinstance(value, bool) for value in non_empty):
        return "boolean"
    if all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in non_empty
    ):
        return "number"
    if all(isinstance(value, (date, datetime)) for value in non_empty):
        return "date"
    if all(
        isinstance(value, str) and value.lower().startswith(("http://", "https://"))
        for value in non_empty
    ):
        return "url"
    return "text"


@dataclass(frozen=True)
class ResultSnapshot:
    snapshot_id: str
    owner_user_id: str
    tenant_id: str
    session_id: str
    turn_id: str
    envelope: ResultEnvelope
    created_at: str
    schema_version: str = "v1"
    source_hash: str = ""
    department_id: str | None = None
    knowledge_base_name: str = ""
    assistant_message_id: int | None = None


@dataclass(frozen=True)
class ExportJob:
    export_job_id: str
    snapshot_id: str
    owner_user_id: str
    tenant_id: str
    session_id: str
    format: str
    content_shape: str
    client_request_id: str
    options: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    attempt: int = 0
    max_attempts: int = 3
    available_at: str = ""
    lease_owner: str | None = None
    lease_token: int = 0
    lease_expires_at: str | None = None
    artifact_id: str | None = None
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    department_id: str | None = None
    knowledge_base_name: str = ""
    assistant_message_id: int | None = None


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    export_job_id: str
    owner_user_id: str
    session_id: str
    format: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    storage_ref: str
    preview: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    expires_at: str | None = None
    tenant_id: str = "default"
    department_id: str | None = None
    knowledge_base_name: str = ""


@dataclass(frozen=True)
class ArtifactHistoryEntry:
    """Append-only metadata for one artifact revision.

    The live ``Artifact`` row is intentionally hidden after retention expiry,
    but its hash and source relationship remain queryable for audit/history.
    ``available`` is computed from the current binary and retention state.
    """

    artifact: Artifact
    snapshot_id: str
    turn_id: str
    available: bool


@dataclass(frozen=True)
class ResourceLock:
    """Fenced lease for a mutable authoring resource."""

    tenant_id: str
    resource_type: str
    resource_id: str
    owner_id: str
    fencing_token: int
    lease_expires_at: str


@dataclass(frozen=True)
class RenderedResult:
    content: bytes
    mime_type: str
    extension: str
    preview: dict[str, Any] = field(default_factory=dict)
