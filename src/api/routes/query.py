from __future__ import annotations

import asyncio
import json
import queue
import inspect
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Callable, Generator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

import config.settings
from src.core.app_logs import AppLogService, query_trace_status
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.core.conversation import (
    GENERAL_CHAT_KB_NAME,
    ChatMessage,
    ChatTurn,
    ConversationService,
)
from src.core.model_factory import create_chat_model
from src.observability import (
    current_trace_identity,
    extract_trace_context,
    inject_trace_context,
    observe,
    start_thread_with_current_context,
)
from src.observability.metrics import record_chat_turn

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import CreateTurnRequest, MessageView, QueryRequest, TurnStartResponse, TurnView

router = APIRouter(tags=["query"])
_TURN_CANCEL_SIGNALS: dict[str, threading.Event] = {}
_TURN_CANCEL_LOCK = threading.Lock()
_TERMINAL_TURN_STATUSES = {"completed", "cancelled", "failed"}

# In-process push channel: worker -> SSE subscribers. The DB remains the
# durable/replay source of truth; this queue only removes the polling delay
# for the common single-process deployment. Cross-process workers simply
# produce no pushes and subscribers fall back to DB polling.
_TURN_EVENT_QUEUES: dict[str, list["queue.Queue"]] = {}
_TURN_EVENT_QUEUES_LOCK = threading.Lock()

_DELTA_FLUSH_CHARS = 240
_DELTA_FLUSH_SECONDS = 0.12


def _publish_turn_event(turn_id: str, seq: int | None, event_type: str, payload: dict) -> None:
    with _TURN_EVENT_QUEUES_LOCK:
        queues = list(_TURN_EVENT_QUEUES.get(turn_id) or ())
    for q in queues:
        try:
            q.put_nowait((seq, event_type, payload))
        except queue.Full:
            pass


def _subscribe_turn_events(turn_id: str) -> tuple["queue.Queue", Callable[[], None]]:
    q: queue.Queue = queue.Queue(maxsize=2000)
    with _TURN_EVENT_QUEUES_LOCK:
        _TURN_EVENT_QUEUES.setdefault(turn_id, []).append(q)

    def unsubscribe() -> None:
        with _TURN_EVENT_QUEUES_LOCK:
            queues = _TURN_EVENT_QUEUES.get(turn_id)
            if queues and q in queues:
                queues.remove(q)
            if queues is not None and not queues:
                _TURN_EVENT_QUEUES.pop(turn_id, None)

    return q, unsubscribe


def _sse(event: str, data: dict, event_id: int | None = None) -> str:
    # default=str keeps token_usage / trace objects serialisable without shaping them here.
    payload = json.dumps(data, ensure_ascii=False, default=str)
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {payload}\n\n"


def _stage(key: str, label: str, status: str = "running", detail: str = "") -> tuple[str, dict]:
    return ("stage", {"key": key, "label": label, "status": status, "detail": detail})


def _general_chat_stream(messages: list[dict[str, str]], usage_out: dict | None = None) -> Generator[str, None, None]:
    """Stream a direct (no-retrieval) chat completion via the shared model factory.

    ``usage_out`` optionally receives aggregated token usage after the stream.
    """
    model = create_chat_model()
    for chunk in model.stream(messages):
        content = getattr(chunk, "content", "")
        if isinstance(content, list):
            content = "".join(
                str(block.get("text") or "") for block in content if isinstance(block, dict)
            )
        if content:
            yield content
        meta = getattr(chunk, "usage_metadata", None)
        if usage_out is not None and meta:
            usage_out["call_count"] = int(usage_out.get("call_count") or 0) + 1
            usage_out["prompt_tokens"] = int(usage_out.get("prompt_tokens") or 0) + int(meta.get("input_tokens") or 0)
            usage_out["completion_tokens"] = int(usage_out.get("completion_tokens") or 0) + int(
                meta.get("output_tokens") or 0
            )
            usage_out["total_tokens"] = int(usage_out.get("total_tokens") or 0) + int(meta.get("total_tokens") or 0)


def _make_event_callback(emit_stage, emit):
    """Adapt the runner's unified ``{"type","payload"}`` event stream to the
    durable turn-event path: route ``stage`` through the timing-aware emit_stage,
    and everything else (degraded / tool_result) through emit.

    ``thought`` (model reasoning) is deliberately dropped here — it is
    display-only and never persisted nor replayed (OpenWorker's
    ``reasoning_delta`` semantics). The SSE layer polls the DB, so leaving it
    out of ``turn_events`` is what makes it ephemeral end-to-end.
    """

    def _on_event(evt: dict) -> None:
        etype = evt.get("type")
        if etype == "thought":
            return
        if etype == "stage":
            payload = evt.get("payload") or {}
            emit_stage(
                payload.get("key", ""),
                payload.get("label", ""),
                payload.get("status", "running"),
                payload.get("detail", ""),
            )
        else:
            emit(etype, evt.get("payload") or {})

    return _on_event


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
    turn_observation = observe.chain(
        "hdb.chat.turn",
        context=extract_trace_context(turn.trace_context),
        **{
            "hdb.turn.id": turn.id,
            "hdb.session.id": str(turn.session_id),
            "session.id": str(turn.session_id),
            "user.id": str(user.id),
            "hdb.query.mode": turn.query_mode,
            "hdb.query.source": "durable_worker",
        },
    ).start()
    turn_observation.set_input(turn.query, content_kind="query")
    cancel = _turn_cancel_signal(turn_id)
    heartbeat_stop = threading.Event()
    heartbeat_interval = max(2, min(30, config.settings.CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS))

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(heartbeat_interval):
            conv.touch_turn_worker(turn_id, worker_id)

    heartbeat_thread = start_thread_with_current_context(
        heartbeat_loop,
        daemon=True,
        name=f"turn-heartbeat-{turn_id[:8]}",
    )
    start = time.monotonic()
    started_at = datetime.now(timezone.utc)
    answer_parts: list[str] = []
    first_token_ms: int | None = None
    stage_started: dict[str, float] = {}
    stage_durations_ms: dict[str, list[int]] = {}
    token_usage_summary = None
    outcome_status = "failed"

    def cancelled() -> bool:
        return cancel.is_set() or conv.is_turn_cancel_requested(turn_id)

    # Delta batching: consecutive answer tokens are merged into one durable
    # turn_event (and one push) so per-token DB writes don't throttle the
    # stream. Flush triggers: size, time window, or any non-delta event.
    _delta_lock = threading.Lock()
    _delta_text_parts: list[str] = []
    _delta_first_at: list[float] = []  # single-element "box"

    def _flush_deltas() -> None:
        with _delta_lock:
            if not _delta_text_parts:
                return
            text = "".join(_delta_text_parts)
            _delta_text_parts.clear()
            _delta_first_at.clear()
        event = conv.append_turn_event(turn_id, "delta", {"text": text})
        _publish_turn_event(turn_id, event.seq, "delta", {"text": text})

    def emit(event_type: str, payload: dict) -> None:
        conv.touch_turn_worker(turn_id, worker_id)
        if event_type == "delta":
            now = time.monotonic()
            flush = False
            with _delta_lock:
                _delta_text_parts.append(str(payload.get("text") or ""))
                if not _delta_first_at:
                    _delta_first_at.append(now)
                total_chars = sum(len(part) for part in _delta_text_parts)
                flush = (
                    total_chars >= _DELTA_FLUSH_CHARS
                    or (now - _delta_first_at[0]) >= _DELTA_FLUSH_SECONDS
                )
            if flush:
                _flush_deltas()
            return
        _flush_deltas()
        event = conv.append_turn_event(turn_id, event_type, payload)
        _publish_turn_event(turn_id, event.seq, event_type, payload)

    def emit_stage(key: str, label: str, status: str = "running", detail: str = "") -> None:
        now = time.monotonic()
        if status == "running":
            stage_started.setdefault(key, now)
        elif status in {"done", "error"} and key in stage_started:
            stage_durations_ms.setdefault(key, []).append(int((now - stage_started.pop(key)) * 1000))
        emit("stage", {"key": key, "label": label, "status": status, "detail": detail})

    # Structural boundary: marks the start of a turn so surfaces can group the
    # whole user-message → answer span as one unit (OpenWorker's turn_start).
    emit("turn_start", {"query": turn.query, "query_mode": turn.query_mode})

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
            usage_acc: dict = {}
            for delta in _general_chat_stream(_general_messages(history, turn.query), usage_out=usage_acc):
                if cancelled():
                    break
                if delta:
                    if first_token_ms is None:
                        first_token_ms = int((time.monotonic() - start) * 1000)
                    answer_parts.append(delta)
                    emit("delta", {"text": delta})
            summary = {"retriever_type": "direct", "final_top_k": 0, "evidence": [], "token_usage": usage_acc}
            token_usage_summary = usage_acc
        else:
            if pipeline is None:
                raise RuntimeError("query pipeline is unavailable")
            gen = _query_generator(
                pipeline,
                turn.query,
                turn.kb_name,
                history,
                ctx,
                str(turn.session_id),
                _make_event_callback(emit_stage, emit),
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
            token_usage_summary = pipeline.get_last_token_usage_summary()

        _flush_deltas()
        if cancelled():
            emit("error", {"message": "已停止生成", "cancelled": True})
            conv.fail_turn(user.id, turn_id, "已停止生成", cancelled=True)
            emit("turn_end", {"status": "cancelled"})
            outcome_status = "cancelled"
            return

        answer = "".join(answer_parts)
        if not answer:
            raise RuntimeError("未生成回答")
        summary = {**summary, "query_mode": turn.query_mode}
        completed = conv.complete_turn(user.id, turn_id, answer, summary, footer, metrics=metrics(summary=summary))
        turn_observation.set_output(answer, content_kind="llm")
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
        emit("turn_end", {"status": "completed"})
        outcome_status = "completed"
        _record_query_trace(
            user=user,
            kb_name="" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name,
            original_query=turn.query,
            thread_id=str(turn.session_id),
            summary=summary,
            latency_ms=int((time.monotonic() - start) * 1000),
            status="success",
            turn_id=turn_id,
        )
    except Exception as exc:
        turn_observation.error(exc)
        if cancelled():
            emit("error", {"message": "已停止生成", "cancelled": True})
            conv.fail_turn(user.id, turn_id, "已停止生成", cancelled=True)
            outcome_status = "cancelled"
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
                turn_id=turn_id,
            )
        emit("turn_end", {"status": "failed"})
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
        duration_s = max(0.0, time.monotonic() - start)
        queue_s = max(0.0, (started_at - _parse_iso_time(turn.created_at)).total_seconds())
        record_chat_turn(
            status=outcome_status,
            mode=turn.query_mode,
            duration_s=duration_s,
            queue_s=queue_s,
            ttft_s=(first_token_ms / 1000.0) if first_token_ms is not None else None,
        )
        turn_observation.set("hdb.chat.status", outcome_status)
        turn_observation.set("hdb.chat.latency_ms", int(duration_s * 1000))
        turn_observation.set("hdb.chat.queue_ms", int(queue_s * 1000))
        turn_observation.set_token_usage(token_usage_summary)
        if first_token_ms is not None:
            turn_observation.set("hdb.chat.ttft_ms", int(first_token_ms))
        turn_observation.set("hdb.retrieval.rounds", int((summary or {}).get("retrieval_rounds") or 0))
        turn_observation.set("hdb.retrieval.calls", len((summary or {}).get("tool_diagnostics") or []))
        turn_observation.set(
            "hdb.retrieval.hits",
            sum(int(item.get("hit_count") or 0) for item in (summary or {}).get("tool_diagnostics") or []),
        )
        turn_observation.set("hdb.retrieval.final_top_k", int((summary or {}).get("final_top_k") or 0))
        turn_observation.set("hdb.retriever.type", (summary or {}).get("retriever_type") or "direct")
        turn_observation.set("hdb.evidence.count", len((summary or {}).get("evidence") or []))
        if (summary or {}).get("rewritten_queries"):
            turn_observation.set(
                "hdb.query.rewritten",
                json.dumps((summary or {}).get("rewritten_queries"), ensure_ascii=False),
            )
        if answer_parts and outcome_status != "completed":
            turn_observation.set_output("".join(answer_parts), content_kind="llm")
        turn_observation.outcome(outcome_status)
        turn_observation.set("hdb.agent.retrieval_round", int((summary or {}).get("retrieval_rounds") or 0))
        turn_observation.end()
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
    turn = conv.create_turn(
        user.id,
        session_id,
        body.query,
        body.client_request_id,
        query_mode,
        trace_context=inject_trace_context(),
    )
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
        start_thread_with_current_context(
            _run_turn,
            turn_id=turn.id,
            user=user,
            ctx=ctx,
            pipeline=pipeline,
            daemon=True,
            name=f"test-chat-turn-{turn.id[:8]}",
        )
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
        q, unsubscribe = _subscribe_turn_events(turn_id)
        db_synced = False  # initial pass: replay anything persisted before we subscribed
        terminal_idle_polls = 0

        def _drain_db_events() -> list:
            return conv.list_turn_events(user.id, turn_id, last_seq)

        try:
            while True:
                # Drain the in-process push channel first (sub-ms delivery).
                pushed = 0
                try:
                    while True:
                        seq, event_type, payload = q.get_nowait()
                        if seq is not None and seq <= last_seq:
                            continue
                        if seq is None:  # cross-process safety: force DB sync
                            db_synced = False
                            continue
                        last_seq = seq
                        pushed += 1
                        yield _sse(event_type, payload, seq)
                except queue.Empty:
                    pass
                if not db_synced:
                    for event in _drain_db_events():
                        last_seq = event.seq
                        yield _sse(event.event_type, event.payload, event.seq)
                    db_synced = True
                    continue

                current = conv.get_turn(user.id, turn_id)
                if current is not None and conv.is_turn_worker_stale(user.id, turn_id):
                    # A live standalone worker requeues stale claims on its next
                    # poll. If no worker exists, close this subscription with a
                    # durable terminal error instead of infinite keepalives.
                    conv.append_turn_event(
                        turn_id, "error", {"message": "任务执行器失去心跳，请重新提交问题", "worker_lost": True}
                    )
                    conv.fail_turn(user.id, turn_id, "任务执行器失去心跳")
                    continue
                if current is None or current.status in _TERMINAL_TURN_STATUSES:
                    # Deliver everything persisted so far before deciding to
                    # close, including events written by the stale-worker
                    # branch above or by complete_turn.
                    for event in _drain_db_events():
                        last_seq = event.seq
                        yield _sse(event.event_type, event.payload, event.seq)
                    # ``complete_turn`` persists the terminal state before the
                    # final SSE event. Give the worker a short grace window so a
                    # subscriber cannot miss ``done`` in that tiny transaction gap.
                    terminal_idle_polls += 1
                    if terminal_idle_polls >= 3:
                        break
                    await asyncio.sleep(0.05)
                    continue
                terminal_idle_polls = 0
                if await request.is_disconnected():
                    break
                if not pushed:
                    # No pushes available (cross-process worker or idle turn):
                    # fall back to a short-interval DB poll, then keepalive.
                    events = _drain_db_events()
                    if events:
                        for event in events:
                            last_seq = event.seq
                            yield _sse(event.event_type, event.payload, event.seq)
                        continue
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.4)
        finally:
            unsubscribe()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/query")
async def query(
    body: QueryRequest,
    request: Request,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    if body.kb_name in ("", GENERAL_CHAT_KB_NAME):
        ctx = build_context_for_user(user, body.kb_name, auth=auth)
        reject_system_admin_kb_access(ctx)
        return StreamingResponse(
            _direct_event_stream(user=user, body=body),
            media_type="text/event-stream",
        )

    ctx = build_context_for_user(user, body.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(body.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")

    pipeline = _resolve_pipeline(request)
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
        observation = observe.chain(
            "hdb.chat.turn",
            **{
                "hdb.session.id": body.thread_id,
                "session.id": body.thread_id,
                "hdb.query.mode": "deep",
                "hdb.query.source": "legacy_api",
            },
        ).start()
        observation.set_input(body.query, content_kind="query")
        outcome_status = "failed"
        try:
            answer_parts: list[str] = []
            gen = _query_generator(
                pipeline,
                body.query,
                body.kb_name,
                history,
                ctx,
                body.thread_id,
                lambda e: _put((e["type"], e.get("payload") or {})),
                "deep",
                None,
            )
            for chunk in gen:
                if cancel.is_set():
                    break
                if chunk:
                    answer_parts.append(chunk)
                    _put(("delta", {"text": chunk}))
            summary = pipeline.get_last_retrieval_summary() or {}
            answer = "".join(answer_parts)
            observation.set_output(answer, content_kind="llm")
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
            outcome_status = "cancelled" if cancel.is_set() else (
                "completed" if trace_status == "success" else trace_status
            )
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
            observation.error(exc)
            if not cancel.is_set():
                _put(_stage("generate", "生成回答", "error", "答案生成失败"))
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
            duration_ms = int((time.monotonic() - start) * 1000)
            record_chat_turn(
                status=outcome_status,
                mode="deep",
                duration_s=max(0.0, duration_ms / 1000.0),
                queue_s=0.0,
                ttft_s=None,
            )
            observation.set("hdb.chat.status", outcome_status)
            observation.set("hdb.chat.latency_ms", duration_ms)
            observation.set("hdb.chat.queue_ms", 0)
            observation.set_token_usage(pipeline.get_last_token_usage_summary())
            observation.set("hdb.retrieval.rounds", int(summary.get("retrieval_rounds") or 0))
            diagnostics = summary.get("tool_diagnostics") or []
            observation.set("hdb.retrieval.calls", len(diagnostics))
            observation.set(
                "hdb.retrieval.hits",
                sum(int(item.get("hit_count") or 0) for item in diagnostics),
            )
            observation.set("hdb.retrieval.final_top_k", int(summary.get("final_top_k") or 0))
            observation.set("hdb.retriever.type", summary.get("retriever_type") or "multi_source_agent")
            observation.set("hdb.retrieval.status", summary.get("status") or "")
            observation.set("hdb.evidence.count", len(summary.get("evidence") or []))
            if summary.get("rewritten_queries"):
                observation.set(
                    "hdb.query.rewritten",
                    json.dumps(summary.get("rewritten_queries"), ensure_ascii=False),
                )
            if answer_parts and outcome_status != "completed":
                observation.set_output("".join(answer_parts), content_kind="llm")
            observation.outcome(outcome_status)
            observation.end()
            q.put(sentinel)

    async def event_stream():
        thread = start_thread_with_current_context(producer, daemon=True, name="api-query-producer")
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
    event_callback,
    query_mode="deep",
    should_cancel=None,
):
    params = inspect.signature(pipeline.query).parameters
    if "event_callback" in params:
        kwargs = {
            "agent_thread_id": thread_id,
            "event_callback": event_callback,
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


def _direct_event_stream(*, user: AuthUser, body: QueryRequest):
    q: queue.Queue = queue.Queue(maxsize=64)
    sentinel = object()
    cancel = threading.Event()

    def _put(item) -> None:
        while not cancel.is_set():
            try:
                q.put(item, timeout=0.5)
                return
            except queue.Full:
                continue

    def producer() -> None:
        start = time.monotonic()
        answer_parts: list[str] = []
        usage_summary: dict = {}
        observation = observe.chain(
            "hdb.chat.turn",
            **{
                "hdb.session.id": body.thread_id,
                "session.id": body.thread_id,
                "hdb.query.mode": "fast",
                "hdb.query.source": "direct_api",
                "hdb.retriever.type": "direct",
            },
        ).start()
        observation.set_input(body.query, content_kind="query")
        outcome_status = "failed"
        try:
            for delta in _general_chat_stream(_general_messages(body.history, body.query), usage_out=usage_summary):
                if cancel.is_set():
                    break
                if delta:
                    answer_parts.append(delta)
                    q.put(("delta", {"text": delta}))
            answer = "".join(answer_parts)
            summary = {"retriever_type": "direct", "final_top_k": 0, "evidence": []}
            observation.set_output(answer, content_kind="llm")
            if not cancel.is_set():
                _put(
                    (
                        "done",
                        {
                            "answer": answer,
                            "summary": summary,
                            "footer": "",
                            "token_usage": _jsonable_usage(usage_summary),
                        },
                    )
                )
            _record_query_trace(
                user=user,
                kb_name="",
                original_query=body.query,
                thread_id=body.thread_id,
                summary=summary,
                latency_ms=int((time.monotonic() - start) * 1000),
                status="failed" if cancel.is_set() else "success",
                error_message="client disconnected" if cancel.is_set() else "",
            )
            outcome_status = "cancelled" if cancel.is_set() else "completed"
        except Exception as exc:
            observation.error(exc)
            if not cancel.is_set():
                _put(_stage("generate", "生成通用回答", "error", "通用回答生成失败"))
                q.put(("error", {"message": str(exc)}))
            _record_query_trace(
                user=user,
                kb_name="",
                original_query=body.query,
                thread_id=body.thread_id,
                summary={"retriever_type": "direct", "final_top_k": 0, "evidence": []},
                latency_ms=int((time.monotonic() - start) * 1000),
                status="failed",
                error_message=str(exc),
            )
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            record_chat_turn(
                status=outcome_status,
                mode="fast",
                duration_s=max(0.0, duration_ms / 1000.0),
                queue_s=0.0,
                ttft_s=None,
            )
            observation.set("hdb.chat.status", outcome_status)
            observation.set("hdb.chat.latency_ms", duration_ms)
            observation.set("hdb.chat.queue_ms", 0)
            observation.set("hdb.retrieval.rounds", 0)
            observation.set("hdb.retrieval.calls", 0)
            observation.set("hdb.retrieval.hits", 0)
            observation.set("hdb.evidence.count", 0)
            observation.set_token_usage(usage_summary)
            observation.outcome(outcome_status)
            observation.end()
            q.put(sentinel)

    def iterator():
        thread = start_thread_with_current_context(producer, daemon=True, name="api-direct-query-producer")
        try:
            while True:
                item = q.get()
                if item is sentinel:
                    break
                event, payload = item
                yield _sse(event, payload)
        finally:
            cancel.set()
            thread.join(timeout=2.0)

    return iterator()


def _jsonable_usage(value):
    if is_dataclass(value):
        return asdict(value)
    return value


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
    turn_id: str = "",
) -> None:
    """Persist a query trace + retrieved evidence, fail-soft. Mirrors the
    Streamlit UI's post-stream logging so the API path is equally observable
    in the log center (query logs + evidence drill-down)."""
    try:
        log_service = AppLogService()
        rewritten_query = " | ".join(summary.get("rewritten_queries") or [])[:500]
        trace_identity = current_trace_identity()
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
            otel_trace_id=trace_identity.trace_id,
            otel_span_id=trace_identity.span_id,
            turn_id=turn_id,
        )
        log_service.record_retrieved_evidence(trace_id, summary.get("evidence") or [])
    except Exception as trace_error:
        # Trace logging must never break the answer stream.
        from src.core.logger import log as _log

        _log(f"API query trace logging failed: {trace_error}")
