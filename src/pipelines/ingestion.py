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
    CONTENT_KIND_SPREADSHEET,
    DATASET_CIRCUIT,
    PIPELINE_REGISTRY,
    PROCESSOR_KIND_CIRCUIT,
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
    TABLE_STATUS_ARCHIVED,
    TABLE_STATUS_PROCESSING,
)


@dataclass
class _IngestLockEntry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    active_count: int = 0


_INGEST_LOCKS: dict[str, _IngestLockEntry] = {}
_INGEST_LOCKS_GUARD = threading.Lock()


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

        for file_path in files:
            filename = os.path.basename(file_path)
            route = route_file(file_path)
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
                        if self._existing_record_is_reusable(existing, handler):
                            messages.append(handler.reuse_message(existing))
                            success_count += 1
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

        return IngestResult(
            success_count=success_count,
            total_count=len(files),
            messages=messages,
            backend=self.backend_name,
            failed_count=failed_count,
            skipped_count=skipped_count,
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

    def _existing_record_is_reusable(self, record: PipelineDocumentRecord, handler: PipelineHandler) -> bool:
        if not self.archive.record_archive_exists(record):
            return False
        if record.processor_kind == PROCESSOR_KIND_RAGFLOW and not self.remote_document_exists(record):
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
            if record_id:
                self.store.update_document_progress_by_id(
                    record_id,
                    100,
                    getattr(index_result, "message", "") or "Circuit design indexed",
                    status=TABLE_STATUS_INDEXED,
                    error_message="",
                )
            if progress_callback:
                progress_callback(100, f"{archived.filename}: circuit design indexed")
            return HandlerResult(
                success=True,
                message=f"[success] Indexed circuit design file: {archived.filename}",
                document_id=document_id,
                record_id=record_id,
                status=TABLE_STATUS_INDEXED,
                warnings=warnings,
                audit_action="circuit_upload_indexed",
                audit_metadata={
                    "store_id": record_id,
                    "kb_id": scope.kb_id,
                    "dataset_kind": dataset_kind,
                    "content_kind": CONTENT_KIND_CIRCUIT,
                    "processor_kind": PROCESSOR_KIND_CIRCUIT,
                    "source_group": archived.source_group,
                    "local_path": archived.relative_local_path,
                    "content_hash": archived.content_hash,
                    "status": TABLE_STATUS_INDEXED,
                    "container_inspection": archived.inspection.to_metadata(),
                    "circuit_stats": getattr(index_result, "stats", {}),
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
