from __future__ import annotations

import json
import queue
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline
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
    if not ctx.has_kb_permission(body.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    history = [tuple(h) for h in body.history]

    # pipeline.query is a *sync* generator whose _RUN_RECORD ContextVar is set
    # inside the generator body. If we iterated it via run_in_threadpool the
    # worker thread could change between next() calls and lose the record, so
    # the footer/summary read afterwards would be empty. Instead a single
    # dedicated producer thread owns the whole generator + the post-stream
    # observability reads; the async side only pulls from the queue.
    q: queue.Queue = queue.Queue()
    sentinel = object()

    def producer() -> None:
        try:
            answer_parts: list[str] = []
            for chunk in pipeline.query(body.query, body.kb_name, history, ctx, agent_thread_id=body.thread_id):
                if chunk:
                    answer_parts.append(chunk)
                    q.put(("delta", {"text": chunk}))
            q.put(
                (
                    "done",
                    {
                        "answer": "".join(answer_parts),
                        "summary": pipeline.get_last_retrieval_summary(),
                        "footer": pipeline.get_last_agent_footer(),
                        "token_usage": pipeline.get_last_token_usage_summary(),
                    },
                )
            )
        except Exception as exc:  # fail-open: surface the error as an SSE event
            q.put(("error", {"message": str(exc)}))
        finally:
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
            thread.join(timeout=1.0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
