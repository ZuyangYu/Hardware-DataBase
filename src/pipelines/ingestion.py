import os
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator
from typing import Callable

from src.core.logger import error, log
from src.ingestion.container_inspector import ContainerInspection, inspect_container_file
from src.pipelines.document_store import PipelineDocumentRecord, PipelineDocumentStore
from src.pipelines.document_rag.schemas import (
    IngestResult,
    RequestContext,
    TASK_STATUS_DEAD_LETTER,
    normalize_parse_status,
)
from src.pipelines.registry import (
    CONTENT_KIND_CIRCUIT,
    CONTENT_KIND_DOCUMENT,
    CONTENT_KIND_EXTERNAL_CONVERSATION,
    CONTENT_KIND_SPREADSHEET,
    DATASET_CIRCUIT,
    DATASET_CONVERSATION,
    PIPELINE_REGISTRY,
    PROCESSOR_KIND_CIRCUIT,
    PROCESSOR_KIND_EXTERNAL_CONVERSATION,
    PROCESSOR_KIND_RAGFLOW,
    PROCESSOR_KIND_SPREADSHEET,
    PipelineRoute,
    PipelineSpec,
    route_file,
)
from src.pipelines.spreadsheet.pipeline import SpreadsheetIndexRequest
from src.services.document_archive import DocumentArchiveManager
from src.services.document_routing import (
    RAGFLOW_STATUS_PARSING,
    RAGFLOW_STATUS_DELETED,
    RAGFLOW_STATUS_FAILED,
    TABLE_STATUS_INDEXED,
    TABLE_STATUS_DEGRADED,
    TABLE_STATUS_ARCHIVED,
    TABLE_STATUS_PROCESSING,
)


@dataclass
class _IngestLockEntry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    active_count: int = 0


_INGEST_LOCKS: dict[str, _IngestLockEntry] = {}
_INGEST_LOCKS_GUARD = threading.Lock()

# Single-flight guard for background LLM post-processing per conversation id.
_EXTERNAL_CONVERSATION_LLM_LOCK = threading.Lock()
_EXTERNAL_CONVERSATION_LLM_INFLIGHT: set[str] = set()


def _noop_audit(*args, **kwargs):
    return None


@contextmanager
def _ingest_lock(scope: "IngestionScope", content_hash: str) -> Iterator[threading.RLock]:
    key = f"{scope.department_id}:{scope.kb_name}:{content_hash}"
    with _INGEST_LOCKS_GUARD:
        entry = _INGEST_LOCKS.get(key)
        if entry is None:
            entry = _IngestLockEntry()
            _INGEST_LOCKS[key] = entry
        entry.active_count += 1
    try:
        with entry.lock:
            yield entry.lock
    finally:
        with _INGEST_LOCKS_GUARD:
            entry.active_count -= 1
            if entry.active_count == 0 and _INGEST_LOCKS.get(key) is entry:
                del _INGEST_LOCKS[key]


@dataclass
class IngestionScope:
    kb_name: str
    department_id: str
    kb_id: int | None = None
    uploaded_by: str = ""
    source_group: str | None = None
    ctx: RequestContext | None = None


@dataclass
class ArchivedFile:
    original_path: str
    archived_path: str
    filename: str
    source_group: str
    relative_local_path: str
    file_size: int
    content_hash: str
    inspection: ContainerInspection


@dataclass
class HandlerResult:
    success: bool
    message: str
    document_id: str = ""
    record_id: int | None = None
    status: str = ""
    uploaded_to_remote: bool = False
    warnings: list[str] = field(default_factory=list)
    audit_action: str = ""
    audit_metadata: dict = field(default_factory=dict)
    preserve_failed_record: bool = False


@dataclass
class HandlerDeleteResult:
    ok: bool
    message: str
    errors: list[str] = field(default_factory=list)
    audit_action: str = ""


@dataclass
class HandlerProcessResult:
    ok: bool
    status: str
    message: str
    progress: int = 100
    audit_action: str = ""
    audit_metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class PipelineHandler(ABC):
    spec: PipelineSpec

    @abstractmethod
    def existing_record_dataset_kind(self, default_dataset_kind: str) -> str:
        """Return the dataset partition used for duplicate detection."""

    def can_reuse_existing(self, record: PipelineDocumentRecord) -> bool:
        return True

    def reuse_message(self, record: PipelineDocumentRecord) -> str:
        return f"[success] Already processed by {self.spec.key}: {record.document_name}"

    def on_stale_existing(self, record: PipelineDocumentRecord):
        return None

    @abstractmethod
    def submit(
        self,
        scope: IngestionScope,
        archived: ArchivedFile,
        default_dataset_kind: str,
        default_dataset_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> HandlerResult:
        """Persist pipeline-specific mapping and submit parse/index work."""

    def rollback(self, result: HandlerResult, scope: IngestionScope):
        return None

    def delete_record(self, record: PipelineDocumentRecord, archive: DocumentArchiveManager) -> HandlerDeleteResult:
        return HandlerDeleteResult(
            ok=False,
            message=f"Pipeline {self.spec.key} does not implement document deletion.",
        )

    def process_record(self, record: PipelineDocumentRecord, archive: DocumentArchiveManager) -> HandlerProcessResult:
        return HandlerProcessResult(
            ok=False,
            status=record.status,
            message=f"Pipeline {self.spec.key} does not implement background processing.",
        )

    def cleanup_failed_process(self, record: PipelineDocumentRecord):
        return None


class IngestionOrchestrator:
    def __init__(
        self,
        *,
        backend_name: str,
        store: PipelineDocumentStore,
        archive: DocumentArchiveManager,
        handlers: dict[str, PipelineHandler],
        audit_callback,
        content_hash_callback,
        remote_document_exists_callback,
    ):
        self.backend_name = backend_name
        self.store = store
        self.archive = archive
        self.handlers = handlers
        self.audit = audit_callback
        self.content_hash = content_hash_callback
        self.remote_document_exists = remote_document_exists_callback
        if not callable(self.audit):
            error("IngestionOrchestrator audit_callback is not callable; audit events will be skipped.")
            self.audit = _noop_audit

    def upload_files(
        self,
        files: list[str],
        scope: IngestionScope,
        default_dataset_kind: str,
        default_dataset_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> IngestResult:
        if not files:
            return IngestResult(success_count=0, total_count=0, messages=["No files selected"], backend=self.backend_name)

        messages: list[str] = []
        success_count = 0
        failed_count = 0
        skipped_count = 0
        table_task_touched = False

        for file_path in files:
            filename = os.path.basename(file_path)
            route = route_file(file_path, source_group=scope.source_group)
            handler = self._handler_for_route(route)
            archived: ArchivedFile | None = None
            handler_result: HandlerResult | None = None
            try:
                extension = os.path.splitext(filename)[1].lower() or "(no extension)"
                if route.rejected:
                    message = self._rejected_message(filename, route)
                    messages.append(message)
                    skipped_count += 1
                    self._audit_unsupported(scope, filename, message, {
                        "extension": extension,
                        "source_group": scope.source_group,
                        "reason": route.reason or "file_type_rejected",
                        "rejected_by": route.rejected_by.key if route.rejected_by else "",
                    })
                    if progress_callback:
                        progress_callback(0, f"{filename}: unsupported file variant")
                    continue

                if handler is None:
                    message = f"[skipped] {filename}: unsupported file type {extension}; no pipeline is configured yet."
                    messages.append(message)
                    skipped_count += 1
                    self._audit_unsupported(scope, filename, message, {
                        "extension": extension,
                        "source_group": scope.source_group,
                        "configured_pipelines": PIPELINE_REGISTRY.configured_extensions(),
                    })
                    if progress_callback:
                        progress_callback(0, f"{filename}: no pipeline configured")
                    continue

                content_hash = self.content_hash(file_path)
                with _ingest_lock(scope, content_hash):
                    record_dataset_kind = handler.existing_record_dataset_kind(default_dataset_kind)
                    existing = self.store.find_by_hash(
                        scope.kb_name,
                        record_dataset_kind,
                        content_hash,
                        scope.department_id,
                    )
                    if existing and existing.status not in {RAGFLOW_STATUS_FAILED, RAGFLOW_STATUS_DELETED}:
                        reusable = self._existing_record_is_reusable(existing, handler)
                        if reusable is None:
                            # Remote status could not be verified; keep the old
                            # record and remote document untouched rather than
                            # risk destroying them on inconclusive evidence.
                            raise RuntimeError(
                                f"Cannot verify existing document {existing.document_name}; "
                                "keeping the current mapping. Please retry later."
                            )
                        if reusable:
                            messages.append(handler.reuse_message(existing))
                            success_count += 1
                            table_task_touched = True
                            continue
                        log(f"Mapping for {existing.document_name} is stale; re-processing.")
                        handler.on_stale_existing(existing)
                        self.archive.remove_record_archive(existing)
                        self.store.delete_document_by_id(existing.id)

                    archived = self._archive_file(file_path, scope, content_hash)
                    handler_result = handler.submit(
                        scope,
                        archived,
                        default_dataset_kind,
                        default_dataset_id,
                        progress_callback=progress_callback,
                    )
                    if route.spec and route.spec.processor_kind == PROCESSOR_KIND_SPREADSHEET:
                        table_task_touched = True
                if handler_result.audit_action:
                    self.audit(
                        handler_result.audit_action,
                        scope.ctx,
                        kb_name=scope.kb_name,
                        target_type="document",
                        target_id=archived.filename,
                        metadata=handler_result.audit_metadata,
                    )
                if handler_result.success:
                    messages.append(handler_result.message)
                    messages.extend(f"[warning] {warning}" for warning in handler_result.warnings)
                    success_count += 1
                else:
                    if not handler_result.preserve_failed_record:
                        self._cleanup_failed_submission(handler, handler_result, scope, archived)
                    failed_count += 1
                    messages.append(handler_result.message)
            except Exception as exc:
                failed_count += 1
                if handler and handler_result:
                    try:
                        handler.rollback(handler_result, scope)
                    except Exception as cleanup_error:
                        error(f"Failed to rollback {filename}: {cleanup_error}")
                elif handler_result and handler_result.record_id:
                    self.store.delete_document_by_id(handler_result.record_id)
                if archived and os.path.exists(archived.archived_path):
                    try:
                        os.remove(archived.archived_path)
                    except OSError as cleanup_error:
                        error(f"Failed to remove archived source file {archived.archived_path}: {cleanup_error}")
                error(f"Ingest failed for {filename}: {exc}")
                self.audit(
                    "pipeline_upload_failed",
                    scope.ctx,
                    kb_name=scope.kb_name,
                    target_type="document",
                    target_id=filename,
                    success=False,
                    error_message=str(exc),
                    metadata={
                        "source_group": scope.source_group,
                        "local_path": archived.archived_path if archived else "",
                        "processor_kind": route.spec.processor_kind if route.spec else "",
                    },
                )
                messages.append(f"[failed] {filename}: {exc}")

        if table_task_touched:
            worker_warning = self._worker_liveness_warning()
            if worker_warning:
                messages.append(worker_warning)

        return IngestResult(
            success_count=success_count,
            total_count=len(files),
            messages=messages,
            backend=self.backend_name,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

    def _worker_liveness_warning(self) -> str | None:
        """H6: 表格任务提交后探测 worker 进程注册表；缺位时给用户可见告警。"""
        from src.settings import OBS_WORKER_STALE_SECONDS

        try:
            from src.observability.worker_registry import list_workers

            alive = list_workers(stale_after_seconds=int(OBS_WORKER_STALE_SECONDS) * 3)
        except Exception:
            return None
        if alive:
            return None
        return (
            "[warning] 未检测到活跃的解析 worker（注册表心跳已过期）。"
            "表格任务会停留在队列，请确认已启动 hardware-database-worker。"
        )

    def _cleanup_failed_submission(
        self,
        handler: PipelineHandler | None,
        handler_result: HandlerResult | None,
        scope: IngestionScope,
        archived: ArchivedFile | None,
    ):
        if handler and handler_result:
            try:
                handler.rollback(handler_result, scope)
            except Exception as cleanup_error:
                error(f"Failed to rollback failed submission: {cleanup_error}")
        elif handler_result and handler_result.record_id:
            try:
                self.store.delete_document_by_id(handler_result.record_id)
            except Exception as cleanup_error:
                error(f"Failed to delete failed submission record {handler_result.record_id}: {cleanup_error}")

        if archived and os.path.exists(archived.archived_path):
            try:
                os.remove(archived.archived_path)
            except OSError as cleanup_error:
                error(f"Failed to remove archived source file {archived.archived_path}: {cleanup_error}")

    def _archive_file(self, file_path: str, scope: IngestionScope, content_hash: str) -> ArchivedFile:
        archived_path, filename, archived_group = self.archive.archive_source_file(
            scope.kb_name,
            file_path,
            scope.source_group,
            department_id=scope.department_id,
        )
        relative_local_path = os.path.relpath(
            archived_path,
            self.archive.kb_path(scope.kb_name, department_id=scope.department_id),
        )
        inspection = inspect_container_file(archived_path)
        return ArchivedFile(
            original_path=file_path,
            archived_path=archived_path,
            filename=filename,
            source_group=archived_group,
            relative_local_path=relative_local_path,
            file_size=os.path.getsize(archived_path),
            content_hash=content_hash,
            inspection=inspection,
        )

    def _existing_record_is_reusable(self, record: PipelineDocumentRecord, handler: PipelineHandler) -> bool | None:
        """Tri-state reuse decision: True (reuse), False (stale, safe to rebuild), None (undetermined)."""
        if not self.archive.record_archive_exists(record):
            return False
        if record.processor_kind == PROCESSOR_KIND_RAGFLOW:
            remote_exists = self.remote_document_exists(record)
            if remote_exists is None:
                return None
            if not remote_exists:
                return False
        return handler.can_reuse_existing(record)

    def _handler_for_route(self, route: PipelineRoute) -> PipelineHandler | None:
        if not route.spec:
            return None
        return self.handlers.get(route.spec.processor_kind)

    def _rejected_message(self, filename: str, route: PipelineRoute) -> str:
        if route.rejected_by and route.rejected_by.processor_kind == PROCESSOR_KIND_SPREADSHEET:
            return (
                f"[skipped] {filename}: 当前不支持 .xls 归档或解析，"
                "请在 Excel 中另存为 .xlsx 后重新上传。"
            )
        return f"[skipped] {filename}: unsupported file variant."

    def _audit_unsupported(self, scope: IngestionScope, filename: str, message: str, metadata: dict):
        self.audit(
            "upload_unsupported_file",
            scope.ctx,
            kb_name=scope.kb_name,
            target_type="document",
            target_id=filename,
            success=False,
            error_message=message,
            metadata=metadata,
        )


class RAGFlowDocumentHandler(PipelineHandler):
    def __init__(
        self,
        *,
        spec: PipelineSpec,
        store: PipelineDocumentStore,
        submit_callback,
        cleanup_remote_callback,
        delete_remote_callback=None,
    ):
        self.spec = spec
        self.store = store
        self.submit_remote = submit_callback
        self.cleanup_remote = cleanup_remote_callback
        self.delete_remote = delete_remote_callback or cleanup_remote_callback

    def existing_record_dataset_kind(self, default_dataset_kind: str) -> str:
        return default_dataset_kind

    def reuse_message(self, record: PipelineDocumentRecord) -> str:
        return f"[success] Already submitted to RAGFlow: {record.document_name}"

    def on_stale_existing(self, record: PipelineDocumentRecord):
        self.cleanup_remote(record.dataset_id, record.document_id)

    def submit(
        self,
        scope: IngestionScope,
        archived: ArchivedFile,
        default_dataset_kind: str,
        default_dataset_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> HandlerResult:
        submission = self.submit_remote(
            default_dataset_id,
            scope.kb_name,
            archived.filename,
            archived.archived_path,
            archived.source_group,
            ctx=scope.ctx,
            progress_callback=progress_callback,
        )
        try:
            self.store.upsert_document(
                kb_name=scope.kb_name,
                document_name=archived.filename,
                dataset_kind=default_dataset_kind,
                dataset_id=default_dataset_id,
                document_id=submission.document_id,
                source_group=archived.source_group,
                department_id=scope.department_id,
                uploaded_by=scope.uploaded_by,
                kb_id=scope.kb_id,
                status=RAGFLOW_STATUS_PARSING,
                original_file_name=os.path.basename(archived.original_path),
                local_path=archived.relative_local_path,
                file_size=archived.file_size,
                content_hash=archived.content_hash,
                upload_status=RAGFLOW_STATUS_PARSING,
                content_kind=CONTENT_KIND_DOCUMENT,
                processor_kind=PROCESSOR_KIND_RAGFLOW,
                parse_progress=5,
                parse_stage="已提交到 RAGFlow 解析队列",
            )
        except Exception:
            self._cleanup_submitted_remote(default_dataset_id, submission.document_id)
            raise
        record = self.store.get_document(
            scope.kb_name,
            archived.filename,
            default_dataset_kind,
            department_id=scope.department_id,
        )
        warning = archived.inspection.to_warning_message()
        return HandlerResult(
            success=True,
            message=f"[success] 已提交 RAGFlow 解析任务: {archived.filename}",
            document_id=submission.document_id,
            record_id=record.id if record else None,
            status=RAGFLOW_STATUS_PARSING,
            uploaded_to_remote=True,
            warnings=[warning] if warning else [],
            audit_action="ragflow_upload_submitted",
            audit_metadata={
                "store_id": record.id if record else None,
                "kb_id": scope.kb_id,
                "dataset_kind": default_dataset_kind,
                "dataset_id": default_dataset_id,
                "ragflow_document_id": submission.document_id,
                "source_group": archived.source_group,
                "local_path": archived.relative_local_path,
                "content_hash": archived.content_hash,
                "container_inspection": archived.inspection.to_metadata(),
            },
        )

    def _cleanup_submitted_remote(self, dataset_id: str, document_id: str):
        try:
            self.cleanup_remote(dataset_id, document_id)
        except Exception as exc:
            error(f"Failed to cleanup submitted RAGFlow document {document_id}: {exc}")
        try:
            self.store.delete_document_by_remote_id(dataset_id, document_id)
        except Exception as exc:
            error(f"Failed to cleanup local RAGFlow mapping {document_id}: {exc}")

    def rollback(self, result: HandlerResult, scope: IngestionScope):
        if result.uploaded_to_remote and result.document_id:
            # Dataset id is available from audit metadata for the current backend.
            dataset_id = str(result.audit_metadata.get("dataset_id") or "")
            self.cleanup_remote(dataset_id, result.document_id)
            self.store.delete_document_by_remote_id(dataset_id, result.document_id)
        elif result.record_id:
            self.store.delete_document_by_id(result.record_id)

    def delete_record(self, record: PipelineDocumentRecord, archive: DocumentArchiveManager) -> HandlerDeleteResult:
        self.delete_remote(record.dataset_id, record.document_id)
        archive.remove_record_archive(record)
        self.store.delete_document_by_id(record.id)
        return HandlerDeleteResult(
            ok=True,
            message=f"Deleted RAGFlow document: {record.document_name}",
            audit_action="ragflow_delete_document",
        )


class CircuitPipelineHandler(PipelineHandler):
    def __init__(self, *, spec: PipelineSpec, store: PipelineDocumentStore, circuit_index=None):
        self.spec = spec
        self.store = store
        if circuit_index is None:
            from src.circuit.index_service import CircuitIndexService

            circuit_index = CircuitIndexService()
        self.circuit_index = circuit_index

    def existing_record_dataset_kind(self, default_dataset_kind: str) -> str:
        return self.spec.dataset_kind or DATASET_CIRCUIT

    def reuse_message(self, record: PipelineDocumentRecord) -> str:
        return f"[success] Circuit design already archived: {record.document_name}"

    def submit(
        self,
        scope: IngestionScope,
        archived: ArchivedFile,
        default_dataset_kind: str,
        default_dataset_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> HandlerResult:
        dataset_kind = self.spec.dataset_kind or DATASET_CIRCUIT
        document_id = f"circuit:{archived.content_hash[:16]}"
        self.store.upsert_document(
            kb_name=scope.kb_name,
            document_name=archived.filename,
            dataset_kind=dataset_kind,
            dataset_id="",
            document_id=document_id,
            source_group=archived.source_group,
            department_id=scope.department_id,
            uploaded_by=scope.uploaded_by,
            kb_id=scope.kb_id,
            status=TABLE_STATUS_ARCHIVED,
            original_file_name=os.path.basename(archived.original_path),
            local_path=archived.relative_local_path,
            file_size=archived.file_size,
            content_hash=archived.content_hash,
            upload_status=TABLE_STATUS_ARCHIVED,
            content_kind=CONTENT_KIND_CIRCUIT,
            processor_kind=PROCESSOR_KIND_CIRCUIT,
            parse_progress=5,
            parse_stage="Archived; waiting for circuit indexing",
        )
        record = self.store.get_document(
            scope.kb_name,
            archived.filename,
            dataset_kind,
            department_id=scope.department_id,
        )
        record_id = record.id if record else None
        warnings: list[str] = []
        warning = archived.inspection.to_warning_message()
        if warning:
            warnings.append(warning)
        try:
            index_result = self.circuit_index.index_file(
                kb_name=scope.kb_name,
                record_id=record_id,
                file_path=archived.archived_path,
                original_name=archived.filename,
                department_id=scope.department_id,
                uploaded_by=scope.uploaded_by,
            )
            warnings.extend(getattr(index_result, "warnings", []) or [])
            index_status = str(getattr(index_result, "status", "") or TABLE_STATUS_INDEXED)
            if index_status not in {TABLE_STATUS_INDEXED, TABLE_STATUS_DEGRADED}:
                index_status = TABLE_STATUS_DEGRADED
            index_message = str(
                getattr(index_result, "message", "") or "Circuit design indexed"
            )
            index_stats = dict(getattr(index_result, "stats", {}) or {})
            if record_id:
                self.store.update_document_progress_by_id(
                    record_id,
                    100,
                    index_message,
                    status=index_status,
                    error_message="",
                )
            if progress_callback:
                progress_callback(
                    100,
                    f"{archived.filename}: circuit design {index_status}; {index_message}",
                )
            audit_action = (
                "circuit_upload_degraded"
                if index_status == TABLE_STATUS_DEGRADED
                else "circuit_upload_indexed"
            )
            handler_message = (
                f"[warning] {index_message}: {archived.filename}"
                if index_status == TABLE_STATUS_DEGRADED
                else f"[success] Indexed circuit design file: {archived.filename}"
            )
            return HandlerResult(
                success=True,
                message=handler_message,
                document_id=document_id,
                record_id=record_id,
                status=index_status,
                warnings=warnings,
                audit_action=audit_action,
                audit_metadata={
                    "store_id": record_id,
                    "kb_id": scope.kb_id,
                    "dataset_kind": dataset_kind,
                    "content_kind": CONTENT_KIND_CIRCUIT,
                    "processor_kind": PROCESSOR_KIND_CIRCUIT,
                    "source_group": archived.source_group,
                    "local_path": archived.relative_local_path,
                    "content_hash": archived.content_hash,
                    "status": index_status,
                    "container_inspection": archived.inspection.to_metadata(),
                    "circuit_index_status": index_status,
                    "circuit_index_message": index_message,
                    "circuit_index_warnings": list(getattr(index_result, "warnings", []) or []),
                    "circuit_stats": index_stats,
                    "circuit_design_id": getattr(index_result, "design_id", ""),
                },
            )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            if record_id:
                self.store.update_document_progress_by_id(
                    record_id,
                    100,
                    "Circuit design indexing failed",
                    status=RAGFLOW_STATUS_FAILED,
                    error_message=error_message,
                )
            if progress_callback:
                progress_callback(100, f"{archived.filename}: circuit design indexing failed")
            return HandlerResult(
                success=False,
                message=f"[failed] Circuit design indexing failed: {archived.filename}: {exc}",
                document_id=document_id,
                record_id=record_id,
                status=RAGFLOW_STATUS_FAILED,
                warnings=warnings,
                audit_action="circuit_upload_failed",
                audit_metadata={
                    "store_id": record_id,
                    "kb_id": scope.kb_id,
                    "dataset_kind": dataset_kind,
                    "content_kind": CONTENT_KIND_CIRCUIT,
                    "processor_kind": PROCESSOR_KIND_CIRCUIT,
                    "source_group": archived.source_group,
                    "local_path": archived.relative_local_path,
                    "content_hash": archived.content_hash,
                    "status": RAGFLOW_STATUS_FAILED,
                    "error_message": error_message,
                    "container_inspection": archived.inspection.to_metadata(),
                },
                preserve_failed_record=True,
            )

    def rollback(self, result: HandlerResult, scope: IngestionScope):
        if result.record_id:
            self.store.delete_document_by_id(result.record_id)

    def delete_record(self, record: PipelineDocumentRecord, archive: DocumentArchiveManager) -> HandlerDeleteResult:
        try:
            self.circuit_index.delete_record(record)
        except Exception as exc:
            return HandlerDeleteResult(
                ok=False,
                message=f"Circuit index cleanup failed for {record.document_name}: {exc}",
                errors=[f"circuit index: {exc}"],
                audit_action="circuit_delete_document_failed",
            )
        archive.remove_record_archive(record)
        self.store.delete_document_by_id(record.id)
        return HandlerDeleteResult(
            ok=True,
            message=f"Deleted circuit design archive: {record.document_name}",
            audit_action="circuit_delete_document",
        )

class ExternalConversationHandler(PipelineHandler):
    """Synchronous local pipeline for 外部数据 txt/markdown conversation records.

    Write order per design contract C4: archive (done by the orchestrator)
    -> conversation.json (source of truth) -> index.db -> ledger upsert.
    Deletion runs in reverse. Terminal status reuses the existing local
    ``indexed`` enum; failures use ``failed``.
    """

    def __init__(
        self,
        *,
        spec: PipelineSpec,
        store: PipelineDocumentStore,
        conversation_store=None,
        conversation_indexes=None,
        vector_index=None,
        parser=None,
        llm_client=None,
    ):
        self.spec = spec
        self.store = store
        if conversation_store is None:
            from src.external_conversations.store import ExternalConversationStore

            conversation_store = ExternalConversationStore()
        self.conversation_store = conversation_store
        if conversation_indexes is None:
            from src.external_conversations.query_engine import ExternalConversationQueryEngine

            conversation_indexes = ExternalConversationQueryEngine(root=self.conversation_store.root)
        self.conversation_indexes = conversation_indexes
        if vector_index is None:
            from src.external_conversations.vector_index import default_external_conversation_vector_index

            vector_index = default_external_conversation_vector_index
        self.vector_index = vector_index
        if parser is None:
            from src.external_conversations.parsers import parse_external_conversation

            parser = parse_external_conversation
        self._parser = parser
        self._llm_client = llm_client

    def _postprocess_llm(self, conversation):
        """Background LLM refinement: structure inference for marker-less text,
        then summary extraction. Runs off the upload critical path; persists
        results back to json + index. Strictly fail-open."""
        try:
            from datetime import date

            from src.external_conversations import llm_structure

            if self._llm_client is None:
                from src.core.llm_client import LLMClient

                self._llm_client = LLMClient()

            changed = False
            if not conversation.turns and conversation.blocks:
                inferred = llm_structure.infer_structure(
                    "\n\n".join(conversation.blocks),
                    llm_client=self._llm_client,
                )
                if inferred:
                    conversation.turns = inferred["turns"]
                    conversation.blocks = []
                    if inferred.get("title"):
                        conversation.title = inferred["title"]
                    changed = True
                    # turns changed -> refresh the keyword index too
                    self.conversation_indexes.index_conversation(conversation)

            body = "\n".join(t.content for t in conversation.turns) or "\n".join(conversation.blocks)
            result = llm_structure.summarize_content(body, llm_client=self._llm_client)
            if result:
                conversation.summary = result["summary"]
                conversation.key_points = result["key_points"]
                conversation.summary_generated_at = date.today().isoformat()
                changed = True

            if changed or (conversation.summary and not conversation.summary_generated_at):
                self.conversation_store.save(
                    conversation,
                    raw_bytes=None,
                    raw_ext=os.path.splitext(conversation.source_file)[1] or ".md",
                )
        except Exception as exc:
            from src.core.logger import warn

            warn(f"External conversation LLM post-processing skipped: {exc}")

    def _spawn_postprocess(self, conversation):
        """Fire-and-forget background refinement, single-flight per id."""
        with _EXTERNAL_CONVERSATION_LLM_LOCK:
            if conversation.conversation_id in _EXTERNAL_CONVERSATION_LLM_INFLIGHT:
                return
            _EXTERNAL_CONVERSATION_LLM_INFLIGHT.add(conversation.conversation_id)

        def _run():
            try:
                self._postprocess_llm(conversation)
            finally:
                with _EXTERNAL_CONVERSATION_LLM_LOCK:
                    _EXTERNAL_CONVERSATION_LLM_INFLIGHT.discard(conversation.conversation_id)

        threading.Thread(target=_run, name="ext-conv-postprocess", daemon=True).start()

    def existing_record_dataset_kind(self, default_dataset_kind: str) -> str:
        return self.spec.dataset_kind or DATASET_CONVERSATION

    def reuse_message(self, record: PipelineDocumentRecord) -> str:
        return f"[success] Conversation already indexed: {record.document_name}"

    def submit(
        self,
        scope: IngestionScope,
        archived: ArchivedFile,
        default_dataset_kind: str,
        default_dataset_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> HandlerResult:
        dataset_kind = self.spec.dataset_kind or DATASET_CONVERSATION
        document_id = f"conversation:{archived.content_hash[:16]}"
        self.store.upsert_document(
            kb_name=scope.kb_name,
            document_name=archived.filename,
            dataset_kind=dataset_kind,
            dataset_id="",
            document_id=document_id,
            source_group=archived.source_group,
            department_id=scope.department_id,
            uploaded_by=scope.uploaded_by,
            kb_id=scope.kb_id,
            status=RAGFLOW_STATUS_PARSING,
            original_file_name=os.path.basename(archived.original_path),
            local_path=archived.relative_local_path,
            file_size=archived.file_size,
            content_hash=archived.content_hash,
            upload_status=TABLE_STATUS_ARCHIVED,
            content_kind=CONTENT_KIND_EXTERNAL_CONVERSATION,
            processor_kind=PROCESSOR_KIND_EXTERNAL_CONVERSATION,
            parse_progress=10,
            parse_stage="解析外部对话记录",
        )
        record = self.store.get_document(
            scope.kb_name,
            archived.filename,
            dataset_kind,
            department_id=scope.department_id,
        )
        record_id = record.id if record else None
        warnings: list[str] = []
        warning = archived.inspection.to_warning_message()
        if warning:
            warnings.append(warning)
        try:
            if progress_callback:
                progress_callback(40, f"{archived.filename}: 解析对话结构")
            with open(archived.archived_path, "rb") as f:
                raw_bytes = f.read()
            conversation = self._parser(
                archived.archived_path,
                archived.filename,
                scope.kb_name,
                department_id=scope.department_id,
                kb_id=scope.kb_id,
                origin="upload",
                source_group=archived.source_group,
            )
            if progress_callback:
                progress_callback(70, f"{archived.filename}: 写入存储与索引")
            self.conversation_store.save(conversation, raw_bytes=raw_bytes, raw_ext=os.path.splitext(archived.filename)[1])
            self.conversation_indexes.index_conversation(conversation)
            # vector supplement is a no-op without an embed model; never fatal
            try:
                self.vector_index.reindex_conversation(conversation)
            except Exception as exc:
                from src.core.logger import warn

                warn(f"External conversation vector indexing skipped: {exc}")
            # LLM refinement (structure inference + summary) runs in the
            # background so the upload request returns immediately.
            self._spawn_postprocess(conversation)
            turn_count = len(conversation.turns) or len(conversation.blocks)
            message = f"[success] 已解析并索引外部对话: {archived.filename}（{turn_count} 条）"
            if record_id:
                # Re-point the ledger row at the real conversation_id so later
                # deletion can map document -> stored directory 1:1.
                self.store.upsert_document(
                    kb_name=scope.kb_name,
                    document_name=archived.filename,
                    dataset_kind=dataset_kind,
                    dataset_id="",
                    document_id=conversation.conversation_id,
                    source_group=archived.source_group,
                    department_id=scope.department_id,
                    uploaded_by=scope.uploaded_by,
                    kb_id=scope.kb_id,
                    status=TABLE_STATUS_INDEXED,
                    original_file_name=os.path.basename(archived.original_path),
                    local_path=archived.relative_local_path,
                    file_size=archived.file_size,
                    content_hash=archived.content_hash,
                    upload_status=TABLE_STATUS_ARCHIVED,
                    content_kind=CONTENT_KIND_EXTERNAL_CONVERSATION,
                    processor_kind=PROCESSOR_KIND_EXTERNAL_CONVERSATION,
                    parse_progress=100,
                    parse_stage=message,
                )
                refreshed = self.store.get_document(
                    scope.kb_name,
                    archived.filename,
                    dataset_kind,
                    department_id=scope.department_id,
                )
                record_id = refreshed.id if refreshed else record_id
            if progress_callback:
                progress_callback(100, message)
            return HandlerResult(
                success=True,
                message=message,
                document_id=document_id,
                record_id=record_id,
                status=TABLE_STATUS_INDEXED,
                warnings=warnings,
                audit_action="external_conversation_indexed",
                audit_metadata={
                    "store_id": record_id,
                    "kb_id": scope.kb_id,
                    "dataset_kind": dataset_kind,
                    "content_kind": CONTENT_KIND_EXTERNAL_CONVERSATION,
                    "processor_kind": PROCESSOR_KIND_EXTERNAL_CONVERSATION,
                    "source_group": archived.source_group,
                    "local_path": archived.relative_local_path,
                    "content_hash": archived.content_hash,
                    "status": TABLE_STATUS_INDEXED,
                    "conversation_id": conversation.conversation_id,
                    "turn_count": len(conversation.turns),
                    "block_count": len(conversation.blocks),
                    "container_inspection": archived.inspection.to_metadata(),
                },
            )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            if record_id:
                self.store.update_document_progress_by_id(
                    record_id,
                    100,
                    "外部对话解析失败",
                    status=RAGFLOW_STATUS_FAILED,
                    error_message=error_message,
                )
            if progress_callback:
                progress_callback(100, f"{archived.filename}: 外部对话解析失败")
            return HandlerResult(
                success=False,
                message=f"[failed] 外部对话解析失败: {archived.filename}: {exc}",
                document_id=document_id,
                record_id=record_id,
                status=RAGFLOW_STATUS_FAILED,
                warnings=warnings,
                audit_action="external_conversation_failed",
                audit_metadata={
                    "store_id": record_id,
                    "kb_id": scope.kb_id,
                    "dataset_kind": dataset_kind,
                    "content_kind": CONTENT_KIND_EXTERNAL_CONVERSATION,
                    "processor_kind": PROCESSOR_KIND_EXTERNAL_CONVERSATION,
                    "source_group": archived.source_group,
                    "status": RAGFLOW_STATUS_FAILED,
                    "error_message": error_message,
                },
                preserve_failed_record=True,
            )

    def rollback(self, result: HandlerResult, scope: IngestionScope):
        if result.record_id:
            self.store.delete_document_by_id(result.record_id)

    def delete_record(self, record: PipelineDocumentRecord, archive: DocumentArchiveManager) -> HandlerDeleteResult:
        errors: list[str] = []
        # reverse write order: index first, then store directory, then
        # archive, then the ledger row.
        try:
            self.vector_index.delete_conversation(record.kb_name, record.document_id, record.department_id)
        except Exception as exc:
            errors.append(f"vector: {exc}")
        try:
            self.conversation_indexes.delete_conversation(record.department_id, record.kb_name, record.document_id)
        except Exception as exc:
            errors.append(f"index: {exc}")
        try:
            # document_id equals the parser conversation_id after submit's re-point
            self.conversation_store.delete_conversation(record.department_id, record.kb_name, record.document_id)
        except Exception as exc:
            errors.append(f"store: {exc}")
        try:
            archive.remove_record_archive(record)
        except Exception as exc:
            errors.append(f"archive: {exc}")
        if errors:
            return HandlerDeleteResult(
                ok=False,
                message=f"External conversation cleanup failed for {record.document_name}",
                errors=errors,
                audit_action="external_conversation_delete_failed",
            )
        self.store.delete_document_by_id(record.id)
        return HandlerDeleteResult(
            ok=True,
            message=f"Deleted external conversation: {record.document_name}",
            audit_action="external_conversation_delete_document",
        )

class SpreadsheetPipelineHandler(PipelineHandler):
    def __init__(
        self,
        *,
        spec: PipelineSpec,
        store: PipelineDocumentStore,
        ensure_worker_callback,
        parse_index_callback=None,
        delete_index_callback=None,
    ):
        self.spec = spec
        self.store = store
        self.ensure_worker = ensure_worker_callback
        self.parse_index = parse_index_callback
        self.delete_index = delete_index_callback

    def existing_record_dataset_kind(self, default_dataset_kind: str) -> str:
        return self.spec.dataset_kind or default_dataset_kind

    def on_stale_existing(self, record: PipelineDocumentRecord):
        # The orchestrator is about to delete this record's store row and archive,
        # then re-submit (which creates a NEW record_id). Drop the spreadsheet's own
        # index rows for the old record_id first, so they do not linger as orphans.
        # delete_index targets record_id and is a no-op when nothing was indexed.
        if self.delete_index:
            try:
                self.delete_index(record)
            except Exception as exc:
                error(f"Failed to clean stale spreadsheet index for record {record.id}: {exc}")

    def can_reuse_existing(self, record: PipelineDocumentRecord) -> bool:
        if record.status == TASK_STATUS_DEAD_LETTER:
            # Permanently failed: the worker never reclaims a dead-letter row
            # (retry_count already exhausted), so re-queueing is futile. Return
            # False so the orchestrator tears the stale mapping down and rebuilds
            # it fresh via submit() with a new, reset record.
            return False
        if normalize_parse_status(record.status) in {
            "cancelled",
            "failed",
            "queued",
            TABLE_STATUS_ARCHIVED,
            "unsupported",
        }:
            self.store.update_document_progress_by_id(
                record.id,
                5,
                "已归档，等待表格结构化解析",
                status=TABLE_STATUS_ARCHIVED,
                error_message="",
            )
            self.ensure_worker()
        return True

    def reuse_message(self, record: PipelineDocumentRecord) -> str:
        if record.status != TABLE_STATUS_INDEXED:
            return f"[success] 表格解析任务已存在: {record.document_name}"
        return f"[success] Already processed by spreadsheet pipeline: {record.document_name}"

    def submit(
        self,
        scope: IngestionScope,
        archived: ArchivedFile,
        default_dataset_kind: str,
        default_dataset_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> HandlerResult:
        dataset_kind = self.spec.dataset_kind or default_dataset_kind
        document_id = f"table:{archived.content_hash[:16]}"
        self.store.upsert_document(
            kb_name=scope.kb_name,
            document_name=archived.filename,
            dataset_kind=dataset_kind,
            dataset_id="",
            document_id=document_id,
            source_group=archived.source_group,
            department_id=scope.department_id,
            uploaded_by=scope.uploaded_by,
            kb_id=scope.kb_id,
            status=TABLE_STATUS_ARCHIVED,
            original_file_name=os.path.basename(archived.original_path),
            local_path=archived.relative_local_path,
            file_size=archived.file_size,
            content_hash=archived.content_hash,
            upload_status=TABLE_STATUS_ARCHIVED,
            content_kind=CONTENT_KIND_SPREADSHEET,
            processor_kind=PROCESSOR_KIND_SPREADSHEET,
        )
        record = self.store.get_document(
            scope.kb_name,
            archived.filename,
            dataset_kind,
            department_id=scope.department_id,
        )
        if record:
            self.store.update_document_progress_by_id(
                record.id,
                5,
                "已归档，等待表格结构化解析",
                status=TABLE_STATUS_ARCHIVED,
                error_message="",
            )
        self.ensure_worker()
        if progress_callback:
            progress_callback(100, f"{archived.filename}: 已提交表格解析任务")
        warning = archived.inspection.to_warning_message()
        return HandlerResult(
            success=True,
            message=f"[success] 已提交表格解析任务: {archived.filename}",
            document_id=document_id,
            record_id=record.id if record else None,
            status=TABLE_STATUS_ARCHIVED,
            warnings=[warning] if warning else [],
            audit_action="spreadsheet_upload_submitted",
            audit_metadata={
                "store_id": record.id if record else None,
                "kb_id": scope.kb_id,
                "dataset_kind": dataset_kind,
                "content_kind": CONTENT_KIND_SPREADSHEET,
                "processor_kind": PROCESSOR_KIND_SPREADSHEET,
                "source_group": archived.source_group,
                "local_path": archived.relative_local_path,
                "content_hash": archived.content_hash,
                "status": TABLE_STATUS_ARCHIVED,
                "container_inspection": archived.inspection.to_metadata(),
            },
        )

    def rollback(self, result: HandlerResult, scope: IngestionScope):
        if result.record_id:
            self.store.delete_document_by_id(result.record_id)

    def process_record(self, record: PipelineDocumentRecord, archive: DocumentArchiveManager) -> HandlerProcessResult:
        if not self.parse_index:
            raise RuntimeError("Spreadsheet index callback is not configured.")

        archived_path = archive.resolve_record_path(record)

        def progress(progress_value: int, stage: str):
            self.store.update_document_progress_by_id(
                record.id,
                progress_value,
                stage,
                status=TABLE_STATUS_PROCESSING,
            )

        progress(10, "Preparing Excel structure parsing")
        result = self.parse_index(
            SpreadsheetIndexRequest(
                record_id=record.id,
                kb_id=record.kb_id,
                kb_name=record.kb_name,
                department_id=record.department_id,
                document_name=record.document_name,
                source_group=record.source_group,
                file_path=archived_path,
                local_path=record.local_path,
                content_hash=record.content_hash,
            ),
            progress_callback=progress,
        )
        self.store.update_document_progress_by_id(
            record.id,
            100,
            result.message,
            status=result.status,
            error_message="",
        )
        self.store.release_parse_claim(record.id)
        return HandlerProcessResult(
            ok=result.ok,
            status=result.status,
            message=result.message,
            progress=100,
            warnings=result.warnings,
            audit_action="spreadsheet_upload_indexed" if result.ok else "spreadsheet_upload_processed",
            audit_metadata={
                "store_id": record.id,
                "kb_id": record.kb_id,
                "dataset_kind": self.spec.dataset_kind,
                "content_kind": CONTENT_KIND_SPREADSHEET,
                "processor_kind": PROCESSOR_KIND_SPREADSHEET,
                "source_group": record.source_group,
                "local_path": record.local_path,
                "content_hash": record.content_hash,
                "status": result.status,
                "table_stats": result.stats.__dict__ if result.stats else None,
                "spreadsheet_warnings": result.warnings,
            },
        )

    def cleanup_failed_process(self, record: PipelineDocumentRecord):
        if self.delete_index:
            self.delete_index(record)

    def delete_record(self, record: PipelineDocumentRecord, archive: DocumentArchiveManager) -> HandlerDeleteResult:
        errors: list[str] = []
        if self.delete_index:
            try:
                self.delete_index(record)
            except Exception as exc:
                errors.append(f"spreadsheet index: {exc}")
                log(f"Spreadsheet index delete failed for {record.id}, keeping store row for retry: {exc}")
                return HandlerDeleteResult(
                    ok=False,
                    message=f"Spreadsheet index cleanup failed for {record.document_name}: {exc}",
                    errors=errors,
                    audit_action="spreadsheet_delete_document_failed",
                )
        try:
            archive.remove_record_archive(record)
        except Exception as exc:
            errors.append(f"archive: {exc}")
            return HandlerDeleteResult(
                ok=False,
                message=f"Archive cleanup failed for {record.document_name}: {exc}",
                errors=errors,
                audit_action="spreadsheet_delete_document_failed",
            )
        try:
            self.store.delete_document_by_id(record.id)
        except Exception as exc:
            errors.append(f"store: {exc}")
        suffix = f" (partial cleanup failed: {'; '.join(errors)})" if errors else ""
        return HandlerDeleteResult(
            ok=True,
            message=f"Removed archived spreadsheet: {record.document_name}{suffix}",
            errors=errors,
            audit_action="spreadsheet_delete_document",
        )
