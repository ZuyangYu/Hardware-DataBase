from __future__ import annotations

import json
import queue
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from src.core.app_logs import AppLogService, query_trace_status
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import QueryRequest

router = APIRouter(tags=["query"])


def _sse(event: str, data: dict) -> str:
    # default=str keeps token_usage / trace objects serialisable without shaping them here.
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/query")
async def query(
    body: QueryRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, body.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(body.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    # Keep only the most recent 5 history turns -- Streamlit slices [-5:] and
    # the agent prompt isn't sized for unbounded history.
    history = [tuple(h) for h in body.history[-5:]]

    # pipeline.query is a *sync* generator whose _RUN_RECORD ContextVar is set
    # inside the generator body. If we iterated it via run_in_threadpool the
    # worker thread could change between next() calls and lose the record, so
    # the footer/summary read afterwards would be empty. Instead a single
    # dedicated producer thread owns the whole generator + the post-stream
    # observability reads; the async side only pulls from the queue.
    q: queue.Queue = queue.Queue(maxsize=64)
    sentinel = object()
    cancel = threading.Event()

    def _put(item) -> None:
        # Block on a full queue (backpressure) but wake periodically so the
        # producer can notice the consumer has disconnected and stop early,
        # instead of buffering an unbounded stream the client will never read.
        while not cancel.is_set():
            try:
                q.put(item, timeout=0.5)
                return
            except queue.Full:
                continue
        # Consumer gone: drop the item.

    def producer() -> None:
        start = time.monotonic()
        summary: dict = {}
        gen = None
        try:
            answer_parts: list[str] = []
            gen = pipeline.query(body.query, body.kb_name, history, ctx, agent_thread_id=body.thread_id)
            for chunk in gen:
                if cancel.is_set():
                    break
                if chunk:
                    answer_parts.append(chunk)
                    _put(("delta", {"text": chunk}))
            summary = pipeline.get_last_retrieval_summary() or {}
            answer = "".join(answer_parts)
            if not cancel.is_set():
                q.put(
                    (
                        "done",
                        {
                            "answer": answer,
                            "summary": summary,
                            "footer": pipeline.get_last_agent_footer(),
                            "token_usage": pipeline.get_last_token_usage_summary(),
                        },
                    )
                )
            # Derive trace status from the answer text + retrieval summary
            # (same helper Streamlit uses) instead of hardcoding "success" --
            # a failed/partial/no-evidence response is then logged accurately.
            trace_status, trace_error = query_trace_status(answer, summary)
            _record_query_trace(
                user=user,
                kb_name=body.kb_name,
                original_query=body.query,
                thread_id=body.thread_id,
                summary=summary,
                latency_ms=int((time.monotonic() - start) * 1000),
                status="failed" if cancel.is_set() else trace_status,
                error_message=("client disconnected" if cancel.is_set() else trace_error),
            )
        except Exception as exc:  # fail-open: surface the error as an SSE event
            if not cancel.is_set():
                q.put(("error", {"message": str(exc)}))
            _record_query_trace(
                user=user,
                kb_name=body.kb_name,
                original_query=body.query,
                thread_id=body.thread_id,
                summary=pipeline.get_last_retrieval_summary() or {},
                latency_ms=int((time.monotonic() - start) * 1000),
                status="failed",
                error_message=str(exc),
            )
        finally:
            # Release the generator if we exited early (client disconnect) so
            # the LLM producer can stop sooner.
            if gen is not None:
                close = getattr(gen, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            q.put(sentinel)

    async def event_stream():
        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        try:
            while True:
                item = await run_in_threadpool(q.get)
                if item is sentinel:
                    break
                event, payload = item
                yield _sse(event, payload)
        finally:
            # Client disconnected: signal the producer to stop and give it a
            # moment to flush its trace + close the generator.
            cancel.set()
            thread.join(timeout=2.0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _record_query_trace(
    *,
    user: AuthUser,
    kb_name: str,
    original_query: str,
    thread_id: str,
    summary: dict,
    latency_ms: int,
    status: str,
    error_message: str = "",
) -> None:
    """Persist a query trace + retrieved evidence, fail-soft. Mirrors the
    Streamlit UI's post-stream logging so the API path is equally observable
    in the log center (query logs + evidence drill-down)."""
    try:
        log_service = AppLogService()
        rewritten_query = " | ".join(summary.get("rewritten_queries") or [])[:500]
        trace_id = log_service.record_query_trace(
            user=user,
            kb_name=kb_name,
            original_query=original_query,
            rewritten_query=rewritten_query,
            backend="ragflow",
            retriever_type=summary.get("retriever_type") or "",
            final_top_k=summary.get("final_top_k"),
            latency_ms=latency_ms,
            status=status,
            error_message=error_message,
            metadata={"thread_id": thread_id, "source": "api"},
        )
        log_service.record_retrieved_evidence(trace_id, summary.get("evidence") or [])
    except Exception as trace_error:
        # Trace logging must never break the answer stream.
        from src.core.logger import log as _log

        _log(f"API query trace logging failed: {trace_error}")
