"""Document RAG retrieval tool (RAGFlow-backed)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.agents.schemas import Evidence
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_store import PipelineDocumentStore


class ScopedEvidenceBatch(list):
    """Evidence rows carrying source-level status and fusion audit metadata."""

    source_statuses: list[dict[str, Any]] = []
    fusion_summary: dict[str, Any] = {}


def _scope_statuses(outcome) -> list[dict[str, Any]]:
    return [
        {
            "kb_id": int(getattr(item, "kb_id", 0) or 0),
            "query": str(getattr(item, "query", "") or ""),
            "retriever_kind": str(getattr(item, "retriever_kind", "") or ""),
            "status": str(getattr(item, "status", "retrieval_failed") or "retrieval_failed"),
            "error_code": str(getattr(item, "error_code", "") or "") or None,
            "diagnostics": dict(getattr(item, "diagnostics", {}) or {}),
        }
        for item in (getattr(outcome, "source_outcomes", None) or [])
    ]


def _fuse_scoped_outcome(outcome, *, purpose: str, max_items: int):
    """Fuse production outcomes while keeping compatibility with old test doubles."""

    source_outcomes = getattr(outcome, "source_outcomes", None)
    if not source_outcomes:
        return list(getattr(outcome, "evidence", []) or []), {}
    import src.settings as settings
    from src.services.evidence_fusion import fuse_evidence

    fused = fuse_evidence(
        outcome,
        purpose=purpose,
        max_items=max(1, min(int(max_items), int(settings.MULTI_KB_EVIDENCE_MAX_ITEMS))),
        token_budget=max(0, int(settings.MULTI_KB_CONTEXT_MAX_TOKENS)),
    )
    return list(fused.evidence), {
        "selection_hash": str(getattr(outcome, "selection_hash", "") or ""),
        "coverage": dict(fused.coverage),
        "complete": bool(fused.complete),
        "token_count": int(fused.token_count),
        "candidate_refs": list(fused.candidate_refs),
        "discarded": dict(fused.discarded),
        "query_coverage": list(fused.query_coverage),
        "source_representatives": list(fused.source_representatives),
    }


def _scoped_batch(outcome, *, purpose: str, max_items: int) -> ScopedEvidenceBatch:
    raw_items, fusion_summary = _fuse_scoped_outcome(
        outcome, purpose=purpose, max_items=max_items
    )
    result = ScopedEvidenceBatch(_convert_scoped_evidence(item) for item in raw_items)
    result.source_statuses = _scope_statuses(outcome)
    result.fusion_summary = fusion_summary
    return result


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


def _convert_scoped_evidence(item) -> Evidence:
    """Convert adapter evidence while retaining its complete source identity."""
    metadata = dict(getattr(item, "metadata", None) or {})
    kb_id = metadata.get("kb_id")
    # Remote chunk IDs are only unique inside a source. Prefixing the citation
    # identity prevents equal chunk IDs from collapsing across KBs.
    identity = str(getattr(item, "id", "") or "")
    if kb_id not in (None, ""):
        identity = f"{kb_id}:{identity}"
    return Evidence(
        id=f"document:{identity}",
        content=str(getattr(item, "content", "") or ""),
        source_name=str(getattr(item, "source_name", "") or ""),
        content_kind=str(metadata.get("content_kind") or "document_text"),
        processor_kind=str(metadata.get("processor_kind") or getattr(item, "backend", "") or "ragflow"),
        score=float(getattr(item, "score", 0.0) or 0.0),
        locator={
            "document_id": metadata.get("remote_document_id") or getattr(item, "id", ""),
            "chunk_id": metadata.get("chunk_or_structured_locator") or getattr(item, "id", ""),
            "page": metadata.get("page") or metadata.get("page_number"),
        },
        metadata={
            **metadata,
            "retriever": getattr(item, "retriever", ""),
            "backend": getattr(item, "backend", ""),
            "tool": "document_search",
        },
    )


def make_document_search(rt, rag_backend: RAGBackend, document_store: PipelineDocumentStore | None):
    """Return a ``document_search(query, top_k)`` tool closure."""

    def run(query: str, top_k: int) -> list[Evidence]:
        if rt.knowledge_scope is not None:
            service = getattr(rt, "multi_kb_retrieval", None)
            budget = getattr(rt, "retrieval_budget", None)
            if service is None or budget is None:
                raise RuntimeError("multi_kb_retrieval_not_configured")
            outcome = service.search(
                snapshot=rt.knowledge_scope,
                queries=[query],
                retriever_kind="document",
                per_source_top_k=max(1, min(int(top_k), 20)),
                budget=budget,
            )
            return _scoped_batch(
                outcome,
                purpose=str(getattr(rt.knowledge_scope, "purpose", "answer")),
                max_items=max(1, min(int(top_k), 20)),
            )
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
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        items, adds_nothing = timed_tool_call(rt, "document_search", query, None, lambda: run(query, max(1, min(int(top_k), 20))))
        return format_tool_result(rt, adds_nothing, items)

    return document_search


def make_document_search_batch(
    rt, rag_backend: RAGBackend, document_store: PipelineDocumentStore | None
):
    """Return a model-controlled fan-out document retrieval tool.

    The model chooses independent query variants; only their backend calls are
    parallelized here. Each child uses the normal document-search path, so
    authorization, cancellation, evidence registration and citations remain
    shared with single-query retrieval.
    """
    search = make_document_search(rt, rag_backend, document_store)

    def document_search_batch(queries: list[str], top_k: int = rt.top_k) -> str:
        """并发检索多个相互独立的文档查询。适合中文/英文、型号/标题等查询变体；不要把有前后依赖的查询放在同一批。"""
        import src.settings as settings

        normalized = []
        for query in queries or []:
            value = str(query or "").strip()
            if value and value not in normalized:
                normalized.append(value)
        try:
            fanout_cap = max(1, int(getattr(settings, "RETRIEVAL_PER_REQUEST_FANOUT", 4)))
        except (TypeError, ValueError):
            fanout_cap = 4
        normalized = normalized[:fanout_cap]
        if not normalized:
            return "（未提供有效的文档查询）"
        safe_top_k = max(1, min(int(top_k), 20))
        if rt.knowledge_scope is not None:
            # Calling ``document_search`` through the shared executor would
            # submit work to the same pool from inside one of its workers.
            # Expand all query × source items in one service call instead.
            from src.agents.tools.runtime import format_tool_result, timed_tool_call

            service = getattr(rt, "multi_kb_retrieval", None)
            budget = getattr(rt, "retrieval_budget", None)
            if service is None or budget is None:
                return "（多知识库检索服务未配置）"

            def run_multi() -> list[Evidence]:
                outcome = service.search(
                    snapshot=rt.knowledge_scope,
                    queries=normalized,
                    retriever_kind="document",
                    per_source_top_k=safe_top_k,
                    budget=budget,
                )
                for value in normalized:
                    rt.log_query("document_search", value)
                return _scoped_batch(
                    outcome,
                    purpose=str(getattr(rt.knowledge_scope, "purpose", "answer")),
                    max_items=max(1, min(safe_top_k, 20)),
                )

            items, adds_nothing = timed_tool_call(
                rt, "document_search_batch", " | ".join(normalized), None, run_multi
            )
            return format_tool_result(rt, adds_nothing, items)
        if not bool(getattr(settings, "SHARED_RETRIEVAL_EXECUTOR_ENABLED", True)):
            with ThreadPoolExecutor(
                max_workers=len(normalized), thread_name_prefix="hdb-document-search"
            ) as executor:
                results = list(executor.map(lambda query: search(query, safe_top_k), normalized))
            return "\n\n".join(result for result in results if result)

        from src.agents.tools.retrieval_executor import get_retrieval_executor

        try:
            from src.observability.metrics import record_rag_batch

            record_rag_batch(fanout=len(normalized))
        except Exception:
            pass
        cancel_check = None
        if getattr(rt, "should_cancel", None) is not None:
            cancel_check = lambda: bool(rt.should_cancel())  # noqa: E731
        call_results = get_retrieval_executor().map_bounded(
            lambda query: search(query, safe_top_k),
            normalized,
            fanout=fanout_cap,
            deadline=float(getattr(settings, "RETRIEVAL_QUEUE_MAX_WAIT_SECONDS", 30.0)),
            cancel_check=cancel_check,
        )
        texts = [str(item.value) for item in call_results if item.status == "ok" and item.value]
        if not texts and any(item.status == "saturated" for item in call_results):
            return "（检索服务繁忙，请稍后重试或减少并发查询）"
        return "\n\n".join(texts)

    return document_search_batch
