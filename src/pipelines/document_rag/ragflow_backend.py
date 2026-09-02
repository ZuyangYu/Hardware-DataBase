import calendar
import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

import src.settings
from src.core.auth import AuthService
from src.core.app_logs import AppLogService
from src.core.cancellation import QueryCancelled
from src.core.logger import error, log
from src.core.source_group_router import route_source_groups
from src.observability import start_thread_with_current_context
from src.ingestion.kb_paths import validate_kb_name
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
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_rag.schemas import (
    BackendHealth,
    BackendResult,
    DocumentInfo,
    Evidence,
    IngestResult,
    ParsedChunk,
    ParseResult,
    RequestContext,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DEAD_LETTER,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    normalize_parse_status,
)
from src.pipelines.ingestion import IngestionScope
from src.pipelines.runtime_factory import PipelineRuntimeFactory
from src.services.document_routing import (
    PROCESSOR_KIND_RAGFLOW,
    PROCESSOR_KIND_SPREADSHEET,
    RAGFLOW_PARSE_START_DELAY_SECONDS,
    RAGFLOW_STATUS_DELETED,
    RAGFLOW_STATUS_FAILED,
    RAGFLOW_STATUS_PARSING,
    TABLE_STATUS_ARCHIVED,
    TABLE_STATUS_INDEXED,
)
from src.services.kb_scope import KbScope, kb_scope_from_context


DATASET_GOVERNANCE = "governance"
DATASET_DESIGN = "design"
RAGFLOW_STATUS_UPLOADED = "uploaded"
RAGFLOW_STATUS_PARSED = "parsed"
RAGFLOW_STATUS_CANCELLED = "cancelled"
RAGFLOW_STATUS_UNKNOWN = "unknown"
RAGFLOW_HIDDEN_TASK_STATUSES = {
    RAGFLOW_STATUS_PARSED,
    RAGFLOW_STATUS_DELETED,
    RAGFLOW_STATUS_CANCELLED,
    TABLE_STATUS_ARCHIVED,
    TABLE_STATUS_INDEXED,
    "completed",
    "complete",
    "done",
    "finish",
    "finished",
    "success",
    "已完成",
}
RAGFLOW_TERMINAL_REMOVABLE_TASK_STATUSES = {
    TASK_STATUS_CANCELLED,
    TASK_STATUS_DEAD_LETTER,
    TASK_STATUS_FAILED,
    RAGFLOW_STATUS_CANCELLED,
    RAGFLOW_STATUS_DELETED,
    RAGFLOW_STATUS_FAILED,
}
RAGFLOW_DOCUMENT_READY_TIMEOUT_SECONDS = 10.0
# RAGFlow 解析卡死兜底阈值:list_parse_tasks/list_documents 检测到 parsing 记录
# 超过该时长仍未到终态时,标记 failed 并删除远端卡住的文档(见 _ragflow_parse_timed_out)。
RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS = 3600.0
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

_LEGACY_SOURCE_GROUP_ALIASES = {
    "docs": DOCS_GROUP,
    "document": DOCS_GROUP,
    "design": DESIGN_GROUP,
    "material": MATERIAL_GROUP,
    "test": TEST_GROUP,
    "project": PROJECT_GROUP,
    "external": EXTERNAL_GROUP,
    "people": PEOPLE_GROUP,
}


def _normalize_chunk_source_group(value: object) -> str:
    raw = str(value or "").strip()
    return _LEGACY_SOURCE_GROUP_ALIASES.get(raw.casefold(), safe_source_group(raw))


@dataclass
class ProcessingResult:
    document_id: str
    status: str
    message: str = ""
    backend: str = "ragflow"


@dataclass
class ProcessingSubmission:
    document_id: str
    backend: str = "ragflow"


class RAGFlowAPIError(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = payload
        self.code = payload.get("code")
        self.message = str(payload.get("message") or payload)
        super().__init__(f"RAGFlow API error: {payload}")


def _is_ragflow_not_owner_error(exc: Exception) -> bool:
    return isinstance(exc, RAGFlowAPIError) and exc.code == 102


def _is_ragflow_not_found_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 404
    if isinstance(exc, RAGFlowAPIError):
        text = str(getattr(exc, "message", "") or "").lower()
        return "not found" in text or "does not exist" in text or "doesn't exist" in text
    return False


def _ragflow_status_unavailable_message(document_name: str) -> str:
    return f"{document_name}: RAGFlow parse submitted; realtime progress is not readable by the current API key."


def _is_ragflow_status_unavailable_text(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "realtime progress is not readable" in text
        or "you don't own the document" in text
        or "you do not own the document" in text
    )


def _ragflow_submission_error(file_name: str, step: str, exc: Exception) -> RuntimeError:
    hint = ""
    if _is_ragflow_not_owner_error(exc):
        hint = (
            " Hint: the current API key can access the dataset but does not own this document. "
            "Delete the remote document in RAGFlow with the owner/admin account, or re-upload "
            "with the API key that owns the target dataset/document."
        )
    return RuntimeError(f"{file_name}: RAGFlow {step} failed: {exc}.{hint}")


def _ctx_department_id(ctx: RequestContext | None) -> str:
    if ctx is None:
        return ""
    metadata = ctx.metadata or {}
    department_id = metadata.get("resource_department_id")
    if department_id in (None, ""):
        department_id = metadata.get("department_id")
    return "" if department_id in (None, "") else str(department_id)


def _scope_for_kb(kb_name: str, ctx: RequestContext | None) -> KbScope:
    return kb_scope_from_context(kb_name, ctx)


def _resolve_kb_id(scope: KbScope) -> int | None:
    if scope.kb_id is not None:
        return scope.kb_id
    if not scope.department_id:
        return None
    try:
        return AuthService().get_knowledge_base_id(scope.kb_name, department_id=scope.department_id)
    except Exception as exc:
        log(f"Could not resolve knowledge-base id for {scope.department_id}:{scope.kb_name}: {exc}")
        return None


def _as_non_empty_strings(values: object) -> list[str]:
    if values in (None, ""):
        return []
    if isinstance(values, (list, tuple, set)):
        raw_values = values
    else:
        raw_values = [values]
    result = []
    seen = set()
    for value in raw_values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _source_name_filters(filters: dict | None) -> list[str]:
    source_names = _as_non_empty_strings((filters or {}).get("source_names"))
    if not source_names:
        source_names = _as_non_empty_strings((filters or {}).get("source_name"))
    return source_names


def _as_allowed_record_ids(filters: dict | None) -> list[int] | None:
    """Return normalized ``allowed_record_ids`` or ``None`` when absent.

    An empty list is meaningful (strict allow-nothing) and stays distinct
    from an absent filter.
    """
    raw = (filters or {}).get("allowed_record_ids")
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    values: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            return []
        values.append(value)
    return sorted(set(values))


def _as_link_stamps(filters: dict | None) -> dict[str, dict[str, Any]]:
    raw = (filters or {}).get("link_stamps")
    if not isinstance(raw, dict):
        return {}
    stamps: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            stamps[str(key)] = value
    return stamps


def _metadata_condition(
    kb_name: str,
    ctx: RequestContext | None,
    source_groups: tuple[str, ...] = (),
    filters: dict | None = None,
    include_source_names: bool = True,
) -> dict:
    conditions = [{"name": "kb_name", "comparison_operator": "=", "value": kb_name}]
    scope = _scope_for_kb(kb_name, ctx)
    if scope.department_id:
        conditions.append({"name": "department_id", "comparison_operator": "=", "value": scope.department_id})
    if len(source_groups) == 1:
        conditions.append({"name": "source_group", "comparison_operator": "=", "value": source_groups[0]})
    elif len(source_groups) > 1:
        conditions.append({"name": "source_group", "comparison_operator": "in", "value": list(source_groups)})
    if include_source_names:
        source_names = _source_name_filters(filters)
        if len(source_names) == 1:
            conditions.append({"name": "original_file_name", "comparison_operator": "=", "value": source_names[0]})
        elif len(source_names) > 1:
            conditions.append({"name": "original_file_name", "comparison_operator": "in", "value": source_names})
    return {"logical_operator": "and", "conditions": conditions}


def _check_record_department(record, ctx: RequestContext | None):
    scope = kb_scope_from_context(record.kb_name, ctx)
    if not scope.department_id or str(record.department_id or "") != scope.department_id:
        raise PermissionError("Document does not belong to the current department.")


def _file_sha256(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _ragflow_parse_timed_out(
    record,
    now_ts: float | None = None,
    threshold_seconds: float = RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS,
) -> bool:
    """本地判定:RAGFlow 记录是否解析超时(未到终态且超过阈值)。

    只看本地 parse_started_at,不调远端 API,因此 API 不可达时仍能触发。
    now_ts 供测试注入;默认用 time.time()。
    """
    if normalize_parse_status(record.status) != TASK_STATUS_RUNNING:
        return False
    started = (record.parse_started_at or "").strip()
    if not started:
        return False
    # parse_started_at is stored in UTC (SQLite CURRENT_TIMESTAMP / utc_now()),
    # so it must be parsed as UTC via calendar.timegm — NOT time.mktime, which
    # interprets the string in local time and skews the diff by the UTC offset
    # (e.g. +8h), falsely tripping the 3600s timeout and marking live parses
    # as failed (which then surfaces as RAGFlow code 102 on the frontend).
    try:
        started_ts = calendar.timegm(time.strptime(started.split(".")[0], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return False
    now = now_ts if now_ts is not None else time.time()
    return (now - started_ts) > threshold_seconds


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
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int | float | None = None,
        async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ):
        self.base_url = (base_url or src.settings.RAGFLOW_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else src.settings.RAGFLOW_API_KEY
        self.timeout = timeout if timeout is not None else src.settings.RAGFLOW_TIMEOUT_SECONDS
        self._async_client_factory = async_client_factory or httpx.AsyncClient
        if not self.api_key:
            raise ValueError("RAGFLOW_API_KEY is not configured.")
        # RAGFlowBackend reuses one client for the process. Keep the sync
        # connection pool alive across retrievals instead of creating a new
        # TCP client for every request.
        self._sync_client = httpx.Client(timeout=self.timeout, follow_redirects=True)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def request(self, method: str, path: str, **kwargs) -> dict:
        response = self._sync_client.request(
            method,
            self._url(path),
            headers={**self.headers, **kwargs.pop("headers", {})},
            **kwargs,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code") not in {None, 0}:
            raise RAGFlowAPIError(data)
        return data

    def close(self) -> None:
        """Close the process-level sync connection pool during pipeline reset."""
        self._sync_client.close()

    async def _async_request_until_cancelled(
        self,
        method: str,
        path: str,
        cancel_event: threading.Event,
        **kwargs,
    ) -> dict:
        """Run one RAGFlow request and cancel its client task on demand."""
        try:
            async with self._async_client_factory(timeout=self.timeout) as client:
                request_task = asyncio.create_task(
                    client.request(
                        method,
                        self._url(path),
                        headers={**self.headers, **kwargs.pop("headers", {})},
                        **kwargs,
                    )
                )
                cancel_task = asyncio.create_task(asyncio.to_thread(cancel_event.wait))
                done, _ = await asyncio.wait({request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done:
                    request_task.cancel()
                    try:
                        await request_task
                    except asyncio.CancelledError:
                        pass
                    raise QueryCancelled()

                cancel_task.cancel()
                response = request_task.result()
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("code") not in {None, 0}:
                    raise RAGFlowAPIError(data)
                return data
        finally:
            # Release asyncio.to_thread(cancel_event.wait) on successful calls.
            cancel_event.set()

    def request_cancellable(
        self,
        method: str,
        path: str,
        *,
        should_cancel: Callable[[], bool],
        **kwargs,
    ) -> dict:
        """Synchronously wait for an async HTTP request that can be cancelled.

        The agent graph remains synchronous, while the dedicated request thread
        owns an AsyncClient task. Setting the event causes that task to be
        cancelled, which closes the client-side HTTP wait/socket immediately.
        """
        if should_cancel():
            raise QueryCancelled()

        cancel_event = threading.Event()
        completed = threading.Event()
        outcome: dict[str, object] = {}

        def run_request() -> None:
            try:
                outcome["data"] = asyncio.run(
                    self._async_request_until_cancelled(method, path, cancel_event, **kwargs)
                )
            except Exception as exc:  # Propagate transport and API errors unchanged.
                outcome["error"] = exc
            finally:
                completed.set()

        start_thread_with_current_context(
            run_request,
            name="ragflow-retrieval-request",
            daemon=True,
        )
        while not completed.wait(0.05):
            if should_cancel():
                cancel_event.set()

        request_error = outcome.get("error")
        if isinstance(request_error, Exception):
            raise request_error
        return outcome["data"]  # type: ignore[return-value]

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

    def retrieve(
        self,
        question: str,
        dataset_ids: list[str],
        top_k: int,
        metadata_condition: dict | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[dict]:
        payload = {
            "question": question,
            "dataset_ids": dataset_ids,
            "page": 1,
            "page_size": top_k,
            "similarity_threshold": src.settings.RAGFLOW_SIMILARITY_THRESHOLD,
            "vector_similarity_weight": src.settings.RAGFLOW_VECTOR_WEIGHT,
            "top_k": top_k,
        }
        if metadata_condition:
            payload["metadata_condition"] = metadata_condition
        if should_cancel is None:
            data = self.request("POST", "/api/v1/retrieval", json=payload)
        else:
            data = self.request_cancellable(
                "POST",
                "/api/v1/retrieval",
                json=payload,
                should_cancel=should_cancel,
            )
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
    # Serialises dataset creation across threads in this process. Two concurrent
    # retrieve()/upload_files() calls could otherwise both see no saved mapping
    # for a kind and each create a duplicate RAGFlow dataset.
    _DATASET_LOCK = threading.Lock()

    def __init__(self):
        self._client_instance: RAGFlowClient | None = None
        runtime_bundle = PipelineRuntimeFactory(
            backend_name=self.name,
            submit_remote_callback=self._submit_archived_document,
            cleanup_remote_callback=self._cleanup_remote_document,
            delete_remote_callback=lambda dataset_id, document_id: self._client().delete_documents(
                dataset_id,
                [document_id],
            ),
            audit_callback=self._audit,
            content_hash_callback=_file_sha256,
            remote_document_exists_callback=self._remote_document_exists,
            worker_name="ragflow-parse-worker",
        ).build()
        self.store = runtime_bundle.store
        self.spreadsheet_indexes = runtime_bundle.spreadsheet_indexes
        self.circuit_indexes = runtime_bundle.circuit_indexes
        self.conversations = runtime_bundle.conversations
        self.conversation_indexes = runtime_bundle.conversation_indexes
        self.archive = runtime_bundle.archive
        self.ingestion = runtime_bundle.ingestion
        self.runtime = runtime_bundle.runtime
        self._dataset_ids: dict[str, str] = {}

    def list_knowledge_bases(self) -> list[str]:
        return AuthService().list_registered_knowledge_bases()

    def create_kb_storage(self, kb_name: str, ctx: RequestContext | None = None) -> None:
        # RAGFlow 后端为逻辑知识库，物理 Dataset 按 source_group 共享，无需预建存储。
        return None

    def _client(self) -> RAGFlowClient:
        client = getattr(self, "_client_instance", None)
        if client is not None:
            return client
        legacy_client = getattr(self, "client", None)
        if legacy_client is not None:
            self._client_instance = legacy_client
            return legacy_client
        self._client_instance = RAGFlowClient()
        return self._client_instance

    def close(self) -> None:
        client = getattr(self, "_client_instance", None)
        if client is not None:
            client.close()

    def _ensure_physical_datasets(self):
        dataset_specs = {
            DATASET_GOVERNANCE: src.settings.RAGFLOW_GOVERNANCE_DATASET_NAME,
            DATASET_DESIGN: src.settings.RAGFLOW_DESIGN_DATASET_NAME,
        }
        dataset_ids = getattr(self, "_dataset_ids", None)
        if not isinstance(dataset_ids, dict):
            dataset_ids = {}
            self._dataset_ids = dataset_ids
        store = getattr(self, "store", None)
        get_dataset = getattr(store, "get_dataset", None)
        for kind, name in dataset_specs.items():
            saved_mapping = get_dataset(kind) if callable(get_dataset) else None
            saved_id, saved_name = saved_mapping or ("", "")
            if saved_id and saved_name == name:
                dataset_ids[kind] = saved_id
                continue

            if not callable(get_dataset) and dataset_ids.get(kind):
                continue

            # Hold a process-wide lock so two concurrent callers don't both
            # create the same dataset; re-check the saved mapping inside the
            # lock in case another thread just created it.
            with self._DATASET_LOCK:
                if callable(get_dataset):
                    saved_mapping = get_dataset(kind)
                    saved_id, saved_name = saved_mapping or ("", "")
                    if saved_id and saved_name == name:
                        dataset_ids[kind] = saved_id
                        continue
                dataset_id = self._client().ensure_dataset(name)
                if store is not None:
                    store.save_dataset(kind, dataset_id, name)
                dataset_ids[kind] = dataset_id

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

    def _dataset_id_for_kind(self, dataset_kind: str) -> str:
        self._ensure_physical_datasets()
        return self._dataset_ids[dataset_kind]

    def _metadata(self, kb_name: str, filename: str, source_group: str | None, ctx: RequestContext | None) -> dict:
        scope = _scope_for_kb(kb_name, ctx)
        kb_id = _resolve_kb_id(scope)
        return {
            "kb_name": kb_name,
            "logical_kb_id": kb_name,
            "kb_id": str(kb_id or ""),
            "department_id": scope.department_id,
            "source_group": safe_source_group(source_group),
            "uploaded_by": ctx.user_id if ctx else "",
            "original_file_name": filename,
        }

    def _cleanup_remote_document(self, dataset_id: str, document_id: str):
        if not dataset_id or not document_id:
            return
        try:
            self._client().delete_documents(dataset_id, [document_id])
        except Exception as cleanup_error:
            log(f"RAGFlow remote cleanup failed for {document_id}: {cleanup_error}")

    def _remote_document_exists(self, record) -> bool | None:
        """Three-state remote lookup: True (exists), False (definitively absent), None (undetermined).

        Only an authoritative miss from RAGFlow may return False. Transport-level
        failures (connection errors, timeouts, 5xx, any other exception) return
        None so callers never tear down records on inconclusive evidence.
        """
        try:
            return bool(self._client().list_documents(record.dataset_id, record.document_id))
        except Exception as exc:
            if _is_ragflow_not_owner_error(exc):
                log(f"RAGFlow duplicate check cannot read status for {record.document_id}; keeping local mapping: {exc}")
                return True
            if _is_ragflow_not_found_error(exc):
                log(f"RAGFlow duplicate check found no usable remote document {record.document_id}: {exc}")
                return False
            log(f"RAGFlow duplicate check could not verify remote document {record.document_id}: {exc}")
            return None

    def _process_parse_record(self, record):
        self.runtime.process_record(record)


    def _delete_pipeline_record(self, record):
        return self.runtime.delete_record(record)

    def _get_record_for_document_id(self, kb_name: str, document_id: str, ctx: RequestContext | None = None):
        record_id = _record_id_from_document_id(document_id)
        if record_id is not None:
            record = self.store.get_document_by_id_scoped(record_id, _ctx_department_id(ctx))
            if record and record.kb_name == kb_name:
                return record
            return None
        return self.store.get_document(kb_name, document_id, department_id=_ctx_department_id(ctx))

    def upload_files(
        self,
        kb_name: str,
        files: list[str],
        ctx: RequestContext | None = None,
        source_group: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> IngestResult:
        self._check_kb_access(kb_name, ctx, "write")
        scope = _scope_for_kb(kb_name, ctx).require_department("upload to")
        dataset_kind = self._dataset_kind_for_group(source_group)
        dataset_id = self._dataset_id_for_kind(dataset_kind)
        ingestion_scope = IngestionScope(
            kb_name=scope.kb_name,
            department_id=scope.department_id,
            kb_id=_resolve_kb_id(scope),
            uploaded_by=ctx.user_id if ctx else "",
            source_group=source_group,
            ctx=ctx,
        )
        return self.ingestion.upload_files(
            files,
            ingestion_scope,
            default_dataset_kind=dataset_kind,
            default_dataset_id=dataset_id,
            progress_callback=progress_callback,
        )

    def _submit_archived_document(
        self,
        dataset_id: str,
        kb_name: str,
        file_name: str,
        archived_path: str,
        source_group: str,
        ctx: RequestContext | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> ProcessingSubmission:
        client = self._client()
        document_id = ""
        try:
            try:
                document_id = client.upload_document(dataset_id, archived_path)
            except Exception as exc:
                raise _ragflow_submission_error(file_name, "upload document", exc) from exc

            metadata = self._metadata(kb_name, file_name, source_group, ctx)
            try:
                client.update_document_metadata(dataset_id, document_id, metadata)
            except Exception as exc:
                raise _ragflow_submission_error(file_name, "update metadata", exc) from exc

            try:
                client.wait_document_ready(dataset_id, document_id)
            except Exception as exc:
                if _is_ragflow_not_owner_error(exc):
                    log(f"RAGFlow document status is not readable after upload for {document_id}: {exc}")
                    if progress_callback:
                        progress_callback(5, _ragflow_status_unavailable_message(file_name))
                else:
                    raise _ragflow_submission_error(file_name, "read document status", exc) from exc

            time.sleep(RAGFLOW_PARSE_START_DELAY_SECONDS)
            try:
                client.parse_documents(dataset_id, [document_id])
            except Exception as exc:
                raise _ragflow_submission_error(file_name, "submit parse", exc) from exc
            if progress_callback:
                progress_callback(5, f"{file_name}: 已提交到 RAGFlow 解析队列")
        except Exception:
            if document_id:
                self._cleanup_remote_document(dataset_id, document_id)
            raise
        return ProcessingSubmission(document_id=document_id, backend=self.name)

    def _wait_archived_document_result(
        self,
        dataset_id: str,
        document_id: str,
        file_name: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> ProcessingResult:
        status, message = self._wait_parse_progress(
            dataset_id,
            document_id,
            file_name,
            progress_callback=progress_callback,
        )
        return ProcessingResult(
            document_id=document_id,
            status=status,
            message=message,
            backend=self.name,
        )

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
                remote_docs = self._client().list_documents(dataset_id, document_id)
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

    def retrieve(
        self,
        kb_name: str,
        query: str,
        top_k: int | None = None,
        ctx: RequestContext | None = None,
        filters: dict | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[Evidence]:
        self._check_kb_access(kb_name, ctx, "read")
        scope = _scope_for_kb(kb_name, ctx).require_department("retrieve from")
        scoped_records = self.store.list_documents(kb_name, department_id=scope.department_id)
        records_by_remote_id = {
            str(record.document_id): record
            for record in scoped_records
            if record.processor_kind == PROCESSOR_KIND_RAGFLOW
            and record.document_id
            and normalize_parse_status(record.status, record.processor_kind) == TASK_STATUS_COMPLETED
        }
        top_k = top_k or src.settings.FINAL_TOP_K
        route = route_source_groups(query)
        # Task 5a strict path: when the caller supplies ``allowed_record_ids``
        # retrieval is restricted to exactly those local records, verified by
        # remote document_id at chunk read-back. Any unknown/unauthorized/
        # stale id fails closed to an empty result — never a broad search.
        allowed_record_ids = _as_allowed_record_ids(filters)
        link_stamps = _as_link_stamps(filters)
        strict_allowed = allowed_record_ids is not None
        if strict_allowed:
            if not allowed_record_ids:
                return []
            allowed_id_set = set(allowed_record_ids)
            # Same authorization gates as the regular path: ragflow processor,
            # remote id present and parse completed — anything else fails closed.
            authorized = [
                record
                for record in scoped_records
                if record.id in allowed_id_set
                and record.processor_kind == PROCESSOR_KIND_RAGFLOW
                and record.document_id
                and normalize_parse_status(record.status, record.processor_kind) == TASK_STATUS_COMPLETED
            ]
            if len(authorized) != len(allowed_id_set):
                log(
                    "RAGFlow strict record filter failed closed: "
                    f"requested={sorted(allowed_id_set)} authorized={sorted(r.id for r in authorized)}"
                )
                return []
            scoped_records = authorized
            if link_stamps:
                for record in scoped_records:
                    stamp = link_stamps.get(str(record.id)) or {}
                    if (
                        str(stamp.get("content_hash") or "") != str(record.content_hash or "")
                        or str(stamp.get("source_version_id") or "") != str(record.source_version_id or "")
                        or str(stamp.get("revision") or "") != str(record.revision or "")
                    ):
                        log(f"RAGFlow strict record filter: version stamp mismatch for record {record.id}.")
                        return []
            records_by_remote_id = {
                str(record.document_id): record
                for record in scoped_records
                if record.processor_kind == PROCESSOR_KIND_RAGFLOW
                and record.document_id
            }
        # Stage 5 adaptive recovery: balanced_route drops the source_group hard
        # filter so a query that mis-routed (the frozen evidence lives in a
        # different group) can still reach frozen sources. The frozen
        # source_names scope and the local _filter_chunks source_name check
        # remain, so this never widens the result beyond the frozen source set.
        balanced_route = bool((filters or {}).get("balanced_route"))
        if balanced_route:
            routed_source_groups = ()
        else:
            routed_source_groups = route.source_groups if route.should_filter else ()
        if routed_source_groups:
            log(f"RAGFlow source-group route: {route.reason}, filter={routed_source_groups}")
        else:
            log(
                "RAGFlow source-group route: "
                + route.reason
                + (", balanced_route" if balanced_route else "")
                + ", no hard filter"
            )
        self._ensure_physical_datasets()
        # Shared read-only records may point at a physical Dataset owned by
        # observability rather than the develop instance's upload Dataset.
        # Include those mapped Dataset IDs in the remote search, while the
        # local records_by_remote_id filter below still limits results to this
        # department's registered documents.
        mapped_dataset_ids = [
            record.dataset_id
            for record in scoped_records
            if record.shared_read_only
            and record.processor_kind == PROCESSOR_KIND_RAGFLOW
            and record.dataset_id
        ]
        dataset_ids = list(dict.fromkeys([*self._dataset_ids.values(), *mapped_dataset_ids]))
        shared_dataset_ids = {
            record.dataset_id
            for record in scoped_records
            if record.shared_read_only
            and record.processor_kind == PROCESSOR_KIND_RAGFLOW
            and record.dataset_id
        }
        retrieval_dataset_groups = (
            [[dataset_id] for dataset_id in dataset_ids]
            if shared_dataset_ids
            else [dataset_ids]
        )
        metadata_condition = _metadata_condition(kb_name, ctx, routed_source_groups, filters=filters)

        def retrieve_chunks(condition: dict | None) -> list[dict]:
            chunks: list[dict] = []
            for retrieval_dataset_ids in retrieval_dataset_groups:
                retrieve_kwargs = {
                    "dataset_ids": retrieval_dataset_ids,
                    "top_k": top_k,
                    "metadata_condition": condition,
                }
                if should_cancel is not None:
                    retrieve_kwargs["should_cancel"] = should_cancel
                chunks.extend(self._client().retrieve(query, **retrieve_kwargs))
            if len(retrieval_dataset_groups) > 1:
                def _chunk_score(chunk: dict) -> float:
                    value = chunk.get("similarity") or chunk.get("score") or chunk.get("vector_similarity") or 0.0
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return 0.0

                chunks.sort(key=_chunk_score, reverse=True)
            return chunks

        chunks = retrieve_chunks(metadata_condition)
        source_names = _source_name_filters(filters)
        if strict_allowed:
            # The strict linked-record path never widens: no unconditioned
            # retry, no filename fallback, no route-based group widening.
            apply_routed_source_groups = False
            metadata_condition_fallback = False
            source_name_fallback = False
        else:
            # An explicit file selection is a stronger scope than keyword routing.
            # It remains constrained by the locally scoped document mapping below,
            # so a mismatched route must not suppress the selected source.
            apply_routed_source_groups = bool(routed_source_groups) and not source_names
            source_name_fallback = False
            metadata_condition_fallback = False
            if not chunks:
                chunks = retrieve_chunks(None)
                metadata_condition_fallback = True
                source_name_fallback = bool(source_names)
                log(
                    "RAGFlow metadata-scoped retrieve returned 0; retried without "
                    f"metadata conditions and received {len(chunks)} raw chunks. "
                    f"source_names={source_names}"
                )

        # RAGFlow retrieval chunks may omit document-level metadata. Resolve
        # their scope through our local, department-scoped document mapping so
        # missing fields do not drop valid chunks and untracked remote chunks
        # cannot bypass the application's access controls.
        def _filter_chunks() -> tuple[list[Evidence], dict[str, int]]:
            evidences = []
            skipped_counts = {
                "document": 0,
                "kb": 0,
                "department": 0,
                "source_group": 0,
                "source_name": 0,
            }
            for chunk in chunks:
                metadata = dict(chunk.get("metadata") or chunk.get("meta_fields") or {})
                remote_document_id = str(chunk.get("document_id") or "").strip()
                record = records_by_remote_id.get(remote_document_id)
                if record is None:
                    skipped_counts["document"] += 1
                    continue

                chunk_kb = str(metadata.get("kb_name") or metadata.get("logical_kb_id") or "")
                if chunk_kb and chunk_kb != record.kb_name:
                    skipped_counts["kb"] += 1
                    continue
                chunk_department = str(metadata.get("department_id") or "")
                if chunk_department and chunk_department != record.department_id:
                    skipped_counts["department"] += 1
                    continue
                record_source_group = _normalize_chunk_source_group(record.source_group)
                chunk_source_group = str(metadata.get("source_group") or "").strip()
                if chunk_source_group and _normalize_chunk_source_group(chunk_source_group) != record_source_group:
                    skipped_counts["source_group"] += 1
                    continue
                if apply_routed_source_groups and record_source_group not in routed_source_groups:
                    skipped_counts["source_group"] += 1
                    continue

                canonical_source_name = str(
                    record.original_file_name
                    or record.document_name
                    or chunk.get("document_keyword")
                    or chunk.get("document_name")
                    or metadata.get("original_file_name")
                    or ""
                )
                if source_names and canonical_source_name not in source_names:
                    skipped_counts["source_name"] += 1
                    continue

                metadata.update({
                    "kb_name": record.kb_name,
                    "department_id": record.department_id,
                    "source_group": record_source_group,
                    "original_file_name": canonical_source_name,
                    "ragflow_document_id": remote_document_id,
                })
                content = chunk.get("content") or chunk.get("text") or ""
                score = chunk.get("similarity") or chunk.get("score") or chunk.get("vector_similarity") or 0.0
                evidences.append(
                    Evidence(
                        id=str(chunk.get("id") or chunk.get("chunk_id") or ""),
                        content=content,
                        source_name=canonical_source_name,
                        score=float(score or 0.0),
                        metadata={
                            **metadata,
                            "query_route_reason": route.reason,
                            "query_route_confidence": route.confidence,
                            "query_route_source_groups": list(routed_source_groups),
                            "ragflow_source_name_fallback": source_name_fallback,
                            "ragflow_metadata_condition_fallback": metadata_condition_fallback,
                            **({"local_record_id": record.id} if strict_allowed else {}),
                        },
                        backend=self.name,
                        retriever="ragflow_retrieval",
                    )
                )
            return evidences, skipped_counts

        evidences, skipped_counts = _filter_chunks()
        if not evidences and source_names and not source_name_fallback and skipped_counts["source_name"]:
            chunks = retrieve_chunks(None)
            source_name_fallback = True
            metadata_condition_fallback = True
            evidences, skipped_counts = _filter_chunks()
            log(
                "RAGFlow scoped source-name retrieve was emptied by local filename validation; "
                "retried without metadata conditions and received "
                f"{len(chunks)} raw chunks. source_names={source_names}"
            )
        if not evidences:
            log(
                "RAGFlow retrieve produced no evidence after filters: "
                f"raw_chunks={len(chunks)}, skipped={skipped_counts}, "
                f"source_names={source_names}, route={route.reason}, "
                f"metadata_condition={metadata_condition}, fallback={metadata_condition_fallback}"
            )
        return evidences[:top_k]

    def delete_document(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> BackendResult:
        self._check_kb_access(kb_name, ctx, "write")
        record = self._get_record_for_document_id(kb_name, document_id, ctx=ctx)
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
        if record.shared_read_only:
            self._audit(
                "shared_document_mutation_denied",
                ctx,
                kb_name=kb_name,
                target_type="document",
                target_id=record.document_name,
                success=False,
                error_message="shared document is read-only",
                metadata={
                    "store_id": record.id,
                    "dataset_id": record.dataset_id,
                    "ragflow_document_id": record.document_id,
                },
            )
            return BackendResult(
                ok=False,
                message=f"共享文档为只读，develop 实例不能删除或重新解析: {record.document_name}",
                backend=self.name,
            )
        try:
            if record.processor_kind == PROCESSOR_KIND_RAGFLOW:
                delete_result = self._delete_pipeline_record(record)
            else:
                # spreadsheet：索引+归档+store 行原子化清理，避免孤儿
                delete_result = self._delete_pipeline_record(record)
            self._audit(
                delete_result.audit_action or f"{record.processor_kind}_delete_document",
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
                    "cleanup_errors": delete_result.errors,
                },
            )
            return BackendResult(ok=delete_result.ok, message=delete_result.message, backend=self.name)
        except Exception as exc:
            if _is_ragflow_not_owner_error(exc):
                self.archive.remove_record_archive(record)
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
        try:
            scope = _scope_for_kb(kb_name, ctx).require_department("delete")
        except PermissionError as exc:
            return BackendResult(ok=False, message=str(exc), backend=self.name)
        records = self.store.list_documents(scope.kb_name, department_id=scope.department_id)
        if any(record.shared_read_only for record in records):
            return BackendResult(
                ok=False,
                message="知识库包含 observability 拥有的共享只读文档，develop 实例不能删除该知识库。",
                backend=self.name,
            )
        errors = []
        deleted_record_ids = []
        for record in records:
            try:
                _check_record_department(record, ctx)
            except PermissionError as exc:
                errors.append(f"{record.document_name}: {exc}")
                continue
            try:
                delete_result = self._delete_pipeline_record(record)
            except Exception as exc:
                if _is_ragflow_not_owner_error(exc):
                    log(f"RAGFlow remote document is not readable while deleting kb {kb_name}: {record.document_id}, {exc}")
                    try:
                        self.archive.remove_record_archive(record)
                        self.store.delete_document_by_id(record.id)
                        deleted_record_ids.append(record.id)
                    except Exception as cleanup_exc:
                        errors.append(f"{record.document_name}: local cleanup: {cleanup_exc}")
                    continue
                errors.append(f"{record.document_name}: {exc}")
                continue
            if not delete_result.ok:
                errors.append(f"{record.document_name}: {delete_result.message}")
                continue
            if delete_result.errors:
                errors.append(f"{record.document_name}: {'; '.join(delete_result.errors)}")
                log(f"Pipeline cleanup had partial errors during kb purge for {record.id}: {'; '.join(delete_result.errors)}")
                continue
            deleted_record_ids.append(record.id)

        if errors:
            for record_id in deleted_record_ids:
                try:
                    self.store.delete_document_by_id(record_id)
                except Exception as cleanup_exc:
                    errors.append(f"record {record_id}: store cleanup: {cleanup_exc}")
            return BackendResult(
                ok=False,
                message=f"RAGFlow 知识库删除失败，部分文档未清理: {'; '.join(errors)}",
                backend=self.name,
            )

        self.store.delete_documents_by_kb(scope.kb_name, department_id=scope.department_id)
        self._audit(
            "ragflow_delete_knowledge_base",
            ctx,
            kb_name=kb_name,
            target_type="knowledge_base",
            target_id=kb_name,
            metadata={"document_count": len(records)},
        )
        return BackendResult(ok=True, message=f"RAGFlow 知识库 '{kb_name}' 已删除", backend=self.name)

    def _mark_ragflow_parse_timed_out(self, record) -> str:
        """把卡死的 RAGFlow 记录标记为 failed,并删除远端卡住的文档。

        远端不可达时 cleanup 失败仅 log,仍标记本地 failed,避免记录无限停在 parsing。
        返回给 UI 显示的超时消息。
        """
        if record.shared_read_only:
            return f"共享文档由 observability 实例负责解析，develop 仅同步只读状态: {record.document_name}"
        timeout_msg = (
            f"解析超时(已等待超过 {int(RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS)}s 未完成),"
            "已终止并标记失败,请重新上传或检查 RAGFlow 服务状态"
        )
        try:
            self._cleanup_remote_document(record.dataset_id, record.document_id)
        except Exception as exc:
            error(f"Failed to cleanup timed-out RAGFlow document {record.document_id}: {exc}")
        self.store.update_document_status_by_id(record.id, RAGFLOW_STATUS_FAILED, timeout_msg)
        return timeout_msg

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None) -> list[ParseTask]:
        if kb_name:
            self._check_kb_access(kb_name, ctx, "read")
        department_id = _ctx_department_id(ctx)
        records = self.store.list_documents(kb_name, department_id=department_id) if kb_name else []
        tasks = []
        for record in records:
            if record.shared_read_only:
                # Parse-task controls are mutating operations.  Shared
                # documents remain visible through list_documents, while
                # their source-owned parse task is intentionally not exposed
                # to the develop instance.
                continue
            if record.processor_kind == PROCESSOR_KIND_SPREADSHEET:
                if normalize_parse_status(record.status) == TASK_STATUS_COMPLETED:
                    continue
                task_status = normalize_parse_status(record.status)
                progress = record.parse_progress or (10 if task_status == TASK_STATUS_QUEUED else 65)
                stage = record.parse_stage or ("表格结构化解析中" if task_status == TASK_STATUS_RUNNING else "等待表格结构化解析")
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
                        message=record.ragflow_error,
                        result=f"{PROCESSOR_KIND_SPREADSHEET}:{record.status}",
                        document_id=_document_info_id(record.id),
                        created_at=self._parse_timestamp(record.created_at),
                        updated_at=self._parse_timestamp(record.updated_at),
                    )
                )
                continue

            if record.status in RAGFLOW_HIDDEN_TASK_STATUSES:
                continue
            # 先用远端真实状态刷新本地,再判定超时:避免本地 status 停在 parsing
            # 但远端其实已解析完成时,被超时误判并删除远端文档(数据丢失)。
            status = record.status
            message = record.ragflow_error
            remote_progress = None
            remote_stage = ""
            refreshed = False
            try:
                remote_docs = self._client().list_documents(record.dataset_id, record.document_id)
                if remote_docs:
                    remote_doc = remote_docs[0]
                    status = _normalize_ragflow_status(remote_doc.get("run") or remote_doc.get("status") or status)
                    message = _extract_ragflow_error(remote_doc)
                    remote_progress = _extract_ragflow_progress(remote_doc)
                    remote_stage = _extract_ragflow_progress_message(remote_doc)
                    self.store.update_document_status(record.dataset_id, record.document_id, status, message)
                    if remote_progress is not None or remote_stage:
                        self.store.update_document_progress_by_id(
                            record.id,
                            remote_progress if remote_progress is not None else record.parse_progress,
                            remote_stage or record.parse_stage,
                        )
                    refreshed = True
                    if status in RAGFLOW_HIDDEN_TASK_STATUSES:
                        continue
            except Exception as status_error:
                if _is_ragflow_not_owner_error(status_error):
                    message = _ragflow_status_unavailable_message(record.document_name)
                    log(f"RAGFlow task status is not readable for {record.document_id}: {status_error}")
                else:
                    message = str(status_error)
            # 仅当远端确认仍在解析、且确实超过阈值时才标记超时;远端不可达/状态不明时不删远端。
            if (
                refreshed
                and normalize_parse_status(status) == TASK_STATUS_RUNNING
                and _ragflow_parse_timed_out(record)
            ):
                timeout_msg = self._mark_ragflow_parse_timed_out(record)
                tasks.append(
                    ParseTask(
                        id=f"ragflow-{record.id}",
                        kb_name=record.kb_name,
                        source_path=record.local_path,
                        original_name=record.document_name,
                        source_group=record.source_group,
                        created_by=record.uploaded_by,
                        status=TASK_STATUS_FAILED,
                        progress=100,
                        stage="解析超时",
                        message=timeout_msg,
                        result=f"{PROCESSOR_KIND_RAGFLOW}:failed",
                        document_id=_document_info_id(record.id),
                        created_at=self._parse_timestamp(record.created_at),
                        updated_at=self._parse_timestamp(record.updated_at),
                    )
                )
                continue

            task_status, progress, stage = self._parse_task_state_from_ragflow_status(
                status,
                remote_progress=remote_progress if remote_progress is not None else record.parse_progress,
                remote_stage=remote_stage or record.parse_stage,
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
                    result=f"{PROCESSOR_KIND_RAGFLOW}:{status}",
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

        try:
            record = self.store.get_document_by_id_scoped(int(raw_record_id), _ctx_department_id(ctx))
        except ValueError as exc:
            return BackendResult(ok=False, message=str(exc), backend=self.name)
        if not record:
            return BackendResult(ok=True, message="RAGFlow parse task is already gone.", backend=self.name)

        try:
            self._check_kb_access(record.kb_name, ctx, "write")
            _check_record_department(record, ctx)
        except PermissionError as exc:
            return BackendResult(ok=False, message=str(exc), backend=self.name)

        if record.shared_read_only:
            return BackendResult(
                ok=False,
                message=f"共享文档为只读，develop 实例不能停止、删除或重新解析: {record.document_name}",
                backend=self.name,
            )

        if record.processor_kind != PROCESSOR_KIND_RAGFLOW:
            # spreadsheet：索引+归档+store 行原子化清理，避免孤儿
            delete_result = self._delete_pipeline_record(record)
            errors = delete_result.errors
            suffix = f"（部分清理失败: {'; '.join(errors)}）" if errors else ""
            return BackendResult(
                ok=delete_result.ok,
                message=f"{delete_result.message}{suffix}",
                backend=self.name,
            )

        normalized_status = normalize_parse_status(record.status, record.processor_kind)
        if normalized_status not in RAGFLOW_TERMINAL_REMOVABLE_TASK_STATUSES:
            try:
                self._client().stop_parse_documents(record.dataset_id, [record.document_id])
            except Exception as exc:
                if not _is_ragflow_not_owner_error(exc):
                    return BackendResult(ok=False, message=f"Stop RAGFlow parsing failed: {exc}", backend=self.name)
                log(f"RAGFlow stop parsing is not readable for {record.document_id}: {exc}")

        try:
            self._cleanup_remote_document(record.dataset_id, record.document_id)
        except Exception as exc:
            log(f"RAGFlow remote cleanup failed while removing parse task {record.document_id}: {exc}")
        self.archive.remove_record_archive(record)
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
        normalized = normalize_parse_status(status)
        if normalized == TASK_STATUS_COMPLETED:
            return TASK_STATUS_COMPLETED, 100, remote_stage or "解析完成"
        if normalized == TASK_STATUS_FAILED:
            return TASK_STATUS_FAILED, remote_progress if remote_progress is not None else 100, remote_stage or "解析失败"
        if normalized == TASK_STATUS_CANCELLED:
            return TASK_STATUS_CANCELLED, remote_progress if remote_progress is not None else 100, remote_stage or "已停止解析"
        if normalized == TASK_STATUS_RUNNING:
            return TASK_STATUS_RUNNING, remote_progress if remote_progress is not None else 65, remote_stage or "RAGFlow 解析中"
        if normalized == TASK_STATUS_QUEUED:
            return TASK_STATUS_QUEUED, remote_progress if remote_progress is not None else 20, remote_stage or "已上传，等待解析"
        return TASK_STATUS_RUNNING, remote_progress if remote_progress is not None else 40, remote_stage or f"RAGFlow 状态: {status or RAGFLOW_STATUS_UNKNOWN}"

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
            status_note = ""
            raw_error_message = record.error_message or record.ragflow_error
            error_message = (
                ""
                if normalize_parse_status(status) != TASK_STATUS_FAILED
                and _is_ragflow_status_unavailable_text(raw_error_message)
                else raw_error_message
            )
            if record.processor_kind == PROCESSOR_KIND_RAGFLOW:
                # 先用远端真实状态刷新本地,再判定超时:避免本地 status 停在 parsing
                # 但远端其实已解析完成时,被超时误判并删除远端文档(数据丢失)。
                refreshed_status: str | None = None
                try:
                    remote_docs = self._client().list_documents(record.dataset_id, record.document_id)
                    if remote_docs:
                        remote_doc = remote_docs[0]
                        refreshed_status = _normalize_ragflow_status(remote_doc.get("run") or remote_doc.get("status") or status)
                        error_message = _extract_ragflow_error(remote_doc)
                        status = refreshed_status
                        self.store.update_document_status(record.dataset_id, record.document_id, status, error_message)
                except Exception as status_error:
                    if _is_ragflow_not_owner_error(status_error):
                        status_note = _ragflow_status_unavailable_message(record.document_name)
                        log(f"RAGFlow document status is not readable for {record.document_id}: {status_error}")
                    else:
                        log(f"RAGFlow status refresh failed for {record.document_name}: {status_error}")
                # 仅当远端确认仍在解析、且确实超过阈值时才标记超时;远端不可达/状态不明时不删远端。
                if (
                    refreshed_status is not None
                    and normalize_parse_status(refreshed_status) == TASK_STATUS_RUNNING
                    and not record.shared_read_only
                    and _ragflow_parse_timed_out(record)
                ):
                    error_message = self._mark_ragflow_parse_timed_out(record)
                    status = RAGFLOW_STATUS_FAILED

            container_inspection = self.archive.inspect_record_archive(record)
            spreadsheet_profile = (
                self.spreadsheet_indexes.get_document_profile(record)
                if record.processor_kind == PROCESSOR_KIND_SPREADSHEET else None
            )
            ragflow_document_id = record.document_id if record.processor_kind == PROCESSOR_KIND_RAGFLOW else ""
            documents.append(
                DocumentInfo(
                    id=_document_info_id(record.id),
                    name=record.document_name,
                    metadata={
                        "store_id": record.id,
                        "dataset_kind": record.dataset_kind,
                        "dataset_id": record.dataset_id,
                        "ragflow_document_id": ragflow_document_id,
                        "table_document_id": record.document_id if record.processor_kind == PROCESSOR_KIND_SPREADSHEET else "",
                        "original_file_name": record.original_file_name,
                        "source_group": record.source_group,
                        "department_id": record.department_id,
                        "status": status,
                        "content_kind": record.content_kind,
                        "processor_kind": record.processor_kind,
                        "shared_read_only": record.shared_read_only,
                        "local_path": record.local_path,
                        "file_size": record.file_size,
                        "content_hash": record.content_hash,
                        "ragflow_error": error_message,
                        "ragflow_status_note": status_note,
                        "container_inspection": container_inspection,
                        "spreadsheet_profile": spreadsheet_profile,
                    },
                    backend=self.name,
                    processor_kind=record.processor_kind,
                    status=status,
                    local_path=record.local_path,
                    ragflow_document_id=ragflow_document_id,
                    dataset_kind=record.dataset_kind,
                    ragflow_error=error_message,
                    spreadsheet_profile=spreadsheet_profile,
                    container_inspection=container_inspection,
                )
            )
        return documents

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> ParseResult | None:
        self._check_kb_access(kb_name, ctx, "read")
        record = self._get_record_for_document_id(kb_name, document_id, ctx=ctx)
        if not record:
            return None
        _check_record_department(record, ctx)
        if record.processor_kind != PROCESSOR_KIND_RAGFLOW:
            return None
        try:
            raw_chunks = self._client().list_chunks(record.dataset_id, record.document_id)
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
            details = self._client().health()
            details["physical_datasets"] = self._dataset_ids
            details["dataset_names"] = {
                DATASET_GOVERNANCE: src.settings.RAGFLOW_GOVERNANCE_DATASET_NAME,
                DATASET_DESIGN: src.settings.RAGFLOW_DESIGN_DATASET_NAME,
            }
            return BackendHealth(ok=True, details=details, backend=self.name)
        except Exception as exc:
            return BackendHealth(ok=False, message=str(exc), backend=self.name)
