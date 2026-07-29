from __future__ import annotations

from typing import Any, Callable

from src.agents.state import Evidence
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.pipelines.document_store import PipelineDocumentStore


class DocumentRAGTool:
    name = "document_rag"
    description = "Retrieve evidence from document RAG sources such as Word, PDF, and parsed text documents."
    supports_cancellation = True

    def __init__(self, rag_backend: RAGBackend, document_store: PipelineDocumentStore | None = None):
        self.rag_backend = rag_backend
        self.document_store = document_store

    def run(
        self,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[Evidence]:
        backend_filters = self._backend_filters(kb_name, ctx, filters)
        retrieve_kwargs = {
            "top_k": top_k,
            "ctx": ctx,
            "filters": backend_filters,
        }
        if should_cancel is not None:
            retrieve_kwargs["should_cancel"] = should_cancel
        raw = self.rag_backend.retrieve(
            kb_name,
            query,
            **retrieve_kwargs,
        )
        evidences: list[Evidence] = []
        for item in raw:
            metadata = dict(item.metadata or {})
            evidences.append(
                Evidence(
                    id=f"document:{item.id}",
                    content=item.content,
                    source_name=item.source_name,
                    content_kind=metadata.get("content_kind", "document_text"),
                    processor_kind=metadata.get("processor_kind", item.backend or "ragflow"),
                    score=float(item.score or 0.0),
                    locator={
                        "document_id": item.id,
                        "chunk_id": metadata.get("chunk_id") or metadata.get("id") or item.id,
                        "page": metadata.get("page") or metadata.get("page_number"),
                    },
                    metadata={
                        **metadata,
                        "retriever": item.retriever,
                        "backend": item.backend,
                        "tool": self.name,
                        "query": query,
                    },
                )
            )
        return evidences[:top_k]

    def _backend_filters(self, kb_name: str, ctx: RequestContext | None, filters: dict | None) -> dict[str, Any]:
        backend_filters = dict(filters or {})
        if not backend_filters.get("record_id") or not self.document_store:
            return backend_filters

        try:
            record_id = int(backend_filters["record_id"])
            ctx_metadata = ctx.metadata if ctx else {}
            department_id = str(ctx_metadata.get("resource_department_id") or ctx_metadata.get("department_id") or "")
            record = self.document_store.get_document_by_id_scoped(record_id, department_id)
        except Exception:
            return backend_filters

        if record and record.kb_name == kb_name:
            source_names = []
            for name in [record.original_file_name, record.document_name, backend_filters.get("source_name")]:
                name = str(name or "").strip()
                if name and name not in source_names:
                    source_names.append(name)
            if source_names:
                backend_filters["source_names"] = source_names
            backend_filters["source_name"] = record.document_name
        return backend_filters
