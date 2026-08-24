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
        filters = dict(filters or {})
        strict_ids = self._strict_allowed_record_ids(kb_name, ctx, filters)
        if strict_ids == []:
            # Fail closed: every requested record was unauthorized or unknown.
            return []
        backend_filters = self._backend_filters(kb_name, ctx, filters)
        if strict_ids is not None:
            backend_filters["allowed_record_ids"] = strict_ids
            stamps = self._link_stamps(filters, strict_ids)
            if stamps:
                backend_filters["link_stamps"] = stamps
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
            locator = {
                "document_id": item.id,
                "chunk_id": metadata.get("chunk_id") or metadata.get("id") or item.id,
                "page": metadata.get("page") or metadata.get("page_number"),
            }
            local_record_id = metadata.get("local_record_id")
            if local_record_id is not None:
                locator["record_id"] = local_record_id
            evidences.append(
                Evidence(
                    id=f"document:{item.id}",
                    content=item.content,
                    source_name=item.source_name,
                    content_kind=metadata.get("content_kind", "document_text"),
                    processor_kind=metadata.get("processor_kind", item.backend or "ragflow"),
                    score=float(item.score or 0.0),
                    locator=locator,
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

    def _strict_allowed_record_ids(
        self,
        kb_name: str,
        ctx: RequestContext | None,
        filters: dict[str, Any],
    ) -> list[int] | None:
        """Normalize ``allowed_record_ids``; ``[]`` means fail closed.

        Returns ``None`` when the caller did not request the strict linked-
        record path so ordinary retrieval behavior stays untouched.
        """
        raw = filters.get("allowed_record_ids")
        if raw is None:
            return None
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple, set, frozenset)):
            return []
        try:
            ids = sorted({int(item) for item in raw})
        except (TypeError, ValueError):
            return []
        if not ids:
            return []
        department_id = str(
            (getattr(ctx, "metadata", {}) or {}).get("resource_department_id")
            or (getattr(ctx, "metadata", {}) or {}).get("department_id")
            or ""
        )
        if not department_id:
            raise PermissionError("allowed_record_ids retrieval requires department context.")
        if self.document_store is None:
            # Strict authorization requires the governed local store.
            return []
        authorized: list[int] = []
        for record_id in ids:
            try:
                record = self.document_store.get_document_by_id_scoped(record_id, department_id)
            except Exception:
                record = None
            if (
                record is None
                or record.kb_name != kb_name
                or str(record.processor_kind or "") != "ragflow"
            ):
                continue
            authorized.append(record.id)
        if len(authorized) != len(ids):
            return []
        return authorized

    def _link_stamps(self, filters: dict[str, Any], record_ids: list[int]) -> dict[int, dict[str, Any]]:
        raw = filters.get("link_stamps")
        if not isinstance(raw, dict):
            return {}
        stamps: dict[int, dict[str, Any]] = {}
        for record_id in record_ids:
            value = raw.get(str(record_id))
            if isinstance(value, dict):
                stamps[record_id] = value
        return stamps

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
