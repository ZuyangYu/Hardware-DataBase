import hashlib
import json
import os
import shutil
import time
from typing import Callable, Generator

import requests

import config.settings
from src.core.auth import AuthService
from src.core.app_logs import AppLogService
from src.core.logger import error, log
from src.core.source_group_router import route_source_groups
from src.ingestion.kb_paths import get_kb_data_path, safe_child_path, validate_kb_name
from src.ingestion.source_groups import (
    DESIGN_GROUP,
    DOCS_GROUP,
    EXTERNAL_GROUP,
    MATERIAL_GROUP,
    PEOPLE_GROUP,
    PROJECT_GROUP,
    TEST_GROUP,
    UNKNOWN_GROUP,
    safe_source_group,
)
from src.ingestion.parse_tasks import ParseTask
from src.rag_backends.base import RAGBackend
from src.rag_backends.ragflow_store import RAGFlowStore
from src.rag_backends.schemas import (
    BackendHealth,
    BackendResult,
    DocumentInfo,
    Evidence,
    IngestResult,
    ParsedChunk,
    ParseResult,
    RequestContext,
)


DATASET_GOVERNANCE = "governance"
DATASET_DESIGN = "design"
DATASET_TABLE = "table"
CONTENT_KIND_DOCUMENT = "document_text"
CONTENT_KIND_SPREADSHEET = "spreadsheet_table"
PROCESSOR_KIND_RAGFLOW = "ragflow"
PROCESSOR_KIND_SPREADSHEET = "spreadsheet_table"

RAGFLOW_STATUS_UPLOADED = "uploaded"
RAGFLOW_STATUS_PARSING = "parsing"
RAGFLOW_STATUS_PARSED = "parsed"
RAGFLOW_STATUS_FAILED = "failed"
RAGFLOW_STATUS_DELETED = "deleted"
RAGFLOW_STATUS_CANCELLED = "cancelled"
RAGFLOW_STATUS_UNKNOWN = "unknown"
TABLE_STATUS_ARCHIVED = "archived"
RAGFLOW_HIDDEN_TASK_STATUSES = {
    RAGFLOW_STATUS_PARSED,
    RAGFLOW_STATUS_DELETED,
    RAGFLOW_STATUS_CANCELLED,
    TABLE_STATUS_ARCHIVED,
    "completed",
    "complete",
    "done",
    "finish",
    "finished",
    "success",
    "已完成",
}
RAGFLOW_PARSE_START_DELAY_SECONDS = 2.0
RAGFLOW_DOCUMENT_READY_TIMEOUT_SECONDS = 10.0
RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS = 1800.0
RAGFLOW_PARSE_PROGRESS_POLL_SECONDS = 2.0

SOURCE_GROUP_DATASET_KIND = {
    DOCS_GROUP: DATASET_DESIGN,
    PROJECT_GROUP: DATASET_GOVERNANCE,
    EXTERNAL_GROUP: DATASET_DESIGN,
    PEOPLE_GROUP: DATASET_GOVERNANCE,
    DESIGN_GROUP: DATASET_DESIGN,
    MATERIAL_GROUP: DATASET_DESIGN,
    TEST_GROUP: DATASET_DESIGN,
    UNKNOWN_GROUP: DATASET_DESIGN,
}

RAGFLOW_INFO_ID_PREFIX = "ragflow:"
SPREADSHEET_EXTENSIONS = {".xls", ".xlsx"}


class RAGFlowAPIError(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = payload
        self.code = payload.get("code")
        self.message = str(payload.get("message") or payload)
        super().__init__(f"RAGFlow API error: {payload}")


def _is_ragflow_not_owner_error(exc: Exception) -> bool:
    return isinstance(exc, RAGFlowAPIError) and exc.code == 102


def _ragflow_status_unavailable_message(document_name: str) -> str:
    return f"{document_name}: RAGFlow parse submitted; realtime progress is not readable by the current API key."


def _ctx_department_id(ctx: RequestContext | None) -> str:
    if not ctx:
        return ""
    value = ctx.metadata.get("department_id")
    return "" if value is None else str(value)


def _require_upload_department(ctx: RequestContext | None) -> str:
    department_id = _ctx_department_id(ctx)
    if not department_id:
        raise PermissionError("Uploading to RAGFlow requires an assigned department.")
    return department_id


def _metadata_condition(kb_name: str, ctx: RequestContext | None, source_groups: tuple[str, ...] = ()) -> dict:
    conditions = [{"name": "kb_name", "comparison_operator": "=", "value": kb_name}]
    department_id = _ctx_department_id(ctx)
    if department_id:
        conditions.append({"name": "department_id", "comparison_operator": "=", "value": department_id})
    if len(source_groups) == 1:
        conditions.append({"name": "source_group", "comparison_operator": "=", "value": source_groups[0]})
    elif len(source_groups) > 1:
        conditions.append({"name": "source_group", "comparison_operator": "in", "value": list(source_groups)})
    return {"logical_operator": "and", "conditions": conditions}


def _check_record_department(record, ctx: RequestContext | None):
    ctx_department = _ctx_department_id(ctx)
    if not ctx_department or str(record.department_id or "") != ctx_department:
        raise PermissionError("Document does not belong to the current department.")


def _file_sha256(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_spreadsheet_file(file_path: str) -> bool:
    return os.path.splitext(file_path.lower())[1] in SPREADSHEET_EXTENSIONS


def _normalize_ragflow_status(raw_status: object) -> str:
    status = str(raw_status or "").strip().lower()
    if not status:
        return RAGFLOW_STATUS_UNKNOWN
    if status in {"0", "queued", "pending", "uploading", "uploaded", "ready"}:
        return RAGFLOW_STATUS_UPLOADED
    if status in {"1", "running", "parsing", "processing", "started"}:
        return RAGFLOW_STATUS_PARSING
    if status in {"2", "done", "success", "parsed", "completed", "finish", "finished"}:
        return RAGFLOW_STATUS_PARSED
    if status in {"3", "fail", "failed", "error", "exception"}:
        return RAGFLOW_STATUS_FAILED
    if status in {"deleted", "removed"}:
        return RAGFLOW_STATUS_DELETED
    if status in {"cancel", "cancelled", "canceled", "stopped", "stop"}:
        return RAGFLOW_STATUS_CANCELLED
    return status


def _coerce_progress_percent(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

    if 0 <= number <= 1:
        number *= 100
    return max(0, min(100, int(round(number))))


def _extract_ragflow_progress(document: dict) -> int | None:
    return _coerce_progress_percent(document.get("progress"))


def _extract_ragflow_progress_message(document: dict) -> str:
    value = document.get("progress_msg")
    if isinstance(value, list):
        messages = [str(item).strip() for item in value if str(item).strip()]
        return messages[-1] if messages else ""
    if value:
        lines = [line.strip() for line in str(value).splitlines() if line.strip()]
        return lines[-1] if lines else str(value)
    return ""


def _extract_ragflow_error(document: dict) -> str:
    keys = (
        "error",
        "message",
        "msg",
        "run_message",
        "process_msg",
        "parser_msg",
        "chunk_error",
        "exception",
        "remark",
    )
    for key in keys:
        value = document.get(key)
        if value:
            return str(value)

    run_value = document.get("run")
    if _normalize_ragflow_status(run_value) == RAGFLOW_STATUS_FAILED:
        progress_message = _extract_ragflow_progress_message(document)
        if progress_message:
            return progress_message
        summary = {
            key: document.get(key)
            for key in ("id", "name", "run", "status", "type", "parser_id")
            if document.get(key) not in {None, ""}
        }
        return f"RAGFlow 未返回明确错误原因: {json.dumps(summary, ensure_ascii=False)}"
    return ""


def _is_small_talk(query: str) -> bool:
    text = query.strip().lower()
    return text in {
        "你好",
        "您好",
        "嗨",
        "hi",
        "hello",
        "hey",
        "在吗",
        "你是谁",
        "介绍一下你自己",
    }


def _document_info_id(record_id: int) -> str:
    return f"{RAGFLOW_INFO_ID_PREFIX}{record_id}"


def _record_id_from_document_id(document_id: str) -> int | None:
    value = str(document_id or "")
    if value.startswith(RAGFLOW_INFO_ID_PREFIX):
        raw_id = value[len(RAGFLOW_INFO_ID_PREFIX):]
        return int(raw_id) if raw_id.isdigit() else None
    return None


class RAGFlowClient:
    def __init__(self):
        self.base_url = config.settings.RAGFLOW_BASE_URL.rstrip("/")
        self.api_key = config.settings.RAGFLOW_API_KEY
        self.timeout = config.settings.RAGFLOW_TIMEOUT_SECONDS
        if not self.api_key:
            raise ValueError("RAGFLOW_API_KEY is not configured.")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def request(self, method: str, path: str, **kwargs) -> dict:
        response = requests.request(
            method,
            self._url(path),
            headers={**self.headers, **kwargs.pop("headers", {})},
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code") not in {None, 0}:
            raise RAGFlowAPIError(data)
        return data

    def list_datasets(self, name: str | None = None) -> list[dict]:
        params = {"name": name} if name else {}
        data = self.request("GET", "/api/v1/datasets", params=params)
        return data.get("data", [])

    def create_dataset(self, name: str) -> str:
        data = self.request("POST", "/api/v1/datasets", json={"name": name})
        dataset = data.get("data") or {}
        return dataset.get("id") or dataset.get("dataset_id")

    def ensure_dataset(self, name: str) -> str:
        for dataset in self.list_datasets(name=name):
            if dataset.get("name") == name:
                return dataset.get("id")
        dataset_id = self.create_dataset(name)
        if not dataset_id:
            raise RuntimeError(f"Failed to create RAGFlow dataset: {name}")
        return dataset_id

    def upload_document(self, dataset_id: str, file_path: str) -> str:
        with open(file_path, "rb") as file_obj:
            files = {"file": (os.path.basename(file_path), file_obj)}
            data = self.request("POST", f"/api/v1/datasets/{dataset_id}/documents", files=files)
        documents = data.get("data") or []
        if isinstance(documents, dict):
            documents = [documents]
        if not documents:
            raise RuntimeError(f"RAGFlow did not return a document id for {file_path}")
        return documents[0].get("id") or documents[0].get("document_id")

    def update_document_metadata(self, dataset_id: str, document_id: str, metadata: dict):
        payload = {"meta_fields": metadata}
        self.request("PUT", f"/api/v1/datasets/{dataset_id}/documents/{document_id}", json=payload)

    def parse_documents(self, dataset_id: str, document_ids: list[str]):
        self.request("POST", f"/api/v1/datasets/{dataset_id}/chunks", json={"document_ids": document_ids})

    def stop_parse_documents(self, dataset_id: str, document_ids: list[str]):
        self.request("DELETE", f"/api/v1/datasets/{dataset_id}/chunks", json={"document_ids": document_ids})

    def wait_document_ready(self, dataset_id: str, document_id: str, timeout_seconds: float = RAGFLOW_DOCUMENT_READY_TIMEOUT_SECONDS) -> dict:
        deadline = time.monotonic() + timeout_seconds
        last_document = {}
        while time.monotonic() < deadline:
            documents = self.list_documents(dataset_id, document_id)
            if documents:
                last_document = documents[0]
                name = str(last_document.get("name") or last_document.get("document_name") or "").strip()
                if name:
                    return last_document
            time.sleep(0.5)
        return last_document

    def delete_documents(self, dataset_id: str, document_ids: list[str]):
        self.request("DELETE", f"/api/v1/datasets/{dataset_id}/documents", json={"ids": document_ids})

    def list_documents(self, dataset_id: str, document_id: str | None = None) -> list[dict]:
        params = {"id": document_id} if document_id else {}
        data = self.request("GET", f"/api/v1/datasets/{dataset_id}/documents", params=params)
        documents = data.get("data", {}).get("docs", data.get("data", []))
        return documents if isinstance(documents, list) else []

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        data = self.request("GET", f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks")
        chunks = data.get("data", {}).get("chunks", data.get("data", []))
        return chunks if isinstance(chunks, list) else []

    def retrieve(self, question: str, dataset_ids: list[str], top_k: int, metadata_condition: dict | None = None) -> list[dict]:
        payload = {
            "question": question,
            "dataset_ids": dataset_ids,
            "page": 1,
            "page_size": top_k,
            "similarity_threshold": config.settings.RAGFLOW_SIMILARITY_THRESHOLD,
            "vector_similarity_weight": config.settings.RAGFLOW_VECTOR_WEIGHT,
            "top_k": top_k,
        }
        if metadata_condition:
            payload["metadata_condition"] = metadata_condition
        data = self.request("POST", "/api/v1/retrieval", json=payload)
        chunks = data.get("data", {}).get("chunks", [])
        return chunks if isinstance(chunks, list) else []

    def health(self) -> dict:
        datasets = self.list_datasets()
        return {
            "datasets": len(datasets),
            "base_url": self.base_url,
            "api_key_configured": bool(self.api_key),
        }


class RAGFlowBackend(RAGBackend):
    """RAGFlow adapter with two physical datasets and local logical-KB permission control."""

    name = "ragflow"

    def __init__(self):
        self.client = RAGFlowClient()
        self.store = RAGFlowStore()
        self._dataset_ids: dict[str, str] = {}
        self._ensure_physical_datasets()

    def _ensure_physical_datasets(self):
        dataset_specs = {
            DATASET_GOVERNANCE: config.settings.RAGFLOW_GOVERNANCE_DATASET_NAME,
            DATASET_DESIGN: config.settings.RAGFLOW_DESIGN_DATASET_NAME,
        }
        for kind, name in dataset_specs.items():
            dataset_id = self.store.get_dataset_id(kind)
            if not dataset_id:
                dataset_id = self.client.ensure_dataset(name)
                self.store.save_dataset(kind, dataset_id, name)
            self._dataset_ids[kind] = dataset_id

    def _audit(
        self,
        action: str,
        ctx: RequestContext | None,
        kb_name: str = "",
        target_type: str = "",
        target_id: str = "",
        success: bool = True,
        error_message: str = "",
        metadata: dict | None = None,
    ):
        try:
            actor = AuthService().get_user_by_username(ctx.user_id) if ctx and ctx.user_id else None
            AppLogService().record_audit(
                action=action,
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                kb_name=kb_name,
                success=success,
                error_message=error_message,
                metadata=metadata,
            )
        except Exception as audit_error:
            log(f"RAGFlow audit failed: {audit_error}")

    def _check_kb_access(self, kb_name: str, ctx: RequestContext | None, required: str = "read"):
        validate_kb_name(kb_name)
        if ctx is not None and not ctx.has_kb_permission(kb_name, required):
            self._audit(
                "ragflow_permission_denied",
                ctx,
                kb_name=kb_name,
                target_type="knowledge_base",
                target_id=kb_name,
                success=False,
                error_message=f"lacks {required} permission",
                metadata={"required": required},
            )
            raise PermissionError(f"User {ctx.user_id} lacks {required} permission for knowledge base {kb_name}")

    def _dataset_kind_for_group(self, source_group: str | None) -> str:
        group = safe_source_group(source_group)
        return SOURCE_GROUP_DATASET_KIND.get(group, DATASET_DESIGN)

    def _metadata(self, kb_name: str, filename: str, source_group: str | None, ctx: RequestContext | None) -> dict:
        return {
            "kb_name": kb_name,
            "logical_kb_id": kb_name,
            "department_id": _ctx_department_id(ctx),
            "source_group": safe_source_group(source_group),
            "uploaded_by": ctx.user_id if ctx else "",
            "original_file_name": filename,
        }

    def _archive_root(self, create: bool = False) -> str:
        root = os.path.abspath(config.settings.RAGFLOW_FILE_ROOT)
        if create:
            os.makedirs(root, exist_ok=True)
        return root

    def _archive_kb_path(self, kb_name: str, create: bool = False) -> str:
        return safe_child_path(self._archive_root(create=True), validate_kb_name(kb_name), create=create)

    def _resolve_archive_path(self, record) -> str:
        path = record.local_path or os.path.join(record.source_group, record.document_name)
        if os.path.isabs(path):
            return path
        archive_path = os.path.join(self._archive_kb_path(record.kb_name), path)
        if os.path.exists(archive_path):
            return archive_path
        return os.path.join(get_kb_data_path(record.kb_name), path)

    def _archive_source_file(self, kb_name: str, file_path: str, source_group: str | None) -> tuple[str, str, str]:
        source_group = safe_source_group(source_group)
        filename = os.path.basename(file_path)
        kb_path = self._archive_kb_path(kb_name, create=True)
        target_dir = os.path.join(kb_path, source_group)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, filename)

        if os.path.exists(target_path):
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{int(time.time())}{ext}"
            target_path = os.path.join(target_dir, filename)

        shutil.copy2(file_path, target_path)
        return target_path, filename, source_group

    def _remove_local_archive(self, record):
        path = self._resolve_archive_path(record)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            error(f"Failed to remove local RAGFlow archive {path}: {exc}")

    def _cleanup_remote_document(self, dataset_id: str, document_id: str):
        if not dataset_id or not document_id:
            return
        try:
            self.client.delete_documents(dataset_id, [document_id])
        except Exception as cleanup_error:
            log(f"RAGFlow remote cleanup failed for {document_id}: {cleanup_error}")

    def _local_archive_exists(self, record) -> bool:
        return os.path.exists(self._resolve_archive_path(record))

    def _remote_document_exists(self, record) -> bool:
        try:
            return bool(self.client.list_documents(record.dataset_id, record.document_id))
        except Exception as exc:
            log(f"RAGFlow duplicate check could not verify remote document {record.document_id}: {exc}")
            return True

    def _get_record_for_document_id(self, kb_name: str, document_id: str):
        record_id = _record_id_from_document_id(document_id)
        if record_id is not None:
            record = self.store.get_document_by_id(record_id)
            if record and record.kb_name == kb_name:
                return record
            return None
        return self.store.get_document(kb_name, document_id)

    def ingest(
        self,
        kb_name: str,
        files: list[str],
        ctx: RequestContext | None = None,
        source_group: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> IngestResult:
        self._check_kb_access(kb_name, ctx, "write")
        if not files:
            return IngestResult(success_count=0, total_count=0, messages=["No files selected"], backend=self.name)

        messages = []
        success_count = 0
        dataset_kind = self._dataset_kind_for_group(source_group)
        dataset_id = self._dataset_ids[dataset_kind]
        department_id = _require_upload_department(ctx)
        uploaded_by = ctx.user_id if ctx else ""

        for file_path in files:
            filename = os.path.basename(file_path)
            archived_path = ""
            document_id = ""
            uploaded_to_ragflow = False
            try:
                content_hash = _file_sha256(file_path)
                is_spreadsheet = _is_spreadsheet_file(file_path)
                record_dataset_kind = DATASET_TABLE if is_spreadsheet else dataset_kind
                existing = self.store.find_by_hash(kb_name, record_dataset_kind, content_hash, department_id)
                if existing and existing.status not in {RAGFLOW_STATUS_FAILED, RAGFLOW_STATUS_DELETED}:
                    if self._local_archive_exists(existing) and (
                        existing.processor_kind != PROCESSOR_KIND_RAGFLOW or self._remote_document_exists(existing)
                    ):
                        messages.append(
                            f"Already archived for spreadsheet pipeline: {existing.document_name}"
                            if is_spreadsheet
                            else f"Already submitted to RAGFlow: {existing.document_name}"
                        )
                        success_count += 1
                        continue
                    log(f"Mapping for {existing.document_name} is stale; re-processing.")
                    if existing.processor_kind == PROCESSOR_KIND_RAGFLOW:
                        self._cleanup_remote_document(existing.dataset_id, existing.document_id)
                    self._remove_local_archive(existing)
                    self.store.delete_document_by_id(existing.id)

                archived_path, filename, archived_group = self._archive_source_file(kb_name, file_path, source_group)
                relative_local_path = os.path.relpath(archived_path, self._archive_kb_path(kb_name))
                file_size = os.path.getsize(archived_path)
                if is_spreadsheet:
                    document_id = f"table:{content_hash[:16]}"
                    self.store.upsert_document(
                        kb_name=kb_name,
                        document_name=filename,
                        dataset_kind=DATASET_TABLE,
                        dataset_id="",
                        document_id=document_id,
                        source_group=archived_group,
                        department_id=department_id,
                        uploaded_by=uploaded_by,
                        status=TABLE_STATUS_ARCHIVED,
                        original_file_name=os.path.basename(file_path),
                        local_path=relative_local_path,
                        file_size=file_size,
                        content_hash=content_hash,
                        upload_status=TABLE_STATUS_ARCHIVED,
                        content_kind=CONTENT_KIND_SPREADSHEET,
                        processor_kind=PROCESSOR_KIND_SPREADSHEET,
                    )
                    record = self.store.get_document(kb_name, filename, DATASET_TABLE)
                    self._audit(
                        "spreadsheet_upload_archived",
                        ctx,
                        kb_name=kb_name,
                        target_type="document",
                        target_id=filename,
                        metadata={
                            "store_id": record.id if record else None,
                            "dataset_kind": DATASET_TABLE,
                            "content_kind": CONTENT_KIND_SPREADSHEET,
                            "processor_kind": PROCESSOR_KIND_SPREADSHEET,
                            "source_group": archived_group,
                            "local_path": relative_local_path,
                            "content_hash": content_hash,
                        },
                    )
                    if progress_callback:
                        progress_callback(100, f"{filename}: 已归档到 Excel 独立管道，未上传 RAGFlow")
                    messages.append(f"Archived for spreadsheet pipeline: {filename}")
                    success_count += 1
                    continue

                document_id = self.client.upload_document(dataset_id, archived_path)
                uploaded_to_ragflow = True
                metadata = self._metadata(kb_name, filename, archived_group, ctx)
                try:
                    self.client.update_document_metadata(dataset_id, document_id, metadata)
                except Exception as metadata_error:
                    self._cleanup_remote_document(dataset_id, document_id)
                    raise RuntimeError(f"RAGFlow metadata update failed for {filename}: {metadata_error}") from metadata_error
                self.client.wait_document_ready(dataset_id, document_id)
                time.sleep(RAGFLOW_PARSE_START_DELAY_SECONDS)
                self.client.parse_documents(dataset_id, [document_id])
                if progress_callback:
                    progress_callback(5, f"{filename}: 已提交到 RAGFlow，等待解析进度")
                self.store.upsert_document(
                    kb_name=kb_name,
                    document_name=filename,
                    dataset_kind=dataset_kind,
                    dataset_id=dataset_id,
                    document_id=document_id,
                    source_group=archived_group,
                    department_id=department_id,
                    uploaded_by=uploaded_by,
                    status=RAGFLOW_STATUS_PARSING,
                    original_file_name=os.path.basename(file_path),
                    local_path=relative_local_path,
                    file_size=file_size,
                    content_hash=content_hash,
                    upload_status=RAGFLOW_STATUS_PARSING,
                    content_kind=CONTENT_KIND_DOCUMENT,
                    processor_kind=PROCESSOR_KIND_RAGFLOW,
                )
                status, message = self._wait_parse_progress(
                    dataset_id,
                    document_id,
                    filename,
                    progress_callback=progress_callback,
                )
                self.store.update_document_status(dataset_id, document_id, status, message)
                if status == RAGFLOW_STATUS_FAILED:
                    raise RuntimeError(message or f"RAGFlow parsing failed for {filename}")
                record = self.store.get_document(kb_name, filename, dataset_kind)
                self._audit(
                    "ragflow_upload_submitted",
                    ctx,
                    kb_name=kb_name,
                    target_type="document",
                    target_id=filename,
                    metadata={
                        "store_id": record.id if record else None,
                        "dataset_kind": dataset_kind,
                        "dataset_id": dataset_id,
                        "ragflow_document_id": document_id,
                        "source_group": archived_group,
                        "local_path": relative_local_path,
                        "content_hash": content_hash,
                    },
                )
                messages.append(f"Submitted to RAGFlow: {filename}")
                success_count += 1
            except Exception as exc:
                if uploaded_to_ragflow and document_id:
                    self._cleanup_remote_document(dataset_id, document_id)
                    self.store.delete_document_by_remote_id(dataset_id, document_id)
                if archived_path and os.path.exists(archived_path):
                    try:
                        os.remove(archived_path)
                    except OSError as cleanup_error:
                        error(f"Failed to remove archived RAGFlow source file {archived_path}: {cleanup_error}")
                error(f"RAGFlow ingest failed for {filename}: {exc}")
                self._audit(
                    "ragflow_upload_failed",
                    ctx,
                    kb_name=kb_name,
                    target_type="document",
                    target_id=filename,
                    success=False,
                    error_message=str(exc),
                    metadata={
                        "dataset_kind": dataset_kind,
                        "dataset_id": dataset_id,
                        "ragflow_document_id": document_id,
                        "source_group": source_group,
                        "local_path": archived_path,
                        "content_kind": CONTENT_KIND_SPREADSHEET if _is_spreadsheet_file(file_path) else CONTENT_KIND_DOCUMENT,
                    },
                )
                messages.append(f"Failed {filename}: {exc}")

        return IngestResult(success_count=success_count, total_count=len(files), messages=messages, backend=self.name)

    def _wait_parse_progress(
        self,
        dataset_id: str,
        document_id: str,
        filename: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> tuple[str, str]:
        deadline = time.monotonic() + RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS
        last_status = RAGFLOW_STATUS_PARSING
        last_message = ""
        last_progress = None

        while time.monotonic() < deadline:
            try:
                remote_docs = self.client.list_documents(dataset_id, document_id)
            except Exception as progress_error:
                last_message = _ragflow_status_unavailable_message(filename)
                log(f"RAGFlow progress polling failed for {document_id}: {progress_error}")
                if progress_callback:
                    progress_callback(last_progress if last_progress is not None else 5, last_message)
                return RAGFLOW_STATUS_PARSING, last_message
            if not remote_docs:
                last_message = f"{filename}: RAGFlow 暂未返回文档状态"
            else:
                remote_doc = remote_docs[0]
                last_status = _normalize_ragflow_status(remote_doc.get("run"))
                last_message = _extract_ragflow_error(remote_doc) or _extract_ragflow_progress_message(remote_doc)
                remote_progress = _extract_ragflow_progress(remote_doc)
                if remote_progress is not None:
                    last_progress = remote_progress

                if progress_callback and last_progress is not None:
                    stage = last_message or f"{filename}: RAGFlow 解析中"
                    progress_callback(last_progress, stage)

                if last_status in {RAGFLOW_STATUS_PARSED, RAGFLOW_STATUS_FAILED, RAGFLOW_STATUS_DELETED}:
                    return last_status, last_message

            time.sleep(RAGFLOW_PARSE_PROGRESS_POLL_SECONDS)

        return last_status, last_message or f"{filename}: RAGFlow parsing progress timed out"

    def retrieve(self, kb_name: str, query: str, top_k: int | None = None, ctx: RequestContext | None = None) -> list[Evidence]:
        self._check_kb_access(kb_name, ctx, "read")
        top_k = top_k or config.settings.FINAL_TOP_K
        route = route_source_groups(query)
        routed_source_groups = route.source_groups if route.should_filter else ()
        if routed_source_groups:
            log(f"RAGFlow source-group route: {route.reason}, filter={routed_source_groups}")
        else:
            log(f"RAGFlow source-group route: {route.reason}, no hard filter")
        dataset_ids = list(self._dataset_ids.values())
        chunks = self.client.retrieve(
            query,
            dataset_ids=dataset_ids,
            top_k=top_k,
            metadata_condition=_metadata_condition(kb_name, ctx, routed_source_groups),
        )

        evidences = []
        for chunk in chunks:
            metadata = chunk.get("metadata") or chunk.get("meta_fields") or {}
            chunk_kb = metadata.get("kb_name") or metadata.get("logical_kb_id")
            if chunk_kb and chunk_kb != kb_name:
                continue
            if ctx:
                chunk_department = str(metadata.get("department_id") or "")
                ctx_department = _ctx_department_id(ctx)
                if chunk_department != ctx_department:
                    continue
            chunk_source_group = safe_source_group(metadata.get("source_group"))
            if routed_source_groups and chunk_source_group not in routed_source_groups:
                continue
            content = chunk.get("content") or chunk.get("text") or ""
            score = chunk.get("similarity") or chunk.get("score") or chunk.get("vector_similarity") or 0.0
            evidences.append(
                Evidence(
                    id=str(chunk.get("id") or chunk.get("chunk_id") or ""),
                    content=content,
                    source_name=chunk.get("document_name") or metadata.get("original_file_name", ""),
                    score=float(score or 0.0),
                    metadata={
                        **metadata,
                        "ragflow_chunk": chunk,
                        "query_route_reason": route.reason,
                        "query_route_confidence": route.confidence,
                        "query_route_source_groups": list(routed_source_groups),
                    },
                    backend=self.name,
                    retriever="ragflow_retrieval",
                )
            )
        return evidences[:top_k]

    def stream_answer(
        self,
        kb_name: str,
        query: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
    ) -> Generator[str, None, None]:
        evidences = []
        context = ""
        try:
            from llama_index.core import Settings
            from llama_index.core.base.llms.types import ChatMessage, MessageRole
            from src.core.model_factory import init_generation_model

            init_generation_model()

            if not _is_small_talk(query):
                evidences = self.retrieve(kb_name, query, config.settings.FINAL_TOP_K, ctx=ctx)
                context = "\n\n".join(
                    f"[Source: {e.source_name} | Score: {e.score:.4f}]\n{e.content}"
                    for e in evidences
                )

            if context:
                system_content = f"{config.settings.SYSTEM_PROMPT}\n\nReference context:\n{context}"
            else:
                system_content = (
                    f"{config.settings.SYSTEM_PROMPT}\n\n"
                    f"### 参考资料 ###\n{config.settings.NO_CONTEXT_PROMPT}"
                )

            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=system_content)
            ]
            for user_msg, bot_msg in history[-5:]:
                messages.append(ChatMessage(role=MessageRole.USER, content=user_msg))
                messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=bot_msg))
            messages.append(ChatMessage(role=MessageRole.USER, content=query))

            for chunk in Settings.llm.stream_chat(messages):
                yield chunk.delta or ""
            if evidences:
                sources = "\n".join(f"- {e.source_name} ({e.score:.4f})" for e in evidences)
                yield f"\n\n---\n\nReferences:\n{sources}"
        except Exception as exc:
            error(f"RAGFlow answer generation failed: {exc}")
            if context:
                yield f"RAGFlow retrieved relevant context, but answer generation failed: {exc}\n\n{context}"
            else:
                yield f"系统错误: {exc}"

    def delete_document(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> BackendResult:
        self._check_kb_access(kb_name, ctx, "write")
        record = self._get_record_for_document_id(kb_name, document_id)
        if not record:
            return BackendResult(ok=False, message="Document mapping was not found.", backend=self.name)
        try:
            _check_record_department(record, ctx)
        except PermissionError as exc:
            self._audit(
                "ragflow_permission_denied",
                ctx,
                kb_name=kb_name,
                target_type="document",
                target_id=record.document_name,
                success=False,
                error_message=str(exc),
            )
            return BackendResult(ok=False, message=str(exc), backend=self.name)
        try:
            if record.processor_kind == PROCESSOR_KIND_RAGFLOW:
                self.client.delete_documents(record.dataset_id, [record.document_id])
            self._remove_local_archive(record)
            self.store.delete_document_by_id(record.id)
            self._audit(
                "ragflow_delete_document" if record.processor_kind == PROCESSOR_KIND_RAGFLOW else "spreadsheet_delete_document",
                ctx,
                kb_name=kb_name,
                target_type="document",
                target_id=record.document_name,
                metadata={
                    "store_id": record.id,
                    "ragflow_document_id": record.document_id,
                    "dataset_id": record.dataset_id,
                    "dataset_kind": record.dataset_kind,
                    "content_kind": record.content_kind,
                    "processor_kind": record.processor_kind,
                    "local_path": record.local_path,
                },
            )
            return BackendResult(ok=True, message=f"✅ Deleted RAGFlow document: {record.document_name}", backend=self.name)
        except Exception as exc:
            if _is_ragflow_not_owner_error(exc):
                self._remove_local_archive(record)
                self.store.delete_document_by_id(record.id)
                self._audit(
                    "ragflow_delete_document_local_cleanup",
                    ctx,
                    kb_name=kb_name,
                    target_type="document",
                    target_id=record.document_name,
                    metadata={
                        "store_id": record.id,
                        "ragflow_document_id": record.document_id,
                        "dataset_id": record.dataset_id,
                        "dataset_kind": record.dataset_kind,
                        "reason": str(exc),
                    },
                )
                return BackendResult(
                    ok=True,
                    message=f"✅ RAGFlow 无权访问远端文档，已清理本地记录: {record.document_name}",
                    backend=self.name,
                )
            self._audit(
                "ragflow_delete_document",
                ctx,
                kb_name=kb_name,
                target_type="document",
                target_id=record.document_name,
                success=False,
                error_message=str(exc),
                metadata={
                    "store_id": record.id,
                    "ragflow_document_id": record.document_id,
                    "dataset_id": record.dataset_id,
                    "dataset_kind": record.dataset_kind,
                },
            )
            return BackendResult(ok=False, message=f"Delete failed: {exc}", backend=self.name)

    def delete_knowledge_base(self, kb_name: str, ctx: RequestContext | None = None) -> BackendResult:
        self._check_kb_access(kb_name, ctx, "admin")
        records = self.store.list_documents(kb_name)
        errors = []
        for record in records:
            try:
                _check_record_department(record, ctx)
            except PermissionError as exc:
                errors.append(f"{record.document_name}: {exc}")
                continue
            try:
                self.client.delete_documents(record.dataset_id, [record.document_id])
            except Exception as exc:
                if not _is_ragflow_not_owner_error(exc):
                    errors.append(f"{record.document_name}: {exc}")
                    continue
                log(f"RAGFlow remote document is not readable while deleting kb {kb_name}: {record.document_id}, {exc}")
            self._remove_local_archive(record)

        if errors:
            return BackendResult(
                ok=False,
                message=f"RAGFlow 知识库删除失败，部分文档未清理: {'; '.join(errors)}",
                backend=self.name,
            )

        self.store.delete_documents_by_kb(kb_name)
        self._audit(
            "ragflow_delete_knowledge_base",
            ctx,
            kb_name=kb_name,
            target_type="knowledge_base",
            target_id=kb_name,
            metadata={"document_count": len(records)},
        )
        return BackendResult(ok=True, message=f"RAGFlow 知识库 '{kb_name}' 已删除", backend=self.name)

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None) -> list[ParseTask]:
        if kb_name:
            self._check_kb_access(kb_name, ctx, "read")
        department_id = _ctx_department_id(ctx)
        records = self.store.list_documents(kb_name, department_id=department_id) if kb_name else []
        tasks = []
        for record in records:
            if record.status in RAGFLOW_HIDDEN_TASK_STATUSES:
                continue
            status = record.status
            message = record.ragflow_error
            remote_progress = None
            remote_stage = ""
            try:
                remote_docs = self.client.list_documents(record.dataset_id, record.document_id)
                if remote_docs:
                    remote_doc = remote_docs[0]
                    status = _normalize_ragflow_status(remote_doc.get("run") or remote_doc.get("status") or status)
                    message = _extract_ragflow_error(remote_doc)
                    remote_progress = _extract_ragflow_progress(remote_doc)
                    remote_stage = _extract_ragflow_progress_message(remote_doc)
                    self.store.update_document_status(record.dataset_id, record.document_id, status, message)
                    if status in RAGFLOW_HIDDEN_TASK_STATUSES:
                        continue
            except Exception as status_error:
                if _is_ragflow_not_owner_error(status_error):
                    message = _ragflow_status_unavailable_message(record.document_name)
                    log(f"RAGFlow task status is not readable for {record.document_id}: {status_error}")
                else:
                    message = str(status_error)

            task_status, progress, stage = self._parse_task_state_from_ragflow_status(
                status,
                remote_progress=remote_progress,
                remote_stage=remote_stage,
            )
            tasks.append(
                ParseTask(
                    id=f"ragflow-{record.id}",
                    kb_name=record.kb_name,
                    source_path=record.local_path,
                    original_name=record.document_name,
                    source_group=record.source_group,
                    created_by=record.uploaded_by,
                    status=task_status,
                    progress=progress,
                    stage=stage,
                    message=message,
                    result=status,
                    document_id=_document_info_id(record.id),
                    created_at=self._parse_timestamp(record.created_at),
                    updated_at=self._parse_timestamp(record.updated_at),
                )
            )
        return sorted(tasks, key=lambda task: task.updated_at, reverse=True)

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        if not task_id.startswith("ragflow-"):
            return BackendResult(ok=False, message="Invalid RAGFlow parse task id.", backend=self.name)

        raw_record_id = task_id.removeprefix("ragflow-")
        if not raw_record_id.isdigit():
            return BackendResult(ok=False, message="Invalid RAGFlow parse task id.", backend=self.name)

        record = self.store.get_document_by_id(int(raw_record_id))
        if not record:
            return BackendResult(ok=True, message="RAGFlow parse task is already gone.", backend=self.name)

        try:
            self._check_kb_access(record.kb_name, ctx, "write")
            _check_record_department(record, ctx)
        except PermissionError as exc:
            return BackendResult(ok=False, message=str(exc), backend=self.name)

        if record.processor_kind != PROCESSOR_KIND_RAGFLOW:
            self._remove_local_archive(record)
            self.store.delete_document_by_id(record.id)
            return BackendResult(ok=True, message=f"Removed archived spreadsheet: {record.document_name}", backend=self.name)

        try:
            self.client.stop_parse_documents(record.dataset_id, [record.document_id])
        except Exception as exc:
            if not _is_ragflow_not_owner_error(exc):
                return BackendResult(ok=False, message=f"Stop RAGFlow parsing failed: {exc}", backend=self.name)
            log(f"RAGFlow stop parsing is not readable for {record.document_id}: {exc}")

        self._cleanup_remote_document(record.dataset_id, record.document_id)
        self._remove_local_archive(record)
        self.store.delete_document_by_id(record.id)
        self._audit(
            "ragflow_stop_parse_task",
            ctx,
            kb_name=record.kb_name,
            target_type="document",
            target_id=record.document_name,
            metadata={
                "store_id": record.id,
                "ragflow_document_id": record.document_id,
                "dataset_id": record.dataset_id,
                "dataset_kind": record.dataset_kind,
            },
        )
        return BackendResult(ok=True, message=f"已停止并移除未完成文档: {record.document_name}", backend=self.name)

    def _parse_task_state_from_ragflow_status(
        self,
        status: str,
        remote_progress: int | None = None,
        remote_stage: str = "",
    ) -> tuple[str, int, str]:
        if status == RAGFLOW_STATUS_PARSED:
            return "completed", 100, remote_stage or "解析完成"
        if status == RAGFLOW_STATUS_FAILED:
            return "failed", remote_progress if remote_progress is not None else 100, remote_stage or "解析失败"
        if status == RAGFLOW_STATUS_CANCELLED:
            return "cancelled", remote_progress if remote_progress is not None else 100, remote_stage or "已停止解析"
        if status == RAGFLOW_STATUS_PARSING:
            return "running", remote_progress if remote_progress is not None else 65, remote_stage or "RAGFlow 解析中"
        if status == RAGFLOW_STATUS_UPLOADED:
            return "queued", remote_progress if remote_progress is not None else 20, remote_stage or "已上传，等待解析"
        return "running", remote_progress if remote_progress is not None else 40, remote_stage or f"RAGFlow 状态: {status or RAGFLOW_STATUS_UNKNOWN}"

    def _parse_timestamp(self, value: str) -> float:
        if not value:
            return time.time()
        try:
            return time.mktime(time.strptime(value.split(".")[0], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return time.time()

    def list_documents(self, kb_name: str, ctx: RequestContext | None = None) -> list[DocumentInfo]:
        self._check_kb_access(kb_name, ctx, "read")
        documents = []
        department_id = _ctx_department_id(ctx)
        for record in self.store.list_documents(kb_name, department_id=department_id):
            status = record.status
            error_message = record.ragflow_error
            if record.processor_kind == PROCESSOR_KIND_RAGFLOW:
                try:
                    remote_docs = self.client.list_documents(record.dataset_id, record.document_id)
                    if remote_docs:
                        remote_doc = remote_docs[0]
                        status = _normalize_ragflow_status(remote_doc.get("run") or remote_doc.get("status") or status)
                        error_message = _extract_ragflow_error(remote_doc)
                        self.store.update_document_status(record.dataset_id, record.document_id, status, error_message)
                except Exception as status_error:
                    if _is_ragflow_not_owner_error(status_error):
                        error_message = _ragflow_status_unavailable_message(record.document_name)
                        log(f"RAGFlow document status is not readable for {record.document_id}: {status_error}")
                    else:
                        log(f"RAGFlow status refresh failed for {record.document_name}: {status_error}")

            documents.append(
                DocumentInfo(
                    id=_document_info_id(record.id),
                    name=record.document_name,
                    metadata={
                        "store_id": record.id,
                        "dataset_kind": record.dataset_kind,
                        "dataset_id": record.dataset_id,
                        "ragflow_document_id": record.document_id if record.processor_kind == PROCESSOR_KIND_RAGFLOW else "",
                        "table_document_id": record.document_id if record.processor_kind == PROCESSOR_KIND_SPREADSHEET else "",
                        "original_file_name": record.original_file_name,
                        "source_group": record.source_group,
                        "department_id": record.department_id,
                        "status": status,
                        "content_kind": record.content_kind,
                        "processor_kind": record.processor_kind,
                        "local_path": record.local_path,
                        "file_size": record.file_size,
                        "content_hash": record.content_hash,
                        "ragflow_error": error_message,
                    },
                    backend=self.name,
                )
            )
        return documents

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> ParseResult | None:
        self._check_kb_access(kb_name, ctx, "read")
        record = self._get_record_for_document_id(kb_name, document_id)
        if not record:
            return None
        _check_record_department(record, ctx)
        if record.processor_kind != PROCESSOR_KIND_RAGFLOW:
            return None
        try:
            raw_chunks = self.client.list_chunks(record.dataset_id, record.document_id)
        except Exception as exc:
            log(f"RAGFlow chunk fetch failed for {document_id}: {exc}")
            return None

        chunks = []
        for index, chunk in enumerate(raw_chunks):
            metadata = chunk.get("metadata") or chunk.get("meta_fields") or {}
            content = chunk.get("content") or chunk.get("text") or ""
            chunks.append(
                ParsedChunk(
                    index=index,
                    content=content,
                    metadata={
                        **metadata,
                        "source_group": record.source_group,
                        "status": record.status,
                        "score": chunk.get("similarity") or chunk.get("score"),
                        "ragflow_chunk_id": chunk.get("id") or chunk.get("chunk_id"),
                    },
                )
            )
        return ParseResult(
            document_id=_document_info_id(record.id),
            file_name=record.document_name,
            chunk_count=len(chunks),
            chunks=chunks,
            backend=self.name,
        )

    def health_check(self) -> BackendHealth:
        try:
            details = self.client.health()
            details["physical_datasets"] = self._dataset_ids
            details["dataset_names"] = {
                DATASET_GOVERNANCE: config.settings.RAGFLOW_GOVERNANCE_DATASET_NAME,
                DATASET_DESIGN: config.settings.RAGFLOW_DESIGN_DATASET_NAME,
            }
            return BackendHealth(ok=True, details=details, backend=self.name)
        except Exception as exc:
            return BackendHealth(ok=False, message=str(exc), backend=self.name)
