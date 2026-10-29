"""P0 External capability API.

Only employee API keys can reach this router.  The router exposes opaque
resource references and a read-only, fail-closed RAGFlow retrieval path; it
does not serialize internal pipeline DTOs or start a chat/Agent session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

import src.external_settings as external_settings
from src.api.deps import get_auth_service, get_pipeline
from src.api.external_support import (
    ExternalApiError,
    ExternalAuthorization,
    authorize_kb,
    audit_external,
    enforce_external_rate_limit,
    external_concurrency,
    request_id,
    require_external_principal,
)
from src.core.auth import AuthService, EmployeeApiKeyPrincipal
from src.core.app_pipeline import AppPipeline
from src.core.doc_assets import DocumentAssetError, DocumentAssetService
from src.agents.tools.spreadsheet_tools import (
    _execute_readonly_sql,
    _load_sql_registry,
    _validate_readonly_sql,
)
from src.circuit.index_service import CircuitIndexService
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.store import CircuitStore
from src.pipelines.document_rag.schemas import (
    DocumentRetrievalContract,
    DocumentSourceStamp,
    Evidence,
    ScopedRetrievalError,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DEAD_LETTER,
    TASK_STATUS_FAILED,
    TASK_STATUS_PAUSED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    normalize_parse_status,
)
from src.services.document_archive import DocumentArchiveManager
from src.services.document_routing import (
    PROCESSOR_KIND_RAGFLOW,
    PROCESSOR_KIND_SPREADSHEET,
    supported_pipeline_for_file,
)
from src.services.spreadsheet_index_service import SpreadsheetIndexService


router = APIRouter(prefix="/external", tags=["external"])
_DEFAULT_CHUNK_PAGE_SIZE = min(
    200,
    max(1, int(external_settings.HDB_EXTERNAL_CHUNK_PAGE_SIZE or 50)),
)


def get_asset_service() -> DocumentAssetService:
    return DocumentAssetService()


class ExternalCapabilityView(BaseModel):
    request_id: str
    capabilities: list[str]
    limits: dict[str, int]


class ExternalKbView(BaseModel):
    kb_ref: str
    name: str


class ExternalFileView(BaseModel):
    file_ref: str
    file_name: str
    file_size: int
    content_hash: str | None = None
    status: str
    download_available: bool = False


class ExternalFilePage(BaseModel):
    items: list[ExternalFileView]
    offset: int
    limit: int
    total: int


class ExternalChunkView(BaseModel):
    chunk_ref: str | None = None
    index: int
    content: str
    page: int | None = None


class ExternalChunkPage(BaseModel):
    file_ref: str
    file_name: str
    chunk_count: int
    offset: int
    limit: int
    items: list[ExternalChunkView]


class ExternalUploadFileView(BaseModel):
    file_name: str
    file_ref: str | None = None
    job_id: str | None = None
    status: str
    success: bool
    error_code: str | None = None


class ExternalUploadResponse(BaseModel):
    request_id: str
    status: str
    files: list[ExternalUploadFileView]
    warnings: list[str] = Field(default_factory=list)


class ExternalIngestionJobView(BaseModel):
    job_id: str
    file_ref: str
    file_name: str
    status: str
    progress: int
    success: bool
    error_code: str | None = None


class ExternalAssetVersionView(BaseModel):
    version_ref: str | None = None
    file_ref: str | None = None
    version_no: int
    state: str
    file_name: str
    content_hash: str = ""
    parse_status: str = ""
    note: str = ""
    created_at: str = ""


class ExternalAssetView(BaseModel):
    asset_ref: str
    title: str
    doc_no: str = ""
    category: str = "other"
    project: str = ""
    description: str = ""
    manufacturer: str = ""
    mpn: str = ""
    revision: str = ""
    tags: list[str] = Field(default_factory=list)
    lifecycle_status: str = "draft"
    version_count: int = 0
    effective_version_no: int | None = None
    updated_at: str = ""


class ExternalAssetDetailView(ExternalAssetView):
    versions: list[ExternalAssetVersionView] = Field(default_factory=list)


class ExternalAssetPage(BaseModel):
    items: list[ExternalAssetView]
    offset: int
    limit: int
    total: int


class ExternalAddAssetVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_ref: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


class ExternalTableColumnView(BaseModel):
    key: str
    header: str = ""
    dtype: str = "TEXT"
    unit: str = ""
    samples: list[str] = Field(default_factory=list)


class ExternalTableSchemaView(BaseModel):
    table_ref: str
    file_ref: str
    file_name: str
    sheet_name: str
    row_count: int = 0
    columns: list[ExternalTableColumnView] = Field(default_factory=list)
    note: str = ""


class ExternalTableSchemaResponse(BaseModel):
    tables: list[ExternalTableSchemaView]
    warnings: list[str] = Field(default_factory=list)


class ExternalTableQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1, max_length=20000)
    max_rows: int = Field(default=200, ge=1, le=200)


class ExternalTableQueryResponse(BaseModel):
    columns: list[ExternalTableColumnView]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = False


class ExternalCircuitQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["modules", "instances", "nets", "cross_references"] = "modules"
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class ExternalCircuitQueryResponse(BaseModel):
    kind: str
    rows: list[dict[str, Any]]
    row_count: int


class ExternalRetrieveScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["files", "kb"]
    file_refs: list[str] = Field(default_factory=list, max_length=50)
    expected_content_hashes: dict[str, str] = Field(default_factory=dict, max_length=50)


class ExternalRetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=10, ge=1, le=100)
    scope: ExternalRetrieveScope
    source_scope_ref: str | None = Field(default=None, max_length=200)
    capabilities: list[Literal["document_claim_lookup"]] = Field(
        default_factory=lambda: ["document_claim_lookup"], max_length=4
    )

    @field_validator("query")
    @classmethod
    def _trim_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query 不能为空")
        return value


class ExternalSourceView(BaseModel):
    file_ref: str
    file_name: str
    content_hash: str
    source_version_ref: str | None = None
    locator: dict[str, Any]


class ExternalRetrievalView(BaseModel):
    type: str
    backend: str
    score: float


class ExternalEvidenceView(BaseModel):
    evidence_id: str
    content: str
    source: ExternalSourceView
    retrieval: ExternalRetrievalView


class ExternalRetrieveResponse(BaseModel):
    request_id: str
    status: str
    results: list[ExternalEvidenceView]
    warnings: list[str] = Field(default_factory=list)


class ExternalBatchRetrieveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_ref: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=10, ge=1, le=100)
    scope: ExternalRetrieveScope
    source_scope_ref: str | None = Field(default=None, max_length=200)
    capabilities: list[Literal["document_claim_lookup"]] = Field(
        default_factory=lambda: ["document_claim_lookup"], max_length=4
    )

    @field_validator("query")
    @classmethod
    def _trim_batch_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query 不能为空")
        return value


class ExternalBatchRetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExternalBatchRetrieveItem] = Field(min_length=1, max_length=100)


class ExternalBatchErrorView(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ExternalBatchItemView(BaseModel):
    kb_ref: str
    status: str
    results: list[ExternalEvidenceView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: ExternalBatchErrorView | None = None


class ExternalBatchRetrieveResponse(BaseModel):
    request_id: str
    status: str
    items: list[ExternalBatchItemView]


class ExternalSourceScopeFileView(BaseModel):
    file_ref: str
    file_name: str
    content_hash: str


class ExternalSourceScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_ref: str = Field(min_length=1, max_length=200)
    file_refs: list[str] = Field(min_length=1, max_length=100)
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class ExternalSourceScopeView(BaseModel):
    scope_ref: str
    kb_ref: str
    files: list[ExternalSourceScopeFileView]
    created_at: str
    expires_at: str


class ExternalStoredEvidenceView(BaseModel):
    evidence_id: str
    content: str
    source: ExternalSourceView
    retrieval: ExternalRetrievalView
    status: str
    created_at: str
    expires_at: str


class ExternalEvidenceValidateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=200)
    required_terms: list[str] = Field(default_factory=list, max_length=50)
    required_fields: list[str] = Field(default_factory=list, max_length=50)
    expected_source_hash: str | None = Field(default=None, max_length=128)
    expected_content_hash: str | None = Field(default=None, max_length=128)


class ExternalEvidenceValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExternalEvidenceValidateItem] = Field(min_length=1, max_length=100)


class ExternalEvidenceValidateResult(BaseModel):
    evidence_id: str
    status: str
    supported: bool
    source_current: bool
    missing_terms: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class ExternalEvidenceValidateResponse(BaseModel):
    request_id: str
    items: list[ExternalEvidenceValidateResult]


def _store_for(pipeline: AppPipeline):
    store = getattr(getattr(pipeline, "backend", None), "store", None)
    if store is None:
        store = getattr(pipeline, "document_store", None)
    if store is None:
        raise ExternalApiError(503, "SOURCE_SCOPE_UNRESOLVED", "文档来源台账不可用", retryable=True)
    return store


def _list_scoped_records(store: Any, kb_name: str, department_id: object) -> list[Any]:
    try:
        return list(store.list_documents(kb_name, department_id=department_id))
    except Exception as exc:  # noqa: BLE001 - convert storage failure to a safe 503
        raise ExternalApiError(
            503,
            "SOURCE_SCOPE_UNRESOLVED",
            "文档来源台账暂不可用",
            retryable=True,
        ) from exc


def _get_scoped_record(store: Any, record_id: int, department_id: object) -> Any:
    try:
        return store.get_document_by_id_scoped(record_id, department_id)
    except Exception as exc:  # noqa: BLE001 - convert storage failure to a safe 503
        raise ExternalApiError(
            503,
            "SOURCE_SCOPE_UNRESOLVED",
            "文档来源台账暂不可用",
            retryable=True,
        ) from exc


def _archive_for(pipeline: AppPipeline) -> DocumentArchiveManager:
    archive = getattr(getattr(pipeline, "backend", None), "archive", None)
    return archive if archive is not None else DocumentArchiveManager()


def _safe_archive_path(archive: DocumentArchiveManager, record: Any) -> str | None:
    """Resolve an archived original without allowing path/symlink escape."""
    try:
        candidate = archive.resolve_record_path(record)
        root = os.path.realpath(archive.archive_root())
        resolved = os.path.realpath(candidate)
        if os.path.commonpath([root, resolved]) != root:
            return None
        if not os.path.isfile(resolved):
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_available(pipeline: AppPipeline, record: Any) -> bool:
    path = _safe_archive_path(_archive_for(pipeline), record)
    if path is None:
        return False
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_DOWNLOAD_BYTES or 0)
    try:
        return max_bytes <= 0 or os.path.getsize(path) <= max_bytes
    except OSError:
        return False


def _safe_upload_name(filename: str | None, used: set[str]) -> str:
    base = os.path.basename(str(filename or "upload")).strip() or "upload"
    if base in {".", ".."}:
        base = "upload"
    base = re.sub(r"[\r\n\x00]", "_", base)[:200] or "upload"
    if base not in used:
        used.add(base)
        return base
    stem, ext = os.path.splitext(base)
    index = 1
    while f"{stem}_{index}{ext}" in used:
        index += 1
    unique = f"{stem}_{index}{ext}"
    used.add(unique)
    return unique


def _upload_request_hash(
    entries: list[dict[str, Any]], kb_ref: str, source_group: str | None
) -> str:
    payload = {
        "kb_ref": kb_ref,
        "source_group": source_group,
        "files": [
            {"name": entry["file_name"], "sha256": entry["sha256"]}
            for entry in entries
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _find_uploaded_record(
    records: list[Any],
    *,
    authz: ExternalAuthorization,
    file_name: str,
    sha256: str,
) -> Any | None:
    candidates = [
        record
        for record in records
        if _record_in_kb(record, authz)
        and str(record.original_file_name or record.document_name or "") == file_name
        and _normalized_hash(record.content_hash) == sha256
    ]
    return max(candidates, key=lambda record: int(record.id)) if candidates else None


def _external_asset_version_ref(asset_id: int, version_no: int) -> str | None:
    secret = str(external_settings.HDB_API_KEY_PEPPER or "").strip()
    if not secret:
        return None
    digest = hmac.new(
        secret.encode("utf-8"),
        f"asset-version:{asset_id}:{version_no}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"versionref_{digest}"


def _asset_view(
    auth: AuthService,
    authz: ExternalAuthorization,
    asset: dict[str, Any],
) -> ExternalAssetView:
    asset_id = int(asset["id"])
    asset_ref = auth.ensure_external_asset_ref(authz.kb.kb_id, asset_id).asset_ref
    tags = asset.get("tags") if isinstance(asset.get("tags"), list) else []
    return ExternalAssetView(
        asset_ref=asset_ref,
        title=str(asset.get("title") or ""),
        doc_no=str(asset.get("doc_no") or ""),
        category=str(asset.get("category") or "other"),
        project=str(asset.get("project") or ""),
        description=str(asset.get("description") or ""),
        manufacturer=str(asset.get("manufacturer") or ""),
        mpn=str(asset.get("mpn") or ""),
        revision=str(asset.get("revision") or ""),
        tags=[str(tag) for tag in tags],
        lifecycle_status=str(asset.get("lifecycle_status") or "draft"),
        version_count=int(asset.get("version_count") or 0),
        effective_version_no=(
            int(asset["effective_version_no"])
            if asset.get("effective_version_no") is not None
            else None
        ),
        updated_at=str(asset.get("updated_at") or ""),
    )


def _asset_version_file_ref(
    auth: AuthService,
    authz: ExternalAuthorization,
    version: dict[str, Any],
) -> str | None:
    raw_file_id = str(version.get("file_id") or "").split(":")[-1].strip()
    if not raw_file_id.isdigit():
        return None
    try:
        return auth.ensure_external_file_ref(
            authz.kb.kb_id, int(raw_file_id)
        ).file_ref
    except Exception:  # noqa: BLE001 - version index remains usable without file_ref
        return None


def _asset_detail_view(
    auth: AuthService,
    authz: ExternalAuthorization,
    asset: dict[str, Any],
) -> ExternalAssetDetailView:
    base = _asset_view(auth, authz, asset)
    versions = []
    for version in asset.get("versions") or []:
        if not isinstance(version, dict):
            continue
        version_no = int(version.get("version_no") or 0)
        versions.append(
            ExternalAssetVersionView(
                version_ref=_external_asset_version_ref(
                    int(asset["id"]), version_no
                ),
                file_ref=_asset_version_file_ref(auth, authz, version),
                version_no=version_no,
                state=str(version.get("state") or ""),
                file_name=str(version.get("file_name") or ""),
                content_hash=str(version.get("content_hash") or ""),
                parse_status=str(version.get("parse_status") or ""),
                note=str(version.get("note") or ""),
                created_at=str(version.get("created_at") or ""),
            )
        )
    return ExternalAssetDetailView(**base.model_dump(), versions=versions)


def _external_table_ref(record_id: int, physical_table: str) -> str:
    salt = str(external_settings.HDB_API_KEY_PEPPER or "").strip()
    if salt:
        digest = hmac.new(
            salt.encode("utf-8"),
            f"table:{record_id}:{physical_table}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
    else:
        digest = hashlib.sha256(
            f"hdb-external-table:{record_id}:{physical_table}".encode("utf-8")
        ).hexdigest()[:24]
    return f"tableref_{digest}"


def _unit_from_header(header: str) -> str:
    match = re.search(r"[\(\[]([^\)\]]{1,24})[\)\]]", str(header or ""))
    return match.group(1).strip() if match else ""


def _spreadsheet_record_ready(record: Any) -> bool:
    return bool(
        record.content_hash
        and normalize_parse_status(record.status, record.processor_kind)
        == TASK_STATUS_COMPLETED
    )


def _table_registry_entries(
    auth: AuthService,
    authz: ExternalAuthorization,
    pipeline: AppPipeline,
) -> list[dict[str, Any]]:
    records = [
        record
        for record in _list_scoped_records(
            _store_for(pipeline), authz.kb.kb_name, authz.kb.department_id
        )
        if _record_in_kb(record, authz)
        and str(record.processor_kind or "") == PROCESSOR_KIND_SPREADSHEET
        and _spreadsheet_record_ready(record)
    ]
    records_by_id = {int(record.id): record for record in records}
    if not records_by_id:
        return []
    db_path = SpreadsheetIndexService().db_path(
        authz.kb.department_id, authz.kb.kb_name, create=False
    )
    entries: list[dict[str, Any]] = []
    for entry in _load_sql_registry(db_path):
        record = records_by_id.get(int(entry.get("record_id") or 0))
        if record is None:
            continue
        physical_table = str(entry.get("table_name") or "")
        if not physical_table:
            continue
        table_ref = _external_table_ref(int(record.id), physical_table)
        file_ref = auth.ensure_external_file_ref(
            authz.kb.kb_id, int(record.id)
        ).file_ref
        columns: list[ExternalTableColumnView] = []
        for column in entry.get("columns") or []:
            if not isinstance(column, dict):
                continue
            header = str(column.get("header") or "")
            columns.append(
                ExternalTableColumnView(
                    key=str(column.get("column") or ""),
                    header=header,
                    dtype=str(column.get("dtype") or "TEXT"),
                    unit=_unit_from_header(header),
                    samples=[
                        str(sample)
                        for sample in (column.get("samples") or [])[:3]
                    ],
                )
            )
        entries.append(
            {
                **entry,
                "record": record,
                "physical_table": physical_table,
                "table_ref": table_ref,
                "file_ref": file_ref,
                "file_name": str(
                    record.original_file_name or record.document_name or ""
                ),
                "external_columns": columns,
            }
        )
    return entries


def _external_circuit_ref(design_id: str) -> str:
    salt = str(external_settings.HDB_API_KEY_PEPPER or "").strip()
    material = f"circuit:{design_id}".encode("utf-8")
    if salt:
        digest = hmac.new(
            salt.encode("utf-8"), material, hashlib.sha256
        ).hexdigest()[:24]
    else:
        digest = hashlib.sha256(b"hdb-external-circuit:" + material).hexdigest()[:24]
    return f"circuitref_{digest}"


def _scoped_circuit_design_ids(
    authz: ExternalAuthorization,
) -> set[str]:
    store = CircuitStore()
    service = CircuitIndexService(store=store)
    allowed: set[str] = set()
    for design in store.list_designs(authz.kb.kb_name):
        try:
            metadata = service._read_metadata(  # noqa: SLF001 - same-domain scope check
                authz.kb.kb_name, design.design_id
            )
        except Exception:  # noqa: BLE001 - uncertain scope must not widen
            continue
        if str(metadata.get("department_id") or "") == str(authz.kb.department_id):
            allowed.add(str(design.design_id))
    return allowed


def _sanitize_circuit_rows(kind: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for row in rows:
        design_id = str(row.get("design_id") or "")
        circuit_ref = _external_circuit_ref(design_id) if design_id else ""
        if kind == "instances":
            sanitized.append(
                {
                    "circuit_ref": circuit_ref,
                    "refdes": row.get("refdes", ""),
                    "library_cell": row.get("library_cell", ""),
                    "part_number": row.get("part_number", ""),
                    "footprint": row.get("footprint", ""),
                    "value": row.get("value", ""),
                    "erp_number": row.get("erp_number", ""),
                    "pin_count": row.get("pin_count", 0),
                    "pins": row.get("pins", []),
                }
            )
        elif kind == "nets":
            sanitized.append(
                {
                    "circuit_ref": circuit_ref,
                    "name": row.get("name", ""),
                    "net_type": row.get("net_type", ""),
                    "connection_count": row.get("connection_count", 0),
                    "connections": row.get("connections", []),
                }
            )
        elif kind == "modules":
            sanitized.append(
                {
                    "circuit_ref": circuit_ref,
                    "name": row.get("name", ""),
                    "strategy": row.get("strategy", ""),
                    "instance_count": row.get("instance_count", 0),
                    "net_count": row.get("net_count", 0),
                    "instances": row.get("instances", []),
                    "nets": row.get("nets", []),
                    "connectivity_description": row.get(
                        "connectivity_description", ""
                    ),
                    "visual_description": row.get("visual_description", ""),
                    "merged_description": row.get("merged_description", ""),
                }
            )
        else:
            sanitized.append(
                {
                    "circuit_ref": circuit_ref,
                    "edf_refdes": row.get("edf_refdes", ""),
                    "pdf_label": row.get("pdf_label", ""),
                    "page_number": row.get("page_number"),
                    "confidence": row.get("confidence", 0),
                    "strategy": row.get("strategy", ""),
                }
            )
    return sanitized


def _query_column_view(
    key: str,
    value: Any,
    entries: list[dict[str, Any]],
) -> ExternalTableColumnView:
    for entry in entries:
        for column in entry.get("external_columns") or []:
            if column.key == key:
                return column.model_copy(update={"samples": []})
    if isinstance(value, bool):
        dtype = "INTEGER"
    elif isinstance(value, int):
        dtype = "INTEGER"
    elif isinstance(value, float):
        dtype = "REAL"
    else:
        dtype = "TEXT"
    return ExternalTableColumnView(
        key=key,
        header=key,
        dtype=dtype,
        samples=[],
    )


def _job_view(
    auth: AuthService,
    authz: ExternalAuthorization,
    job: Any,
    record: Any,
) -> ExternalIngestionJobView:
    status = _external_file_status(record)
    try:
        progress = max(0, min(100, int(record.parse_progress or 0)))
    except (TypeError, ValueError):
        progress = 0
    if status == "ready":
        progress = 100
    success = status in {"ready", "processing"}
    return ExternalIngestionJobView(
        job_id=str(job.job_ref),
        file_ref=auth.ensure_external_file_ref(
            authz.kb.kb_id, int(record.id)
        ).file_ref,
        file_name=str(record.original_file_name or record.document_name or ""),
        status=status,
        progress=progress,
        success=success,
        error_code=None if success else "INGESTION_FAILED",
    )


def _normalized_hash(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _display_hash(value: object) -> str:
    normalized = _normalized_hash(value)
    return f"sha256:{normalized}" if normalized else ""


def _record_is_ready(record: Any, *, allow_non_ragflow: bool = False) -> bool:
    if not allow_non_ragflow and str(record.processor_kind or "") != PROCESSOR_KIND_RAGFLOW:
        return False
    return bool(
        record.document_id
        and _normalized_hash(record.content_hash)
        and normalize_parse_status(record.status, record.processor_kind) == TASK_STATUS_COMPLETED
    )


def _record_in_kb(record: Any, authz: ExternalAuthorization) -> bool:
    return bool(
        str(record.kb_name) == authz.kb.kb_name
        and str(record.department_id) == str(authz.kb.department_id)
        and (record.kb_id in (0, None, authz.kb.kb_id))
    )


def _not_ready_error(record: Any) -> ExternalApiError:
    normalized = normalize_parse_status(record.status, record.processor_kind)
    if normalized in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_PAUSED}:
        return ExternalApiError(
            503,
            "SOURCE_SCOPE_UNRESOLVED",
            "来源尚未达到可检索状态",
            retryable=True,
        )
    return ExternalApiError(
        422,
        "SOURCE_SCOPE_UNRESOLVED",
        "来源解析失败或缺失可检索身份",
        retryable=False,
    )


def _external_file_status(record: Any) -> str:
    """Map internal parse states onto the External protocol vocabulary."""
    if str(record.processor_kind or "") != PROCESSOR_KIND_RAGFLOW:
        return "unsupported"
    normalized = normalize_parse_status(record.status, record.processor_kind)
    if normalized == TASK_STATUS_COMPLETED and _record_is_ready(record):
        return "ready"
    if normalized in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_PAUSED}:
        return "processing"
    if normalized in {TASK_STATUS_FAILED, TASK_STATUS_DEAD_LETTER, TASK_STATUS_CANCELLED}:
        return "failed"
    return "unavailable"


def _file_view(
    auth: AuthService,
    authz: ExternalAuthorization,
    record: Any,
    pipeline: AppPipeline,
) -> ExternalFileView:
    file_ref = auth.ensure_external_file_ref(authz.kb.kb_id, int(record.id)).file_ref
    return ExternalFileView(
        file_ref=file_ref,
        file_name=str(record.original_file_name or record.document_name or ""),
        file_size=max(0, int(record.file_size or 0)),
        content_hash=_display_hash(record.content_hash) or None,
        status=_external_file_status(record),
        download_available=_download_available(pipeline, record),
    )


def _check_top_k(top_k: int) -> int:
    maximum = int(external_settings.HDB_EXTERNAL_MAX_TOP_K or 20)
    if top_k > maximum:
        raise ExternalApiError(422, "INVALID_REQUEST", f"top_k 不能超过 {maximum}")
    return top_k


def _resolve_records(
    body: ExternalRetrieveRequest,
    authz: ExternalAuthorization,
    auth: AuthService,
    pipeline: AppPipeline,
) -> tuple[list[Any], dict[int, str]]:
    store = _store_for(pipeline)
    scope = body.scope
    refs = [str(value).strip() for value in scope.file_refs]
    if len(refs) != len(set(refs)):
        raise ExternalApiError(422, "INVALID_REQUEST", "file_refs 不能重复")
    if scope.mode == "files" and not refs:
        raise ExternalApiError(422, "SOURCE_SCOPE_REQUIRED", "files 模式必须提供 file_refs")
    if scope.mode == "kb" and refs:
        raise ExternalApiError(422, "INVALID_REQUEST", "kb 模式不能提交 file_refs")
    if len(refs) > int(external_settings.HDB_EXTERNAL_MAX_FILE_REFS or 50):
        raise ExternalApiError(422, "INVALID_REQUEST", "file_refs 数量超过限制")
    if any(not value for value in refs):
        raise ExternalApiError(422, "INVALID_REQUEST", "file_refs 不能包含空值")
    if set(scope.expected_content_hashes) - set(refs):
        raise ExternalApiError(422, "INVALID_REQUEST", "expected_content_hashes 必须是 file_refs 的子集")
    if any(not _normalized_hash(value) for value in scope.expected_content_hashes.values()):
        raise ExternalApiError(422, "INVALID_REQUEST", "expected_content_hashes 必须是 SHA-256")

    records: list[Any] = []
    file_ref_by_record: dict[int, str] = {}
    if scope.mode == "files":
        for file_ref in refs:
            mapping = auth.get_external_file_ref(file_ref)
            if mapping is None or mapping.kb_id != authz.kb.kb_id:
                raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
            record = _get_scoped_record(store, mapping.record_id, authz.kb.department_id)
            if record is None or not _record_in_kb(record, authz):
                raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
            if str(record.processor_kind or "") != PROCESSOR_KIND_RAGFLOW:
                raise ExternalApiError(422, "CAPABILITY_NOT_ENABLED", "P0 暂不支持该文件类型的检索")
            expected = scope.expected_content_hashes.get(file_ref)
            if expected is not None and _normalized_hash(expected) != _normalized_hash(record.content_hash):
                raise ExternalApiError(409, "SOURCE_HASH_CONFLICT", "来源内容哈希不匹配")
            if not _record_is_ready(record):
                raise _not_ready_error(record)
            records.append(record)
            file_ref_by_record[int(record.id)] = file_ref
    else:
        all_records = [
            record
            for record in _list_scoped_records(
                store, authz.kb.kb_name, authz.kb.department_id
            )
            if _record_in_kb(record, authz)
        ]
        # Whole-KB scope must not silently narrow itself to whichever records
        # happen to use the text processor.  If the KB contains a structured
        # file, require an explicit files scope until the P2 adapter exists.
        if any(str(record.processor_kind or "") != PROCESSOR_KIND_RAGFLOW for record in all_records):
            raise ExternalApiError(
                422,
                "CAPABILITY_NOT_ENABLED",
                "P0 暂不支持包含结构化文件的知识库全库检索；请使用 files 模式",
            )
        ragflow_records = [
            record for record in all_records if str(record.processor_kind or "") == PROCESSOR_KIND_RAGFLOW
        ]
        if not ragflow_records:
            raise ExternalApiError(
                503,
                "SOURCE_SCOPE_UNRESOLVED",
                "知识库来源集合为空或尚未完全可解析",
                retryable=True,
            )
        for record in ragflow_records:
            if not _record_is_ready(record):
                raise _not_ready_error(record)
        records = ragflow_records
        for record in records:
            file_ref_by_record[int(record.id)] = auth.ensure_external_file_ref(
                authz.kb.kb_id, int(record.id)
            ).file_ref

    if not records:
        raise ExternalApiError(503, "SOURCE_SCOPE_UNRESOLVED", "无法确定来源范围", retryable=True)
    return records, file_ref_by_record


def _resolve_source_scope(
    scope: Any,
    authz: ExternalAuthorization,
    auth: AuthService,
    store: Any,
) -> tuple[list[Any], dict[int, str]]:
    if int(scope.kb_id) != authz.kb.kb_id:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在")
    records: list[Any] = []
    file_ref_by_record: dict[int, str] = {}
    for source in scope.sources:
        if not isinstance(source, dict):
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在")
        file_ref = str(source.get("file_ref") or "")
        try:
            record_id = int(source.get("record_id") or 0)
        except (TypeError, ValueError) as exc:
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在") from exc
        mapping = auth.get_external_file_ref(file_ref)
        if (
            mapping is None
            or mapping.kb_id != authz.kb.kb_id
            or mapping.record_id != record_id
        ):
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在")
        record = _get_scoped_record(store, record_id, authz.kb.department_id)
        if record is None or not _record_in_kb(record, authz):
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在")
        if not _record_is_ready(record):
            raise _not_ready_error(record)
        if _normalized_hash(record.content_hash) != _normalized_hash(
            source.get("content_hash")
        ):
            raise ExternalApiError(409, "SOURCE_HASH_CONFLICT", "来源范围内容已变化")
        if record_id in file_ref_by_record:
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在")
        records.append(record)
        file_ref_by_record[record_id] = file_ref
    if not records:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在")
    return records, file_ref_by_record


def _contract_for(authz: ExternalAuthorization, records: list[Any]) -> DocumentRetrievalContract:
    allowed_ids = tuple(sorted(int(record.id) for record in records))
    stamps = {
        int(record.id): DocumentSourceStamp(
            kb_id=authz.kb.kb_id,
            local_record_id=int(record.id),
            # The internal scoped contract compares the stamp byte-for-byte
            # with the pipeline ledger.  Normalize only at the HTTP boundary;
            # do not rewrite the ledger's stored representation here.
            source_content_hash=str(record.content_hash or ""),
            remote_document_id=str(record.document_id),
            index_revision=int(getattr(record, "index_revision", 0) or 0),
            source_version_id=str(record.source_version_id or "") or None,
            revision=str(record.revision or "") or None,
            processing_artifact_id=str(getattr(record, "processing_artifact_id", "") or "") or None,
            processor_version=str(getattr(record, "processor_version", "") or "") or None,
        )
        for record in records
    }
    return DocumentRetrievalContract(
        mode="frozen", allowed_record_ids=allowed_ids, expected_source_stamps=stamps
    )


def _external_chunk_ref(record_id: int, locator: object) -> str | None:
    """Derive a stable, opaque chunk reference without storing a mapping.

    The internal chunk id never leaves the process; the HMAC output is
    deterministic because the P1 chunk listing needs a stable locator while
    the response itself remains response-local.  Without a pepper we emit
    null rather than inventing an unstable identifier.
    """
    text = str(locator or "").strip()
    secret = str(external_settings.HDB_API_KEY_PEPPER or "").strip()
    if not text or not secret:
        return None
    digest = hmac.new(
        secret.encode("utf-8"),
        f"chunk:{record_id}:{text}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"chunkref_{digest}"


def _scoped_error(status: str, code: str, message: str = "检索未完成") -> ExternalApiError:
    code_map = {
        "access_denied": (403, "KB_ACCESS_DENIED", False),
        "source_changed": (409, "SOURCE_HASH_CONFLICT", False),
        "filter_unsupported": (422, "SOURCE_SCOPE_UNRESOLVED", False),
        "timeout": (504, "RETRIEVAL_TIMEOUT", True),
        "source_unavailable": (503, "SOURCE_SCOPE_UNRESOLVED", True),
        "retrieval_failed": (503, "RETRIEVAL_BACKEND_UNAVAILABLE", True),
        "cancelled": (499, "RETRIEVAL_CANCELLED", False),
        "budget_exhausted": (429, "RATE_LIMITED", True),
    }
    status_code, default_code, retryable = code_map.get(
        status, (503, "RETRIEVAL_BACKEND_UNAVAILABLE", True)
    )
    safe_code = default_code
    if code and code in {"source_identity_missing", "frozen_dataset_mapping_missing"}:
        safe_code = "SOURCE_SCOPE_UNRESOLVED"
    return ExternalApiError(
        status_code,
        safe_code,
        message,
        retryable=retryable,
    )


def _outcome_error(outcome: Any) -> ExternalApiError:
    return _scoped_error(
        str(getattr(outcome, "status", "retrieval_failed")),
        str(getattr(outcome, "error_code", "") or ""),
    )


def _evidence_view(
    evidence: Evidence,
    *,
    request: Request,
    allowed_records: dict[int, Any],
    file_ref_by_record: dict[int, str],
    store: Any,
    authz: ExternalAuthorization,
    auth: AuthService,
    source_scope_ref: str | None = None,
) -> ExternalEvidenceView:
    raw_record_id = evidence.metadata.get("local_record_id")
    try:
        record_id = int(raw_record_id)
    except (TypeError, ValueError) as exc:
        raise ExternalApiError(503, "SOURCE_SCOPE_UNVERIFIED", "检索结果来源校验失败") from exc
    if record_id not in allowed_records or record_id not in file_ref_by_record:
        raise ExternalApiError(503, "SOURCE_SCOPE_UNVERIFIED", "检索结果来源校验失败")
    current = _get_scoped_record(store, record_id, authz.kb.department_id)
    expected = allowed_records[record_id]
    if (
        current is None
        or not _record_in_kb(current, authz)
        or not _record_is_ready(current)
        or _normalized_hash(current.content_hash) != _normalized_hash(expected.content_hash)
        or str(current.document_id) != str(expected.document_id)
    ):
        raise ExternalApiError(503, "SOURCE_SCOPE_UNVERIFIED", "检索结果来源校验失败")
    content = str(evidence.content or "")
    expected_text_hash = _normalized_hash(evidence.metadata.get("evidence_text_hash"))
    if expected_text_hash and expected_text_hash != hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest():
        raise ExternalApiError(503, "SOURCE_SCOPE_UNVERIFIED", "检索结果内容校验失败")
    request_key = request_id(request) or "request"
    locator_value = (
        evidence.metadata.get("chunk_or_structured_locator")
        or evidence.metadata.get("chunk_id")
        or evidence.id
        or ""
    )
    digest = hashlib.sha256(
        f"{request_key}:{record_id}:{locator_value}:{len(content)}:{content}".encode("utf-8")
    ).hexdigest()
    chunk_ref = _external_chunk_ref(record_id, locator_value)
    page = evidence.metadata.get("page")
    if not isinstance(page, int) or page < 1:
        page = None
    locator = {
        "page": page,
        "chunk_ref": chunk_ref,
    }
    file_name = str(current.original_file_name or current.document_name or "")
    evidence_id = f"evidence_{digest[:24]}"
    if source_scope_ref:
        try:
            evidence_id = auth.save_external_evidence(
                key_id=authz.principal.key_id,
                scope_ref=source_scope_ref,
                kb_ref=authz.kb.kb_ref,
                kb_id=authz.kb.kb_id,
                department_id=int(authz.kb.department_id or 0),
                record_id=record_id,
                file_ref=file_ref_by_record[record_id],
                file_name=file_name,
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                source_content_hash=str(current.content_hash or ""),
                locator=locator,
                score=float(evidence.score or 0.0),
                backend="ragflow",
            )
        except Exception as exc:  # noqa: BLE001 - persistent evidence is required with a scope
            raise ExternalApiError(
                503,
                "INGESTION_BACKEND_UNAVAILABLE",
                "证据持久化暂不可用",
                retryable=True,
            ) from exc
    return ExternalEvidenceView(
        evidence_id=evidence_id,
        content=content,
        source=ExternalSourceView(
            file_ref=file_ref_by_record[record_id],
            file_name=file_name,
            content_hash=_display_hash(current.content_hash),
            source_version_ref=None,
            locator=locator,
        ),
        retrieval=ExternalRetrievalView(
            type="document",
            backend="ragflow",
            score=float(evidence.score or 0.0),
        ),
    )


@router.get("/capabilities", response_model=ExternalCapabilityView)
def capabilities(
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
) -> ExternalCapabilityView:
    enforce_external_rate_limit(principal)
    audit_external(
        auth,
        action="external_capabilities",
        principal=principal,
        request=request,
        success=True,
    )
    return ExternalCapabilityView(
        request_id=request_id(request),
        capabilities=[
            "document_claim_lookup",
            "file_download",
            "chunk_listing",
            "file_upload",
            "ingestion_job",
            "asset_index",
            "asset_version",
            "table_schema",
            "table_query",
            "circuit_query",
            "batch_evidence",
            "source_scope",
            "evidence_validation",
            "evidence_readback",
        ],
        limits={
            "max_top_k": int(external_settings.HDB_EXTERNAL_MAX_TOP_K or 20),
            "max_file_refs": int(external_settings.HDB_EXTERNAL_MAX_FILE_REFS or 50),
            "max_request_bytes": int(external_settings.HDB_EXTERNAL_MAX_REQUEST_BYTES or 0),
            "max_response_bytes": int(external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0),
            "max_download_bytes": int(
                external_settings.HDB_EXTERNAL_MAX_DOWNLOAD_BYTES or 0
            ),
            "max_upload_bytes": int(
                external_settings.HDB_EXTERNAL_MAX_UPLOAD_BYTES or 0
            ),
            "max_upload_files": int(external_settings.HDB_EXTERNAL_MAX_UPLOAD_FILES or 20),
            "max_batch_items": int(external_settings.HDB_EXTERNAL_MAX_BATCH_ITEMS or 20),
            "chunk_page_size": _DEFAULT_CHUNK_PAGE_SIZE,
            "max_chunk_response_bytes": int(
                external_settings.HDB_EXTERNAL_CHUNK_MAX_BYTES or 0
            ),
            "rate_limit_per_minute": int(
                external_settings.HDB_EXTERNAL_RATE_LIMIT_PER_MINUTE or 0
            ),
            "daily_quota": int(external_settings.HDB_EXTERNAL_DAILY_QUOTA or 0),
            "concurrency_limit": int(external_settings.HDB_EXTERNAL_CONCURRENCY_LIMIT or 0),
        },
    )


@router.get("/kbs", response_model=list[ExternalKbView])
def list_external_kbs(
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
) -> list[ExternalKbView]:
    enforce_external_rate_limit(principal)
    if "read" not in principal.scopes:
        raise ExternalApiError(403, "KEY_SCOPE_DENIED", "API Key 缺少 read 权限")
    if principal.kb_access_mode == "all_current_accessible_kbs" and not bool(
        external_settings.HDB_EXTERNAL_ALL_KBS_MODE_ENABLED
    ):
        raise ExternalApiError(403, "KEY_SCOPE_DENIED", "API Key 的访问模式已关闭")
    user = auth.get_user_by_id(principal.user_id)
    if user is None or not user.is_active or user.role != "employee":
        raise ExternalApiError(401, "AUTH_INVALID", "绑定员工账号已停用")
    result: list[ExternalKbView] = []
    for identity in auth.list_accessible_knowledge_base_identities(user):
        if principal.kb_access_mode == "selected_kbs" and identity.kb_id not in principal.kb_ids:
            continue
        ref = auth.ensure_external_kb_ref(identity.kb_id)
        result.append(ExternalKbView(kb_ref=ref.kb_ref, name=ref.kb_name))
    audit_external(auth, action="external_list_kbs", principal=principal, request=request, success=True)
    return result


@router.get("/kbs/{kb_ref}/files", response_model=ExternalFilePage)
def list_external_files(
    kb_ref: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    status: Literal[
        "ready", "processing", "failed", "unsupported", "unavailable"
    ]
    | None = Query(default=None),
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalFilePage:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="read", request=request)
    store = _store_for(pipeline)
    records = [
        record
        for record in _list_scoped_records(
            store, authz.kb.kb_name, authz.kb.department_id
        )
        if _record_in_kb(record, authz)
        and (status is None or _external_file_status(record) == status)
    ]
    records.sort(key=lambda record: str(record.original_file_name or record.document_name or ""))
    page_records = records[offset : offset + limit]
    page = ExternalFilePage(
        items=[
            _file_view(auth, authz, record, pipeline) for record in page_records
        ],
        offset=offset,
        limit=limit,
        total=len(records),
    )
    audit_external(
        auth,
        action="external_list_files",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"count": len(page.items)},
    )
    return page


@router.get("/kbs/{kb_ref}/files/{file_ref}", response_model=ExternalFileView)
def get_external_file(
    kb_ref: str,
    file_ref: str,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalFileView:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="read", request=request)
    mapping = auth.get_external_file_ref(file_ref)
    if mapping is None or mapping.kb_id != authz.kb.kb_id:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    store = _store_for(pipeline)
    record = _get_scoped_record(store, mapping.record_id, authz.kb.department_id)
    if record is None or not _record_in_kb(record, authz):
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    view = _file_view(auth, authz, record, pipeline)
    audit_external(
        auth,
        action="external_get_file",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"file_ref": file_ref},
    )
    return view


@router.post(
    "/kbs/{kb_ref}/files",
    response_model=ExternalUploadResponse,
    status_code=202,
)
async def upload_external_files(
    kb_ref: str,
    request: Request,
    files: list[UploadFile] = File(...),
    source_group: str | None = Form(default=None),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalUploadResponse:
    enforce_external_rate_limit(principal)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", idempotency_key):
        raise ExternalApiError(422, "INVALID_REQUEST", "Idempotency-Key 格式不合法")
    authz = authorize_kb(
        principal,
        kb_ref,
        auth=auth,
        required_scope="write",
        required_kb_permission="write",
        request=request,
    )
    if not files:
        raise ExternalApiError(422, "INVALID_REQUEST", "至少需要一个文件")
    max_files = int(external_settings.HDB_EXTERNAL_MAX_UPLOAD_FILES or 20)
    if len(files) > max_files:
        raise ExternalApiError(422, "INVALID_REQUEST", "文件数量超过限制")
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_UPLOAD_BYTES or 0)
    tmp_dir = tempfile.mkdtemp(prefix="hdb_external_upload_")
    entries: list[dict[str, Any]] = []
    claimed = False
    try:
        used_names: set[str] = set()
        total_bytes = 0
        for upload in files:
            file_name = _safe_upload_name(upload.filename, used_names)
            path = os.path.join(tmp_dir, file_name)
            digest = hashlib.sha256()
            size = 0
            with open(path, "wb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if max_bytes > 0 and total_bytes > max_bytes:
                        raise ExternalApiError(
                            413, "RESPONSE_TOO_LARGE", "上传总大小超过限制"
                        )
                    size += len(chunk)
                    digest.update(chunk)
                    handle.write(chunk)
            entries.append(
                {
                    "file_name": file_name,
                    "path": path,
                    "size": size,
                    "sha256": digest.hexdigest(),
                }
            )

        request_hash = _upload_request_hash(entries, kb_ref, source_group)
        claim, stored_response = auth.claim_external_upload(
            key_id=principal.key_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            kb_id=authz.kb.kb_id,
        )
        if claim == "conflict":
            raise ExternalApiError(
                409, "IDEMPOTENCY_CONFLICT", "同一 Idempotency-Key 对应不同内容"
            )
        if claim == "in_progress":
            raise ExternalApiError(
                409, "IDEMPOTENCY_CONFLICT", "同一 Idempotency-Key 正在处理中"
            )
        if claim == "replay":
            try:
                payload = json.loads(stored_response)
                payload["request_id"] = request_id(request)
                replay_response = ExternalUploadResponse(**payload)
            except (TypeError, ValueError) as exc:
                raise ExternalApiError(
                    503, "INGESTION_BACKEND_UNAVAILABLE", "幂等结果不可用", retryable=True
                ) from exc
            audit_external(
                auth,
                action="external_upload_files_replayed",
                principal=principal,
                request=request,
                success=True,
                kb_ref=kb_ref,
                metadata={"status": replay_response.status, "replayed": True},
            )
            return replay_response
        claimed = True

        supported: list[dict[str, Any]] = []
        file_views_by_name: dict[str, ExternalUploadFileView] = {}
        unsupported: list[dict[str, Any]] = []
        for entry in entries:
            if (
                supported_pipeline_for_file(entry["path"])
                != PROCESSOR_KIND_RAGFLOW
            ):
                unsupported.append(entry)
            else:
                supported.append(entry)
        if supported:
            try:
                await run_in_threadpool(
                    pipeline.upload_files,
                    [entry["path"] for entry in supported],
                    authz.kb.kb_name,
                    authz.context,
                    source_group,
                )
            except Exception as exc:  # noqa: BLE001 - safe external error contract
                raise ExternalApiError(
                    503,
                    "INGESTION_BACKEND_UNAVAILABLE",
                    "上传处理暂不可用",
                    retryable=True,
                ) from exc
            records = _list_scoped_records(
                _store_for(pipeline), authz.kb.kb_name, authz.kb.department_id
            )
        else:
            records = []
        for entry in unsupported:
            file_views_by_name[entry["file_name"]] = ExternalUploadFileView(
                file_name=entry["file_name"],
                status="failed",
                success=False,
                error_code="CAPABILITY_NOT_ENABLED",
            )
        for entry in supported:
            record = _find_uploaded_record(
                records,
                authz=authz,
                file_name=entry["file_name"],
                sha256=entry["sha256"],
            )
            if record is None:
                file_views_by_name[entry["file_name"]] = ExternalUploadFileView(
                    file_name=entry["file_name"],
                    status="failed",
                    success=False,
                    error_code="INGESTION_FAILED",
                )
                continue
            job = auth.ensure_external_job_ref(
                key_id=principal.key_id,
                kb_ref=kb_ref,
                kb_id=authz.kb.kb_id,
                record_id=int(record.id),
            )
            view = _job_view(auth, authz, job, record)
            file_views_by_name[entry["file_name"]] = ExternalUploadFileView(
                file_name=entry["file_name"],
                file_ref=view.file_ref,
                job_id=view.job_id,
                status=view.status,
                success=view.success,
                error_code=view.error_code,
            )
        file_views = [file_views_by_name[entry["file_name"]] for entry in entries]
        success_count = sum(1 for item in file_views if item.success)
        if success_count == len(file_views):
            overall = "accepted"
        elif success_count:
            overall = "partial"
        else:
            overall = "failed"
        response = ExternalUploadResponse(
            request_id=request_id(request),
            status=overall,
            files=file_views,
            warnings=[],
        )
        max_response_bytes = int(
            external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0
        )
        if max_response_bytes and len(
            json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8")
        ) > max_response_bytes:
            raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "上传响应超过大小限制")
        auth.finish_external_upload(
            key_id=principal.key_id,
            idempotency_key=idempotency_key,
            response_json=json.dumps(response.model_dump(), ensure_ascii=False),
        )
        audit_external(
            auth,
            action="external_upload_files",
            principal=principal,
            request=request,
            success=overall != "failed",
            kb_ref=kb_ref,
            metadata={"file_count": len(file_views), "status": overall},
        )
        return response
    except Exception:
        if claimed:
            auth.abort_external_upload(
                key_id=principal.key_id, idempotency_key=idempotency_key
            )
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("/ingestion-jobs/{job_id}", response_model=ExternalIngestionJobView)
def get_external_ingestion_job(
    job_id: str,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalIngestionJobView:
    enforce_external_rate_limit(principal)
    if "read" in principal.scopes:
        required_scope = "read"
    elif "write" in principal.scopes:
        required_scope = "write"
    else:
        raise ExternalApiError(403, "KEY_SCOPE_DENIED", "API Key 缺少任务查询权限")
    job = auth.get_external_job(job_id, principal.key_id)
    if job is None:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "任务不存在")
    authz = authorize_kb(
        principal,
        job.kb_ref,
        auth=auth,
        required_scope=required_scope,
        required_kb_permission="read",
        request=request,
    )
    record = _get_scoped_record(
        _store_for(pipeline), job.record_id, authz.kb.department_id
    )
    if record is None or not _record_in_kb(record, authz):
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "任务不存在")
    view = _job_view(auth, authz, job, record)
    audit_external(
        auth,
        action="external_get_ingestion_job",
        principal=principal,
        request=request,
        success=True,
        kb_ref=job.kb_ref,
        metadata={"job_id": job.job_ref, "status": view.status},
    )
    return view


def _stored_evidence_view(record: Any, *, status: str) -> ExternalStoredEvidenceView:
    return ExternalStoredEvidenceView(
        evidence_id=str(record.evidence_id),
        content=str(record.content),
        source=ExternalSourceView(
            file_ref=str(record.file_ref),
            file_name=str(record.file_name),
            content_hash=_display_hash(record.source_content_hash),
            source_version_ref=None,
            locator=record.locator,
        ),
        retrieval=ExternalRetrievalView(
            type="document",
            backend=str(record.backend or "ragflow"),
            score=float(record.score or 0.0),
        ),
        status=status,
        created_at=str(record.created_at),
        expires_at=str(record.expires_at),
    )


def _validate_evidence_record(
    record: Any,
    *,
    auth: AuthService,
    principal: EmployeeApiKeyPrincipal,
    store: Any,
    request: Request,
    required_terms: list[str],
    required_fields: list[str],
    expected_source_hash: str | None,
    expected_content_hash: str | None,
) -> ExternalEvidenceValidateResult:
    if "read" in principal.scopes:
        required_scope = "read"
    elif "invoke" in principal.scopes:
        required_scope = "invoke"
    elif "write" in principal.scopes:
        required_scope = "write"
    else:
        return ExternalEvidenceValidateResult(
            evidence_id=str(record.evidence_id),
            status="access_denied",
            supported=False,
            source_current=False,
        )
    try:
        authz = authorize_kb(
            principal,
            record.kb_ref,
            auth=auth,
            required_scope=required_scope,
            required_kb_permission="read",
            request=request,
        )
    except ExternalApiError:
        return ExternalEvidenceValidateResult(
            evidence_id=str(record.evidence_id),
            status="access_denied",
            supported=False,
            source_current=False,
        )
    current = _get_scoped_record(store, record.record_id, authz.kb.department_id)
    source_current = bool(
        current is not None
        and _record_in_kb(current, authz)
        and _record_is_ready(current)
        and _normalized_hash(current.content_hash)
        == _normalized_hash(record.source_content_hash)
    )
    content_ok = hashlib.sha256(str(record.content).encode("utf-8")).hexdigest() == str(
        record.content_hash
    )
    expected_source_ok = True
    if expected_source_hash:
        expected_source_ok = _normalized_hash(expected_source_hash) == _normalized_hash(
            record.source_content_hash
        )
    expected_content_ok = True
    if expected_content_hash:
        expected_content_ok = _normalized_hash(
            expected_content_hash
        ) == _normalized_hash(record.content_hash)
    content_text = str(record.content).casefold()
    missing_terms = [
        term
        for term in required_terms
        if str(term).strip() and str(term).casefold() not in content_text
    ]
    missing_fields = [
        field
        for field in required_fields
        if str(field).strip() and str(field).casefold() not in content_text
    ]
    if current is None:
        status = "source_missing"
    elif not source_current or not expected_source_ok:
        status = "source_changed"
    elif not content_ok or not expected_content_ok:
        status = "content_integrity_failed"
    elif missing_fields:
        status = "missing_fields"
    elif missing_terms:
        status = "missing_terms"
    else:
        status = "supported"
    return ExternalEvidenceValidateResult(
        evidence_id=str(record.evidence_id),
        status=status,
        supported=status == "supported",
        source_current=source_current and expected_source_ok,
        missing_terms=missing_terms,
        missing_fields=missing_fields,
    )


@router.post("/source-scopes", response_model=ExternalSourceScopeView, status_code=201)
def create_external_source_scope(
    body: ExternalSourceScopeRequest,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalSourceScopeView:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(
        principal,
        body.kb_ref,
        auth=auth,
        required_scope="invoke",
        required_kb_permission="read",
        request=request,
    )
    file_refs = list(
        dict.fromkeys(str(ref).strip() for ref in body.file_refs if str(ref).strip())
    )
    if not file_refs:
        raise ExternalApiError(422, "SOURCE_SCOPE_REQUIRED", "file_refs 不能为空")
    max_refs = int(external_settings.HDB_EXTERNAL_MAX_FILE_REFS or 50)
    if len(file_refs) > max_refs:
        raise ExternalApiError(422, "INVALID_REQUEST", "file_refs 数量超过限制")
    store = _store_for(pipeline)
    sources: list[dict[str, Any]] = []
    external_files: list[ExternalSourceScopeFileView] = []
    for file_ref in file_refs:
        mapping = auth.get_external_file_ref(file_ref)
        if mapping is None or mapping.kb_id != authz.kb.kb_id:
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
        record = _get_scoped_record(store, mapping.record_id, authz.kb.department_id)
        if record is None or not _record_in_kb(record, authz):
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
        if not _record_is_ready(record):
            raise _not_ready_error(record)
        sources.append(
            {
                "file_ref": file_ref,
                "record_id": int(record.id),
                "content_hash": str(record.content_hash or ""),
                "file_name": str(
                    record.original_file_name or record.document_name or ""
                ),
            }
        )
        external_files.append(
            ExternalSourceScopeFileView(
                file_ref=file_ref,
                file_name=str(record.original_file_name or record.document_name or ""),
                content_hash=_display_hash(record.content_hash),
            )
        )
    scope = auth.create_external_source_scope(
        key_id=principal.key_id,
        kb_ref=body.kb_ref,
        kb_id=authz.kb.kb_id,
        department_id=int(authz.kb.department_id or 0),
        sources=sources,
        ttl_seconds=body.ttl_seconds,
    )
    audit_external(
        auth,
        action="external_create_source_scope",
        principal=principal,
        request=request,
        success=True,
        kb_ref=body.kb_ref,
        metadata={"scope_ref": scope.scope_ref, "file_count": len(sources)},
    )
    return ExternalSourceScopeView(
        scope_ref=scope.scope_ref,
        kb_ref=scope.kb_ref,
        files=external_files,
        created_at=scope.created_at,
        expires_at=scope.expires_at,
    )


@router.get("/evidence/{evidence_id}", response_model=ExternalStoredEvidenceView)
def get_external_evidence(
    evidence_id: str,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalStoredEvidenceView:
    enforce_external_rate_limit(principal)
    record = auth.get_external_evidence(evidence_id, principal.key_id)
    if record is None:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "证据不存在或已过期")
    if "read" in principal.scopes:
        required_scope = "read"
    elif "invoke" in principal.scopes:
        required_scope = "invoke"
    else:
        required_scope = "write"
    authz = authorize_kb(
        principal,
        record.kb_ref,
        auth=auth,
        required_scope=required_scope,
        required_kb_permission="read",
        request=request,
    )
    current = _get_scoped_record(
        _store_for(pipeline), record.record_id, authz.kb.department_id
    )
    current_ok = bool(
        current is not None
        and _record_in_kb(current, authz)
        and _record_is_ready(current)
        and _normalized_hash(current.content_hash)
        == _normalized_hash(record.source_content_hash)
    )
    audit_external(
        auth,
        action="external_get_evidence",
        principal=principal,
        request=request,
        success=True,
        kb_ref=record.kb_ref,
        metadata={
            "evidence_id": record.evidence_id,
            "status": "current" if current_ok else "stale",
        },
    )
    return _stored_evidence_view(record, status="current" if current_ok else "stale")


@router.post("/evidence/validate", response_model=ExternalEvidenceValidateResponse)
def validate_external_evidence(
    body: ExternalEvidenceValidateRequest,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalEvidenceValidateResponse:
    enforce_external_rate_limit(principal)
    max_items = int(external_settings.HDB_EXTERNAL_MAX_BATCH_ITEMS or 20)
    if len(body.items) > max_items:
        raise ExternalApiError(422, "INVALID_REQUEST", "验证条目数量超过限制")
    store = _store_for(pipeline)
    results: list[ExternalEvidenceValidateResult] = []
    for item in body.items:
        record = auth.get_external_evidence(item.evidence_id, principal.key_id)
        if record is None:
            results.append(
                ExternalEvidenceValidateResult(
                    evidence_id=item.evidence_id,
                    status="not_found",
                    supported=False,
                    source_current=False,
                )
            )
            continue
        results.append(
            _validate_evidence_record(
                record,
                auth=auth,
                principal=principal,
                store=store,
                request=request,
                required_terms=item.required_terms,
                required_fields=item.required_fields,
                expected_source_hash=item.expected_source_hash,
                expected_content_hash=item.expected_content_hash,
            )
        )
    audit_external(
        auth,
        action="external_validate_evidence",
        principal=principal,
        request=request,
        success=True,
        metadata={"item_count": len(results)},
    )
    return ExternalEvidenceValidateResponse(
        request_id=request_id(request),
        items=results,
    )


@router.get("/kbs/{kb_ref}/assets", response_model=ExternalAssetPage)
def list_external_assets(
    kb_ref: str,
    request: Request,
    query: str = "",
    status: str = Query(default="", pattern="^(draft|effective|in_revision|obsolete)?$"),
    category: str = "",
    project: str = "",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    asset_service: DocumentAssetService = Depends(get_asset_service),
) -> ExternalAssetPage:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="read", request=request)
    try:
        assets = asset_service.list_assets(
            kb_id=authz.kb.kb_id,
            department_id=int(authz.kb.department_id),
            query=query,
            status=status,
            category=category,
            project=project,
        )
    except Exception as exc:  # noqa: BLE001 - safe external error contract
        raise ExternalApiError(
            503, "SOURCE_SCOPE_UNRESOLVED", "资产台账暂不可用", retryable=True
        ) from exc
    page_assets = assets[offset : offset + limit]
    page = ExternalAssetPage(
        items=[_asset_view(auth, authz, asset) for asset in page_assets],
        offset=offset,
        limit=limit,
        total=len(assets),
    )
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0)
    if max_bytes and len(
        json.dumps(page.model_dump(), ensure_ascii=False).encode("utf-8")
    ) > max_bytes:
        raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "资产响应超过大小限制")
    audit_external(
        auth,
        action="external_list_assets",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"count": len(page.items)},
    )
    return page


@router.get("/kbs/{kb_ref}/assets/{asset_ref}", response_model=ExternalAssetDetailView)
def get_external_asset(
    kb_ref: str,
    asset_ref: str,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    asset_service: DocumentAssetService = Depends(get_asset_service),
) -> ExternalAssetDetailView:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="read", request=request)
    mapping = auth.get_external_asset_ref(asset_ref)
    if mapping is None or mapping.kb_id != authz.kb.kb_id:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    try:
        asset = asset_service.get_asset(
            asset_id=mapping.asset_id,
            kb_id=authz.kb.kb_id,
            department_id=int(authz.kb.department_id),
        )
    except Exception as exc:  # noqa: BLE001 - safe external error contract
        raise ExternalApiError(
            503, "SOURCE_SCOPE_UNRESOLVED", "资产台账暂不可用", retryable=True
        ) from exc
    if asset is None:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    view = _asset_detail_view(auth, authz, asset)
    audit_external(
        auth,
        action="external_get_asset",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"asset_ref": asset_ref},
    )
    return view


@router.post(
    "/kbs/{kb_ref}/assets/{asset_ref}/versions",
    response_model=ExternalAssetDetailView,
)
def add_external_asset_version(
    kb_ref: str,
    asset_ref: str,
    body: ExternalAddAssetVersionRequest,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
    asset_service: DocumentAssetService = Depends(get_asset_service),
) -> ExternalAssetDetailView:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(
        principal,
        kb_ref,
        auth=auth,
        required_scope="write",
        required_kb_permission="write",
        request=request,
    )
    mapping = auth.get_external_asset_ref(asset_ref)
    if mapping is None or mapping.kb_id != authz.kb.kb_id:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    file_mapping = auth.get_external_file_ref(body.file_ref)
    if file_mapping is None or file_mapping.kb_id != authz.kb.kb_id:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    store = _store_for(pipeline)
    record = _get_scoped_record(store, file_mapping.record_id, authz.kb.department_id)
    if record is None or not _record_in_kb(record, authz):
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    if not _record_is_ready(record):
        raise _not_ready_error(record)
    service = asset_service
    try:
        current = service.get_asset(
            asset_id=mapping.asset_id,
            kb_id=authz.kb.kb_id,
            department_id=int(authz.kb.department_id),
        )
        if current is None:
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
        service.add_version(
            asset_id=mapping.asset_id,
            kb_id=authz.kb.kb_id,
            department_id=int(authz.kb.department_id),
            actor_user_id=int(authz.user.id),
            file_id=str(int(record.id)),
            file_name=str(record.original_file_name or record.document_name or ""),
            content_hash=str(record.content_hash or ""),
            parse_status=normalize_parse_status(record.status, record.processor_kind),
            note=body.note,
        )
    except ExternalApiError:
        raise
    except DocumentAssetError as exc:
        raise ExternalApiError(422, "INVALID_REQUEST", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - safe external error contract
        raise ExternalApiError(
            503, "SOURCE_SCOPE_UNRESOLVED", "资产台账暂不可用", retryable=True
        ) from exc
    try:
        service.absorb_shadow_for_file(
            kb_id=authz.kb.kb_id,
            department_id=int(authz.kb.department_id),
            file_id=str(int(record.id)),
            exclude_asset_id=mapping.asset_id,
        )
    except Exception:  # noqa: BLE001 - absorb is best effort after version commit
        pass
    asset = service.get_asset(
        asset_id=mapping.asset_id,
        kb_id=authz.kb.kb_id,
        department_id=int(authz.kb.department_id),
    )
    if asset is None:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    view = _asset_detail_view(auth, authz, asset)
    audit_external(
        auth,
        action="external_add_asset_version",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"asset_ref": asset_ref, "file_ref": body.file_ref},
    )
    return view


@router.get("/kbs/{kb_ref}/tables/schema", response_model=ExternalTableSchemaResponse)
def list_external_table_schema(
    kb_ref: str,
    request: Request,
    file_ref: str = "",
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalTableSchemaResponse:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="read", request=request)
    entries = _table_registry_entries(auth, authz, pipeline)
    if file_ref:
        mapping = auth.get_external_file_ref(file_ref)
        if mapping is None or mapping.kb_id != authz.kb.kb_id:
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
        entries = [
            entry for entry in entries if int(entry["record"].id) == mapping.record_id
        ]
    response = ExternalTableSchemaResponse(
        tables=[
            ExternalTableSchemaView(
                table_ref=entry["table_ref"],
                file_ref=entry["file_ref"],
                file_name=entry["file_name"],
                sheet_name=str(entry.get("sheet_name") or ""),
                row_count=int(entry.get("row_count") or 0),
                columns=entry["external_columns"],
                note=str(entry.get("note") or ""),
            )
            for entry in entries
        ]
    )
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0)
    if max_bytes and len(
        json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8")
    ) > max_bytes:
        raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "Schema 响应超过大小限制")
    audit_external(
        auth,
        action="external_table_schema",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"table_count": len(response.tables)},
    )
    return response


@router.post("/kbs/{kb_ref}/tables/query", response_model=ExternalTableQueryResponse)
def query_external_tables(
    kb_ref: str,
    body: ExternalTableQueryRequest,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalTableQueryResponse:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(
        principal,
        kb_ref,
        auth=auth,
        required_scope="invoke",
        required_kb_permission="read",
        request=request,
    )
    entries = _table_registry_entries(auth, authz, pipeline)
    if not entries:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "没有可查询的结构化表")
    alias_to_physical = {
        str(entry["table_ref"]): str(entry["physical_table"]) for entry in entries
    }
    ast, error = _validate_readonly_sql(body.sql, set(alias_to_physical))
    if error:
        raise ExternalApiError(422, "INVALID_REQUEST", f"SQL 校验失败: {error}")
    from sqlglot import exp

    for table in ast.find_all(exp.Table):
        physical = alias_to_physical.get(table.name)
        if physical:
            table.set("this", exp.to_identifier(physical))
    rewritten = ast.sql(dialect="sqlite")
    db_path = SpreadsheetIndexService().db_path(
        authz.kb.department_id, authz.kb.kb_name, create=False
    )
    rows, exec_error = _execute_readonly_sql(db_path, rewritten)
    if exec_error or rows is None:
        raise ExternalApiError(422, "INVALID_REQUEST", "SQL 执行失败")
    max_rows = min(int(body.max_rows), 200)
    truncated = len(rows) >= max_rows
    rows = rows[:max_rows]
    keys = list(rows[0].keys()) if rows else []
    columns = [
        _query_column_view(key, rows[0].get(key) if rows else None, entries)
        for key in keys
    ]
    response = ExternalTableQueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0)
    if max_bytes and len(
        json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8")
    ) > max_bytes:
        raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "查询响应超过大小限制")
    audit_external(
        auth,
        action="external_table_query",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"row_count": response.row_count, "truncated": truncated},
    )
    return response


@router.post("/kbs/{kb_ref}/circuits/query", response_model=ExternalCircuitQueryResponse)
def query_external_circuits(
    kb_ref: str,
    body: ExternalCircuitQueryRequest,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
) -> ExternalCircuitQueryResponse:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(
        principal,
        kb_ref,
        auth=auth,
        required_scope="invoke",
        required_kb_permission="read",
        request=request,
    )
    allowed_ids = _scoped_circuit_design_ids(authz)
    if not allowed_ids:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "没有可查询的电路设计")
    engine = CircuitQueryEngine()
    try:
        if body.kind == "instances":
            rows = engine.search_instances(
                authz.kb.kb_name,
                body.query,
                limit=body.limit,
                allowed_design_ids=allowed_ids,
            )
        elif body.kind == "nets":
            rows = engine.search_nets(
                authz.kb.kb_name,
                body.query,
                limit=body.limit,
                allowed_design_ids=allowed_ids,
            )
        elif body.kind == "cross_references":
            rows = [
                row
                for row in engine.search_cross_references(
                    authz.kb.kb_name, body.query, limit=body.limit
                )
                if str(row.get("design_id") or "") in allowed_ids
            ]
        else:
            rows = engine.search_modules(
                authz.kb.kb_name,
                body.query,
                limit=body.limit,
                allowed_design_ids=allowed_ids,
            )
    except Exception as exc:  # noqa: BLE001 - safe external error contract
        raise ExternalApiError(
            503, "SOURCE_SCOPE_UNRESOLVED", "电路索引暂不可用", retryable=True
        ) from exc
    sanitized = _sanitize_circuit_rows(body.kind, rows)
    response = ExternalCircuitQueryResponse(
        kind=body.kind,
        rows=sanitized,
        row_count=len(sanitized),
    )
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0)
    if max_bytes and len(
        json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8")
    ) > max_bytes:
        raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "电路响应超过大小限制")
    audit_external(
        auth,
        action="external_circuit_query",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"kind": body.kind, "row_count": response.row_count},
    )
    return response


@router.get("/kbs/{kb_ref}/files/{file_ref}/download")
def download_external_file(
    kb_ref: str,
    file_ref: str,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
):
    enforce_external_rate_limit(principal)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="read", request=request)
    mapping = auth.get_external_file_ref(file_ref)
    if mapping is None or mapping.kb_id != authz.kb.kb_id:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    store = _store_for(pipeline)
    record = _get_scoped_record(store, mapping.record_id, authz.kb.department_id)
    if record is None or not _record_in_kb(record, authz):
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")

    archive = _archive_for(pipeline)
    path = _safe_archive_path(archive, record)
    if path is None:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "原始文件不可用")
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_DOWNLOAD_BYTES or 0)
    try:
        size = os.path.getsize(path)
        if max_bytes > 0 and size > max_bytes:
            raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "文件超过下载大小限制")
        expected_hash = _normalized_hash(record.content_hash)
        if expected_hash and _file_sha256(path) != expected_hash:
            raise ExternalApiError(409, "SOURCE_HASH_CONFLICT", "原始文件内容哈希不匹配")
    except ExternalApiError:
        raise
    except OSError as exc:
        raise ExternalApiError(
            503,
            "SOURCE_SCOPE_UNRESOLVED",
            "原始文件暂不可用",
            retryable=True,
        ) from exc

    file_name = os.path.basename(
        str(record.original_file_name or record.document_name or "download")
    ) or "download"
    file_name = re.sub(r"[\r\n\"\\]", "_", file_name)[:255] or "download"
    media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    audit_external(
        auth,
        action="external_download_file",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"file_ref": file_ref, "file_size": size},
    )
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if expected_hash:
        headers["ETag"] = f'"{expected_hash}"'
    return FileResponse(
        path,
        media_type=media_type,
        filename=file_name,
        headers=headers,
    )


@router.get("/kbs/{kb_ref}/files/{file_ref}/chunks", response_model=ExternalChunkPage)
def list_external_chunks(
    kb_ref: str,
    file_ref: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEFAULT_CHUNK_PAGE_SIZE, ge=1, le=200),
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalChunkPage:
    enforce_external_rate_limit(principal)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="read", request=request)
    mapping = auth.get_external_file_ref(file_ref)
    if mapping is None or mapping.kb_id != authz.kb.kb_id:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    store = _store_for(pipeline)
    record = _get_scoped_record(store, mapping.record_id, authz.kb.department_id)
    if record is None or not _record_in_kb(record, authz):
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    if str(record.processor_kind or "") != PROCESSOR_KIND_RAGFLOW:
        raise ExternalApiError(422, "CAPABILITY_NOT_ENABLED", "该文件类型暂不支持切片读取")
    if not _record_is_ready(record):
        raise _not_ready_error(record)

    backend = getattr(pipeline, "backend", None)
    get_parse_result = getattr(backend, "get_parse_result", None)
    if not callable(get_parse_result):
        raise ExternalApiError(
            503,
            "RETRIEVAL_BACKEND_UNAVAILABLE",
            "解析切片服务暂不可用",
            retryable=True,
        )
    timeout_seconds = float(external_settings.HDB_EXTERNAL_TIMEOUT_SECONDS or 30)
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    try:
        result = get_parse_result(
            authz.kb.kb_name,
            str(record.document_id),
            ctx=authz.context,
            deadline=deadline,
        )
    except ScopedRetrievalError as exc:
        raise _scoped_error(
            exc.status,
            exc.error_code or "",
            "解析切片读取失败",
        ) from exc
    if result is None:
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "解析切片不存在")
    current = _get_scoped_record(store, int(record.id), authz.kb.department_id)
    if (
        current is None
        or not _record_in_kb(current, authz)
        or not _record_is_ready(current)
        or _normalized_hash(current.content_hash)
        != _normalized_hash(record.content_hash)
    ):
        raise ExternalApiError(409, "SOURCE_HASH_CONFLICT", "来源在切片读取期间发生变化")

    chunks = list(getattr(result, "chunks", []) or [])
    page_chunks = chunks[offset : offset + limit]
    items: list[ExternalChunkView] = []
    for chunk in page_chunks:
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        locator = (
            metadata.get("chunk_id")
            or metadata.get("chunk_or_structured_locator")
            or f"index:{chunk.index}"
        )
        page = metadata.get("page")
        if not isinstance(page, int) or page < 1:
            page = None
        items.append(
            ExternalChunkView(
                chunk_ref=_external_chunk_ref(int(record.id), locator),
                index=int(chunk.index),
                content=str(chunk.content or ""),
                page=page,
            )
        )
    response = ExternalChunkPage(
        file_ref=file_ref,
        file_name=str(record.original_file_name or record.document_name or ""),
        chunk_count=len(chunks),
        offset=offset,
        limit=limit,
        items=items,
    )
    max_bytes = int(external_settings.HDB_EXTERNAL_CHUNK_MAX_BYTES or 0)
    if max_bytes and len(
        json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8")
    ) > max_bytes:
        raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "切片响应超过大小限制")
    audit_external(
        auth,
        action="external_list_chunks",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"file_ref": file_ref, "count": len(items)},
    )
    return response


@router.post("/kbs/{kb_ref}/retrieve", response_model=ExternalRetrieveResponse)
def retrieve_external(
    kb_ref: str,
    body: ExternalRetrieveRequest,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalRetrieveResponse:
    enforce_external_rate_limit(principal)
    _check_top_k(body.top_k)
    authz = authorize_kb(principal, kb_ref, auth=auth, required_scope="invoke", request=request)
    source_scope_ref = str(body.source_scope_ref or "").strip() or None
    if source_scope_ref:
        source_scope = auth.get_external_source_scope(
            source_scope_ref, principal.key_id
        )
        if source_scope is None or source_scope.kb_ref != kb_ref:
            raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "来源范围不存在")
        if body.scope.mode != "files":
            raise ExternalApiError(422, "INVALID_REQUEST", "来源范围检索只支持 files 模式")
        scope_refs = {
            str(source.get("file_ref") or "")
            for source in source_scope.sources
            if isinstance(source, dict)
        }
        if set(body.scope.file_refs) != scope_refs:
            raise ExternalApiError(422, "INVALID_REQUEST", "file_refs 与来源范围不一致")
        records, file_ref_by_record = _resolve_source_scope(
            source_scope, authz, auth, _store_for(pipeline)
        )
        for file_ref, expected in body.scope.expected_content_hashes.items():
            record = next(
                (
                    item
                    for item in records
                    if file_ref_by_record.get(int(item.id)) == file_ref
                ),
                None,
            )
            if record is None or _normalized_hash(expected) != _normalized_hash(
                record.content_hash
            ):
                raise ExternalApiError(409, "SOURCE_HASH_CONFLICT", "来源范围内容哈希不匹配")
    else:
        records, file_ref_by_record = _resolve_records(body, authz, auth, pipeline)
    contract = _contract_for(authz, records)
    backend = getattr(pipeline, "backend", None)
    retrieve_with_scope = getattr(backend, "retrieve_with_scope", None)
    if not callable(retrieve_with_scope):
        raise ExternalApiError(503, "RETRIEVAL_BACKEND_UNAVAILABLE", "检索后端暂不可用", retryable=True)

    timeout_seconds = float(external_settings.HDB_EXTERNAL_TIMEOUT_SECONDS or 0)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None

    def _remaining_seconds() -> float:
        if deadline is None:
            return float("inf")
        return max(0.0, deadline - time.monotonic())

    with external_concurrency(principal):
        outcome = retrieve_with_scope(
            authz.kb.kb_name,
            body.query,
            contract=contract,
            top_k=body.top_k,
            ctx=authz.context,
            filters={
                "allowed_record_ids": list(contract.allowed_record_ids or ()),
                "link_stamps": {
                    str(record.id): {
                        "content_hash": str(record.content_hash or ""),
                        "source_version_id": str(record.source_version_id or ""),
                        "revision": str(record.revision or ""),
                    }
                    for record in records
                },
            },
            remaining_seconds=_remaining_seconds if deadline is not None else None,
        )
    if str(getattr(outcome, "status", "")) not in {"success_with_hits", "success_empty"}:
        raise _outcome_error(outcome)

    store = _store_for(pipeline)
    allowed_records = {int(record.id): record for record in records}
    views = [
        _evidence_view(
            evidence,
            request=request,
            allowed_records=allowed_records,
            file_ref_by_record=file_ref_by_record,
            store=store,
            authz=authz,
            auth=auth,
            source_scope_ref=source_scope_ref,
        )
        for evidence in list(getattr(outcome, "evidence", []) or [])
    ]
    response = ExternalRetrieveResponse(
        request_id=request_id(request),
        status="completed" if views else "completed_no_hits",
        results=views,
        warnings=[],
    )
    encoded_size = len(json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8"))
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0)
    if max_bytes and encoded_size > max_bytes:
        raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "响应超过大小限制")
    audit_external(
        auth,
        action="external_retrieve",
        principal=principal,
        request=request,
        success=True,
        kb_ref=kb_ref,
        metadata={"status": response.status, "hit_count": len(views)},
    )
    return response


@router.post(
    "/evidence/batch-retrieve",
    response_model=ExternalBatchRetrieveResponse,
)
def batch_retrieve_external(
    body: ExternalBatchRetrieveRequest,
    request: Request,
    principal: EmployeeApiKeyPrincipal = Depends(require_external_principal),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> ExternalBatchRetrieveResponse:
    enforce_external_rate_limit(principal)
    max_items = int(external_settings.HDB_EXTERNAL_MAX_BATCH_ITEMS or 20)
    if len(body.items) > max_items:
        raise ExternalApiError(422, "INVALID_REQUEST", "批量条目数量超过限制")
    items: list[ExternalBatchItemView] = []
    for item in body.items:
        try:
            result = retrieve_external(
                item.kb_ref,
                ExternalRetrieveRequest(
                    query=item.query,
                    top_k=item.top_k,
                    scope=item.scope,
                    source_scope_ref=item.source_scope_ref,
                    capabilities=item.capabilities,
                ),
                request,
                principal,
                auth,
                pipeline,
            )
            items.append(
                ExternalBatchItemView(
                    kb_ref=item.kb_ref,
                    status=result.status,
                    results=result.results,
                    warnings=result.warnings,
                )
            )
        except ExternalApiError as exc:
            items.append(
                ExternalBatchItemView(
                    kb_ref=item.kb_ref,
                    status="failed",
                    error=ExternalBatchErrorView(
                        code=exc.code,
                        message=exc.message,
                        retryable=exc.retryable,
                    ),
                )
            )
        except Exception:  # noqa: BLE001 - per-item failure must not abort batch
            items.append(
                ExternalBatchItemView(
                    kb_ref=item.kb_ref,
                    status="failed",
                    error=ExternalBatchErrorView(
                        code="RETRIEVAL_BACKEND_UNAVAILABLE",
                        message="检索暂不可用",
                        retryable=True,
                    ),
                )
            )
    failed = sum(1 for item in items if item.error is not None)
    if failed == 0:
        overall = "completed"
    elif failed == len(items):
        overall = "failed"
    else:
        overall = "partial"
    response = ExternalBatchRetrieveResponse(
        request_id=request_id(request),
        status=overall,
        items=items,
    )
    max_bytes = int(external_settings.HDB_EXTERNAL_MAX_RESPONSE_BYTES or 0)
    if max_bytes and len(
        json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8")
    ) > max_bytes:
        raise ExternalApiError(413, "RESPONSE_TOO_LARGE", "批量响应超过大小限制")
    audit_external(
        auth,
        action="external_batch_retrieve",
        principal=principal,
        request=request,
        success=overall != "failed",
        metadata={"status": overall, "item_count": len(items), "failed_count": failed},
    )
    return response
