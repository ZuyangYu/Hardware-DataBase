"""Document RAG retrieval tool (RAGFlow-backed)."""

from __future__ import annotations

from typing import Any

from src.agents.schemas import Evidence
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_store import PipelineDocumentStore


def _backend_filters(
    kb_name: str,
    ctx,
    filters: dict[str, Any] | None,
    document_store: PipelineDocumentStore | None,
) -> dict[str, Any]:
    backend_filters = dict(filters or {})
    if not backend_filters.get("record_id") or not document_store:
        return backend_filters

    try:
        record_id = int(backend_filters["record_id"])
        ctx_metadata = ctx.metadata if ctx else {}
        department_id = str(ctx_metadata.get("resource_department_id") or ctx_metadata.get("department_id") or "")
        record = document_store.get_document_by_id_scoped(record_id, department_id)
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


def make_document_search(rt, rag_backend: RAGBackend, document_store: PipelineDocumentStore | None):
    """Return a ``document_search(query, top_k)`` tool closure."""

    def run(query: str, top_k: int) -> list[Evidence]:
        backend_filters = _backend_filters(rt.kb_name, rt.ctx, None, document_store)
        retrieve_kwargs: dict[str, Any] = {"top_k": top_k, "ctx": rt.ctx, "filters": backend_filters}
        raw = rag_backend.retrieve(rt.kb_name, query, **retrieve_kwargs)
        if not raw and not backend_filters.get("source_names") and not backend_filters.get("record_id"):
            # M2: source_group 朴素子串路由可能把查询锁进错误来源组。全库检索
            # 零命中时做一次 fail-open 恢复——放开来源组硬过滤重查（冻结的
            # source_names 范围仍在时跳过，避免对定向检索误报）。
            retry_filters = dict(backend_filters)
            retry_filters["balanced_route"] = True
            raw = rag_backend.retrieve(
                rt.kb_name, query, top_k=top_k, ctx=rt.ctx, filters=retry_filters
            )
            if raw:
                rt.emit(
                    "stage",
                    {
                        "key": "balanced_route_retry",
                        "label": "来源路由降级重试",
                        "status": "done",
                        "detail": f"首次 0 命中，放开来源组过滤后补回 {len(raw)} 条",
                    },
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
                        "tool": "document_search",
                        "query": query,
                    },
                )
            )
        return evidences[:top_k]

    def document_search(query: str, top_k: int = rt.top_k) -> str:
        """在知识库中检索文档资料（Word/PDF/文本等），返回与查询最相关的证据片段。支持硬件设计文档、规范、说明书的语义检索。"""
        from src.agents.tools.runtime import format_evidence_for_llm, timed_tool_call

        items = timed_tool_call(rt, "document_search", query, None, lambda: run(query, max(1, min(int(top_k), 20))))
        return format_evidence_for_llm(items)

    return document_search
