from dataclasses import dataclass

from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.ingestion import (
    CircuitPipelineHandler,
    ExternalConversationHandler,
    IngestionOrchestrator,
    RAGFlowDocumentHandler,
    SpreadsheetPipelineHandler,
)
from src.pipelines.registry import (
    PIPELINE_REGISTRY,
    PROCESSOR_KIND_CIRCUIT,
    PROCESSOR_KIND_EXTERNAL_CONVERSATION,
    PROCESSOR_KIND_RAGFLOW,
    PROCESSOR_KIND_SPREADSHEET,
)
from src.pipelines.runtime import PipelineRuntime
from src.circuit.index_service import CircuitIndexService
from src.external_conversations.query_engine import ExternalConversationQueryEngine
from src.external_conversations.store import ExternalConversationStore
from src.external_conversations.vector_index import default_external_conversation_vector_index
from src.services.document_archive import DocumentArchiveManager
from src.services.spreadsheet_index_service import SpreadsheetIndexService


@dataclass
class PipelineRuntimeBundle:
    store: PipelineDocumentStore
    archive: DocumentArchiveManager
    spreadsheet_indexes: SpreadsheetIndexService
    circuit_indexes: CircuitIndexService
    ingestion: IngestionOrchestrator
    runtime: PipelineRuntime
    conversations: ExternalConversationStore | None = None
    conversation_indexes: ExternalConversationQueryEngine | None = None


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
        circuit_indexes: CircuitIndexService | None = None,
        conversations: ExternalConversationStore | None = None,
        conversation_indexes: ExternalConversationQueryEngine | None = None,
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
        self.circuit_indexes = circuit_indexes or CircuitIndexService()
        self.conversations = conversations or ExternalConversationStore()
        self.conversation_indexes = conversation_indexes or ExternalConversationQueryEngine(
            root=self.conversations.root
        )

    def build(self) -> PipelineRuntimeBundle:
        rag_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_RAGFLOW)
        spreadsheet_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET)
        circuit_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_CIRCUIT)
        conversation_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_EXTERNAL_CONVERSATION)
        if rag_spec is None or spreadsheet_spec is None or circuit_spec is None:
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
                PROCESSOR_KIND_CIRCUIT: CircuitPipelineHandler(
                    spec=circuit_spec,
                    store=self.store,
                    circuit_index=self.circuit_indexes,
                ),
                **(
                    {
                        PROCESSOR_KIND_EXTERNAL_CONVERSATION: ExternalConversationHandler(
                            spec=conversation_spec,
                            store=self.store,
                            conversation_store=self.conversations,
                            conversation_indexes=self.conversation_indexes,
                            vector_index=default_external_conversation_vector_index,
                        )
                    }
                    if conversation_spec is not None
                    else {}
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
            circuit_indexes=self.circuit_indexes,
            ingestion=ingestion,
            runtime=runtime,
            conversations=self.conversations,
            conversation_indexes=self.conversation_indexes,
        )
