from __future__ import annotations

import asyncio
import json
import inspect
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import config.settings
from src.core.app_logs import AppLogService, query_trace_status
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.core.conversation import ChatMessage, ChatTurn, ConversationService
from src.core.llm_client import LLMClient

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import CreateTurnRequest, MessageView, TurnStartResponse, TurnView

router = APIRouter(tags=["query"])
GENERAL_CHAT_KB_NAME = "__general__"
_TURN_CANCEL_SIGNALS: dict[str, threading.Event] = {}
_TURN_CANCEL_LOCK = threading.Lock()
_TERMINAL_TURN_STATUSES = {"completed", "cancelled", "failed"}


def _sse(event: str, data: dict, event_id: int | None = None) -> str:
    # default=str keeps token_usage / trace objects serialisable without shaping them here.
    payload = json.dumps(data, ensure_ascii=False, default=str)
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {payload}\n\n"


def _conv_service() -> ConversationService:
    return ConversationService()


def _turn_view(turn: ChatTurn) -> TurnView:
    return TurnView(
        id=turn.id,
        session_id=turn.session_id,
        user_message_id=turn.user_message_id,
        assistant_message_id=turn.assistant_message_id,
        kb_name=turn.kb_name,
        query=turn.query,
        query_mode=turn.query_mode if turn.query_mode in {"fast", "deep"} else "fast",
        status=turn.status,
        cancel_requested=turn.cancel_requested,
        last_event_seq=turn.last_event_seq,
        answer=turn.answer,
        summary=turn.summary,
        footer=turn.footer,
        metrics=turn.metrics,
        error_message=turn.error_message,
        created_at=turn.created_at,
        started_at=turn.started_at,
        finished_at=turn.finished_at,
    )


def _message_view(message: ChatMessage) -> MessageView:
    return MessageView(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        content=message.content,
        footer=message.footer,
        created_at=message.created_at,
    )


def _turn_cancel_signal(turn_id: str) -> threading.Event:
    """Create the optional in-process fast path for the owning worker only.

    The database ``cancel_requested`` flag is authoritative across processes.
    """
    with _TURN_CANCEL_LOCK:
        return _TURN_CANCEL_SIGNALS.setdefault(turn_id, threading.Event())


def _existing_turn_cancel_signal(turn_id: str) -> threading.Event | None:
    with _TURN_CANCEL_LOCK:
        return _TURN_CANCEL_SIGNALS.get(turn_id)


def _clear_turn_cancel_signal(turn_id: str) -> None:
    with _TURN_CANCEL_LOCK:
        _TURN_CANCEL_SIGNALS.pop(turn_id, None)


def _general_messages(history: list[tuple[str, str]], query: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "你是 Hardware DataBase 的通用对话助手。当前未挂载知识库,不要声称"
                "读取了任何私有文档、表格或电路数据。可以回答通用问题;如果用户需要"
                "基于知识库资料回答,请提示先挂载知识库。"
            ),
        }
    ]
    for user_text, assistant_text in history[-5:]:
        messages.append({"role": "user", "content": str(user_text or "")})
        messages.append({"role": "assistant", "content": str(assistant_text or "")})
    messages.append({"role": "user", "content": query})
    return messages


def _run_turn(*, turn_id: str, user: AuthUser, ctx, pipeline: AppPipeline | None) -> None:
    """Own a turn independently of any one SSE subscriber.

    Events and answer content are committed before they are published, so a
    browser refresh can replay the same turn without relying on client memory.
    """
    conv = ConversationService()
    worker_id = f"chat-{uuid.uuid4().hex}"
    turn = conv.claim_turn(user.id, turn_id, worker_id)
    if turn is None:
        return
    cancel = _turn_cancel_signal(turn_id)
    heartbeat_stop = threading.Event()
    heartbeat_interval = max(2, min(30, config.settings.CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS))

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(heartbeat_interval):
            conv.touch_turn_worker(turn_id, worker_id)

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        daemon=True,
        name=f"turn-heartbeat-{turn_id[:8]}",
    )
    heartbeat_thread.start()
    start = time.monotonic()
    started_at = datetime.now(timezone.utc)
    answer_parts: list[str] = []
    first_token_ms: int | None = None
    stage_started: dict[str, float] = {}
    stage_durations_ms: dict[str, list[int]] = {}

    def cancelled() -> bool:
        return cancel.is_set() or conv.is_turn_cancel_requested(turn_id)

    def emit(event_type: str, payload: dict) -> None:
        conv.touch_turn_worker(turn_id, worker_id)
        conv.append_turn_event(turn_id, event_type, payload)

    def emit_stage(key: str, label: str, status: str = "running", detail: str = "") -> None:
        now = time.monotonic()
        if status == "running":
            stage_started.setdefault(key, now)
        elif status in {"done", "error"} and key in stage_started:
            stage_durations_ms.setdefault(key, []).append(int((now - stage_started.pop(key)) * 1000))
        emit("stage", {"key": key, "label": label, "status": status, "detail": detail})

    def metrics(*, failed: bool = False, cancelled: bool = False, summary: dict | None = None) -> dict:
        queue_ms = max(0, int((started_at - _parse_iso_time(turn.created_at)).total_seconds() * 1000))
        tool_latencies = [
            {
                "tool": item.get("tool_name"),
                "latency_ms": item.get("latency_ms"),
                "status": item.get("status"),
                "hit_count": item.get("hit_count"),
            }
            for item in (summary or {}).get("tool_diagnostics") or []
        ]
        return {
            "query_mode": turn.query_mode,
            "queue_ms": queue_ms,
            "first_token_ms": first_token_ms,
            "total_ms": int((time.monotonic() - start) * 1000),
            "stage_durations_ms": stage_durations_ms,
            "tool_calls": tool_latencies,
            "failed": failed,
            "cancelled": cancelled,
        }

    summary: dict = {}
    footer = ""
    try:
        history = conv.history_before_turn(user.id, turn_id)
        if turn.kb_name in ("", GENERAL_CHAT_KB_NAME):
            llm = LLMClient()
            for delta in llm.stream_chat(_general_messages(history, turn.query), usage_stage="general_chat"):
                if cancelled():
                    break
                if delta:
                    if first_token_ms is None:
                        first_token_ms = int((time.monotonic() - start) * 1000)
                    answer_parts.append(delta)
                    emit("delta", {"text": delta})
            summary = {"retriever_type": "direct", "final_top_k": 0, "evidence": []}
        else:
            if pipeline is None:
                raise RuntimeError("query pipeline is unavailable")
            emit_stage("permission", "权限校验", "done", "已完成访问范围检查")
            gen = _query_generator(
                pipeline,
                turn.query,
                turn.kb_name,
                history,
                ctx,
                str(turn.session_id),
                emit_stage,
                turn.query_mode,
                cancelled,
            )
            try:
                for chunk in gen:
                    if cancelled():
                        break
                    if chunk:
                        if first_token_ms is None:
                            first_token_ms = int((time.monotonic() - start) * 1000)
                        answer_parts.append(chunk)
                        emit("delta", {"text": chunk})
            finally:
                if cancelled():
                    close = getattr(gen, "close", None)
                    if callable(close):
                        close()
            summary = pipeline.get_last_retrieval_summary() or {}
            footer = pipeline.get_last_agent_footer() or ""

        if cancelled():
            emit("error", {"message": "已停止生成", "cancelled": True})
            conv.fail_turn(user.id, turn_id, "已停止生成", cancelled=True)
            return

        answer = "".join(answer_parts)
        if not answer:
            raise RuntimeError("未生成回答")
        summary = {**summary, "query_mode": turn.query_mode}
        completed = conv.complete_turn(user.id, turn_id, answer, summary, footer, metrics=metrics(summary=summary))
        if completed is None:
            # Status guard rejected the update (e.g. cancel was requested
            # concurrently); the cancellation already won — surface it.
            conv.fail_turn(user.id, turn_id, "已停止生成", cancelled=True)
            emit("error", {"message": "已停止生成", "cancelled": True})
            _record_query_trace(
                user=user,
                kb_name="" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name,
                original_query=turn.query,
                thread_id=str(turn.session_id),
                summary=summary,
                latency_ms=int((time.monotonic() - start) * 1000),
                status="cancelled",
                error_message="已停止生成",
            )
            return
        emit_stage("generate", "生成回答", "done", "最终回答已生成")
        emit(
            "done",
            {
                "turn": _turn_view(completed).model_dump(),
                "answer": answer,
                "summary": summary,
                "footer": footer,
            },
        )
        # Derive the trace status from the answer text + retrieval summary
        # (same helper Streamlit/the old inline /query path used) instead of
        # hardcoding "success" -- a failed/partial/no-evidence response is then
        # logged accurately in the log center.
        trace_status, trace_error = query_trace_status(answer, summary)
        _record_query_trace(
            user=user,
            kb_name="" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name,
            original_query=turn.query,
            thread_id=str(turn.session_id),
            summary=summary,
            latency_ms=int((time.monotonic() - start) * 1000),
            status=trace_status,
            error_message=trace_error,
        )
    except Exception as exc:
        if cancelled():
            emit("error", {"message": "已停止生成", "cancelled": True})
            conv.fail_turn(user.id, turn_id, "已停止生成", cancelled=True)
        else:
            conv.fail_turn(user.id, turn_id, str(exc))
            emit("error", {"message": str(exc)})
            _record_query_trace(
                user=user,
                kb_name="" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name,
                original_query=turn.query,
                thread_id=str(turn.session_id),
                summary=summary,
                latency_ms=int((time.monotonic() - start) * 1000),
                status="failed",
                error_message=str(exc),
            )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
        _clear_turn_cancel_signal(turn_id)


@router.post("/conversations/{session_id}/turns", response_model=TurnStartResponse, status_code=201)
def create_turn(
    session_id: int,
    body: CreateTurnRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    session = conv.get_session(user.id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    ctx = build_context_for_user(user, session.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if session.kb_name != GENERAL_CHAT_KB_NAME and not ctx.has_kb_permission(session.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    query_mode = "fast" if session.kb_name == GENERAL_CHAT_KB_NAME else "deep"
    turn = conv.create_turn(user.id, session_id, body.query, body.client_request_id, query_mode)
    messages = conv.list_messages(user.id, session_id)
    user_message = next((message for message in messages if message.id == turn.user_message_id), None)
    if user_message is None:
        raise HTTPException(status_code=500, detail="turn message was not persisted")
    return TurnStartResponse(turn=_turn_view(turn), user_message=_message_view(user_message))


@router.post("/turns/{turn_id}/start", response_model=TurnView, status_code=202)
def start_turn(
    turn_id: str,
    request: Request,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    turn = conv.get_turn(user.id, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    ctx = build_context_for_user(user, turn.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if turn.kb_name != GENERAL_CHAT_KB_NAME and not ctx.has_kb_permission(turn.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    # Worker polling owns execution. This endpoint remains idempotent so the
    # browser can safely call it after a refresh to ensure the turn is queued.
    # A dependency-overridden pipeline is only used by in-process API tests;
    # production never enters this branch and always relies on the worker.
    if request.app.dependency_overrides.get(get_pipeline) is not None and turn.status in {"pending", "streaming", "cancelling"}:
        pipeline = None if turn.kb_name == GENERAL_CHAT_KB_NAME else _resolve_pipeline(request)
        threading.Thread(
            target=_run_turn,
            kwargs={"turn_id": turn.id, "user": user, "ctx": ctx, "pipeline": pipeline},
            daemon=True,
            name=f"test-chat-turn-{turn.id[:8]}",
        ).start()
    return _turn_view(turn)


@router.get("/turns/{turn_id}", response_model=TurnView)
def get_turn(
    turn_id: str,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    turn = conv.get_turn(user.id, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    return _turn_view(turn)


@router.get("/conversations/{session_id}/turns", response_model=list[TurnView])
def list_active_turns(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    session = conv.get_session(user.id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    ctx = build_context_for_user(user, session.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if session.kb_name != GENERAL_CHAT_KB_NAME and not ctx.has_kb_permission(session.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    return [_turn_view(turn) for turn in conv.list_active_turns(user.id, session_id)]


@router.post("/turns/{turn_id}/cancel", response_model=TurnView)
def cancel_turn(
    turn_id: str,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    turn = conv.request_turn_cancel(user.id, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    # Do not create a process-local signal in an API process that does not own
    # the worker. The DB flag above is the cross-process cancellation signal.
    local_signal = _existing_turn_cancel_signal(turn_id)
    if local_signal is not None:
        local_signal.set()
    return _turn_view(turn)


@router.get("/turns/{turn_id}/events")
async def stream_turn_events(
    turn_id: str,
    request: Request,
    after: int = 0,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    turn = conv.get_turn(user.id, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    try:
        last_seq = max(after, int(request.headers.get("Last-Event-ID") or 0))
    except ValueError:
        last_seq = max(0, after)

    async def event_stream():
        nonlocal last_seq
        terminal_idle_polls = 0
        while True:
            events = conv.list_turn_events(user.id, turn_id, last_seq)
            for event in events:
                last_seq = event.seq
                yield _sse(event.event_type, event.payload, event.seq)
            current = conv.get_turn(user.id, turn_id)
            if current is not None and conv.is_turn_worker_stale(user.id, turn_id):
                # A live standalone worker requeues stale claims on its next
                # poll. If no worker exists, close this subscription with a
                # durable terminal error instead of infinite keepalives.
                conv.append_turn_event(turn_id, "error", {"message": "任务执行器失去心跳，请重新提交问题", "worker_lost": True})
                conv.fail_turn(user.id, turn_id, "任务执行器失去心跳")
                continue
            if current is None or current.status in _TERMINAL_TURN_STATUSES:
                # ``complete_turn`` persists the terminal state before the
                # final SSE event. Give the worker a short grace window so a
                # subscriber cannot miss ``done`` in that tiny transaction gap.
                terminal_idle_polls += 1
                if terminal_idle_polls >= 3:
                    break
                await asyncio.sleep(0.1)
                continue
            terminal_idle_polls = 0
            if await request.is_disconnected():
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


def _resolve_pipeline(request: Request) -> AppPipeline:
    provider = request.app.dependency_overrides.get(get_pipeline)
    if provider is not None:
        return provider()
    return get_pipeline()


def _query_generator(
    pipeline,
    query,
    kb_name,
    history,
    ctx,
    thread_id,
    progress_callback,
    query_mode="deep",
    should_cancel=None,
):
    params = inspect.signature(pipeline.query).parameters
    if "progress_callback" in params:
        kwargs = {
            "agent_thread_id": thread_id,
            "progress_callback": progress_callback,
        }
        if "query_mode" in params:
            kwargs["query_mode"] = query_mode
        if "should_cancel" in params:
            kwargs["should_cancel"] = should_cancel
        return pipeline.query(
            query,
            kb_name,
            history,
            ctx,
            **kwargs,
        )
    kwargs = {"agent_thread_id": thread_id}
    if "query_mode" in params:
        kwargs["query_mode"] = query_mode
    if "should_cancel" in params:
        kwargs["should_cancel"] = should_cancel
    return pipeline.query(query, kb_name, history, ctx, **kwargs)


def _parse_iso_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


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
