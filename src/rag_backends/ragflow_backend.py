from typing import Generator

from src.rag_backends.base import RAGBackend
from src.rag_backends.schemas import BackendHealth, BackendResult, DocumentInfo, Evidence, IngestResult, RequestContext


class RAGFlowBackend(RAGBackend):
    """Reserved adapter for future RAGFlow integration."""

    name = "ragflow"

    def _not_configured(self):
        raise NotImplementedError("RAGFlowBackend 尚未接入，请先使用 RAG_BACKEND=local")

    def ingest(self, kb_name: str, files: list[str], ctx: RequestContext | None = None) -> IngestResult:
        self._not_configured()

    def retrieve(self, kb_name: str, query: str, top_k: int | None = None, ctx: RequestContext | None = None) -> list[Evidence]:
        self._not_configured()

    def stream_answer(
        self,
        kb_name: str,
        query: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
    ) -> Generator[str, None, None]:
        self._not_configured()

    def delete_document(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> BackendResult:
        self._not_configured()

    def list_documents(self, kb_name: str, ctx: RequestContext | None = None) -> list[DocumentInfo]:
        self._not_configured()

    def health_check(self) -> BackendHealth:
        return BackendHealth(ok=False, message="RAGFlowBackend 尚未接入", backend=self.name)
