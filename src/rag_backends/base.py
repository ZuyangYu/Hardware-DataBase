from abc import ABC, abstractmethod
from typing import Generator

from src.rag_backends.schemas import BackendHealth, BackendResult, DocumentInfo, Evidence, IngestResult, RequestContext


class RAGBackend(ABC):
    name: str

    @abstractmethod
    def ingest(self, kb_name: str, files: list[str], ctx: RequestContext | None = None) -> IngestResult:
        """Ingest and index files into a knowledge base."""

    @abstractmethod
    def retrieve(self, kb_name: str, query: str, top_k: int | None = None, ctx: RequestContext | None = None) -> list[Evidence]:
        """Retrieve evidence for a query."""

    @abstractmethod
    def stream_answer(
        self,
        kb_name: str,
        query: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
    ) -> Generator[str, None, None]:
        """Stream an answer for a query."""

    @abstractmethod
    def delete_document(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> BackendResult:
        """Delete a document from a knowledge base."""

    @abstractmethod
    def list_documents(self, kb_name: str, ctx: RequestContext | None = None) -> list[DocumentInfo]:
        """List documents in a knowledge base."""

    @abstractmethod
    def health_check(self) -> BackendHealth:
        """Return backend health."""
