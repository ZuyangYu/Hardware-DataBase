from dataclasses import dataclass

from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.ingestion import (
    IngestionOrchestrator,
    RAGFlowDocumentHandler,
    SpreadsheetPipelineHandler,
)
from src.pipelines.registry import PIPELINE_REGISTRY, PROCESSOR_KIND_RAGFLOW, PROCESSOR_KIND_SPREADSHEET
from src.pipelines.runtime import PipelineRuntime
from src.services.document_archive import DocumentArchiveManager
from src.services.spreadsheet_index_service import SpreadsheetIndexService


@dataclass
class PipelineRuntimeBundle:
    store: PipelineDocumentStore
    archive: DocumentArchiveManager
    spreadsheet_indexes: SpreadsheetIndexService
    ingestion: IngestionOrchestrator
    runtime: PipelineRuntime


class PipelineRuntimeFactory:
    def __init__(
        self,
        *,
        backend_name: str,
        submit_remote_callback,
        cleanup_remote_callback,
        delete_remote_callback,
        audit_callback,
        content_hash_callback,
        remote_document_exists_callback,
        worker_name: str,
        store: PipelineDocumentStore | None = None,
        archive: DocumentArchiveManager | None = None,
        spreadsheet_indexes: SpreadsheetIndexService | None = None,
    ):
        self.backend_name = backend_name
        self.submit_remote = submit_remote_callback
        self.cleanup_remote = cleanup_remote_callback
        self.delete_remote = delete_remote_callback
        self.audit = audit_callback
        self.content_hash = content_hash_callback
        self.remote_document_exists = remote_document_exists_callback
        self.worker_name = worker_name
        self.store = store or PipelineDocumentStore()
        self.archive = archive or DocumentArchiveManager()
        self.spreadsheet_indexes = spreadsheet_indexes or SpreadsheetIndexService()

    def build(self) -> PipelineRuntimeBundle:
        rag_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_RAGFLOW)
        spreadsheet_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET)
        if rag_spec is None or spreadsheet_spec is None:
            raise RuntimeError("Required ingestion pipeline specs are not registered.")

        runtime_ref: dict[str, PipelineRuntime] = {}

        def ensure_worker_running():
            runtime_ref["runtime"].ensure_worker_running()

        ingestion = IngestionOrchestrator(
            backend_name=self.backend_name,
            store=self.store,
            archive=self.archive,
            handlers={
                PROCESSOR_KIND_RAGFLOW: RAGFlowDocumentHandler(
                    spec=rag_spec,
                    store=self.store,
                    submit_callback=self.submit_remote,
                    cleanup_remote_callback=self.cleanup_remote,
                    delete_remote_callback=self.delete_remote,
                ),
                PROCESSOR_KIND_SPREADSHEET: SpreadsheetPipelineHandler(
                    spec=spreadsheet_spec,
                    store=self.store,
                    ensure_worker_callback=ensure_worker_running,
                    parse_index_callback=self.spreadsheet_indexes.parse_and_index,
                    delete_index_callback=self.spreadsheet_indexes.delete_record,
                ),
            },
            audit_callback=self.audit,
            content_hash_callback=self.content_hash,
            remote_document_exists_callback=self.remote_document_exists,
        )
        runtime = PipelineRuntime(
            store=self.store,
            archive=self.archive,
            ingestion=ingestion,
            audit_callback=self.audit,
            worker_name=self.worker_name,
        )
        runtime_ref["runtime"] = runtime
        return PipelineRuntimeBundle(
            store=self.store,
            archive=self.archive,
            spreadsheet_indexes=self.spreadsheet_indexes,
            ingestion=ingestion,
            runtime=runtime,
        )
