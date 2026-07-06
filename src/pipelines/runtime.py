import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from src.core.logger import error
from src.pipelines.document_store import PipelineDocumentRecord, PipelineDocumentStore
from src.pipelines.ingestion import HandlerDeleteResult, IngestionOrchestrator, PipelineHandler
from src.pipelines.registry import PROCESSOR_KIND_SPREADSHEET
from src.services.document_archive import DocumentArchiveManager


def _noop_audit(*args, **kwargs):
    return None


@dataclass
class PipelineRuntime:
    store: PipelineDocumentStore
    archive: DocumentArchiveManager
    ingestion: IngestionOrchestrator
    audit_callback: Callable[..., object] | None
    worker_name: str = "pipeline-parse-worker"

    def __post_init__(self):
        if not callable(self.audit_callback):
            error("PipelineRuntime audit_callback is not callable; audit events will be skipped.")
            self.audit_callback = _noop_audit
        self._worker_lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._worker_id = f"{self.worker_name}-{uuid.uuid4().hex}"

    @property
    def handlers(self) -> dict[str, PipelineHandler]:
        return self.ingestion.handlers

    def ensure_worker_running(self):
        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._worker_thread = threading.Thread(
                target=self.parse_worker_loop,
                name=self.worker_name,
                daemon=True,
            )
            self._worker_thread.start()

    def parse_worker_loop(self):
        processor_kinds = self.background_processor_kinds()
        while True:
            record = self.store.claim_next_parse_record(self._worker_id, processor_kinds=processor_kinds)
            if not record:
                return
            try:
                self.process_record(record)
            except Exception as exc:
                error(f"Parse worker failed for {record.document_name}: {exc}")
                try:
                    handler = self.handlers.get(record.processor_kind)
                    if handler is not None:
                        handler.cleanup_failed_process(record)
                    self.store.mark_document_failed_by_id(record.id, str(exc))
                except Exception as mark_error:
                    error(f"Failed to mark parse task failed for {record.id}: {mark_error}")

    def process_record(self, record: PipelineDocumentRecord):
        handler = self.handlers.get(record.processor_kind)
        if handler is None:
            raise RuntimeError(f"No pipeline handler registered for {record.processor_kind}.")
        result = handler.process_record(record, self.archive)
        if not result.ok:
            raise RuntimeError(result.message)
        self.audit_callback(
            result.audit_action or f"{record.processor_kind}_processed",
            None,
            kb_name=record.kb_name,
            target_type="document",
            target_id=record.document_name,
            metadata=result.audit_metadata,
        )

    def delete_record(self, record: PipelineDocumentRecord) -> HandlerDeleteResult:
        handler = self.handlers.get(record.processor_kind)
        if handler is None:
            raise RuntimeError(f"No pipeline handler registered for {record.processor_kind}.")
        return handler.delete_record(record, self.archive)

    def background_processor_kinds(self) -> tuple[str, ...]:
        # Until registry carries a background-processing flag, only processors
        # that queue local records should be claimed by this worker.
        return tuple(kind for kind in self.handlers if kind == PROCESSOR_KIND_SPREADSHEET)
