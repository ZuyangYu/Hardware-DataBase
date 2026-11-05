from __future__ import annotations

import asyncio
import json
import queue
import inspect
from src.services.knowledge_scope_access import resolve_session_scope, can_read_turn
from src.services.knowledge_scope import KnowledgeScopeDenied, KnowledgeScopeInvalid, KnowledgeScopeChanged, KnowledgeScopeResolver
from src.services.knowledge_scope_models import KnowledgeScopeSnapshot
from src.services.knowledge_scope_persistence import IdempotencyConflict
from src.agents.tools.retrieval_executor import MultiKnowledgeRetrievalUnavailable
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import src.settings
from src.agents.runner import strip_narration_segments
from src.core.app_logs import AppLogService, query_trace_status
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.costs.context import attributed, update_context
from src.core.conversation import (
    GENERAL_CHAT_KB_NAME,
    ChatMessage,
    ChatTurn,
    ConversationService,
    StaleTurnOwner,
)
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
from src.api.schemas import (
    AttachmentSnapshotView,
    CreateTurnRequest,
    MessageView,
    TurnStartResponse,
    TurnView,
)


def _can_read_turn(auth, user, turn, conv: ConversationService) -> bool:
    return can_read_turn(
        auth,
        user,
        turn,
        dependency_loader=lambda inherited_id: conv.get_turn(user.id, str(inherited_id)),
    )

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
_SHORT_TERM_HISTORY_LIMIT = 5






def _publish_turn_event(turn_id: str, seq: int | None, event_type: str, payload: dict) -> None:
    with _TURN_EVENT_QUEUES_LOCK:
        queues = list(_TURN_EVENT_QUEUES.get(turn_id) or ())
    for q in queues:
        try:
            q.put_nowait((seq, event_type, payload))
        except queue.Full:
            pass
    if seq is not None:
        # Cross-process SSE wake-up (v3 §10); PG remains the replay truth.
        try:
            from src.core.redis_events import publish_turn_event

            publish_turn_event(turn_id, seq, event_type, payload)
        except Exception:
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


def _short_term_memory_summary(
    history: list[tuple[str, str]],
    *,
    source: str,
    status: str | None = None,
) -> dict[str, object]:
    """Return privacy-safe observability for the bounded chat history window.

    Short-term memory is the canonical conversation history, not the semantic
    MemoryService. Only counts and the source path are recorded; prior user or
    assistant text is never copied into an event or metric.
    """
    turn_count = len(history)
    message_count = sum(
        1
        for user_text, assistant_text in history
        for text in (user_text, assistant_text)
        if str(text or "").strip()
    )
    return {
        "source": source,
        "status": status or ("hit" if turn_count > 0 else "empty"),
        "hit": turn_count > 0,
        "hit_count": turn_count,
        "turn_count": turn_count,
        "message_count": message_count,
        "window_turns": _SHORT_TERM_HISTORY_LIMIT,
    }


def _set_short_term_memory_observation(observation, summary: dict[str, object]) -> None:
    """Attach low-cardinality short-term-memory fields to a turn span."""
    observation.set("hdb.chat.short_term_memory.status", str(summary.get("status") or ""))
    observation.set("hdb.chat.short_term_memory.hit", bool(summary.get("hit")))
    observation.set("hdb.chat.short_term_memory.hit_count", int(summary.get("hit_count") or 0))
    observation.set("hdb.chat.short_term_memory.turn_count", int(summary.get("turn_count") or 0))
    observation.set("hdb.chat.short_term_memory.message_count", int(summary.get("message_count") or 0))
    observation.set("hdb.chat.short_term_memory.window_turns", int(summary.get("window_turns") or 0))


def _make_event_callback(emit_stage, emit):
    """Adapt the runner's unified ``{"type","payload"}`` event stream to the
    durable turn-event path: route ``stage`` through the timing-aware emit_stage,
    and everything else (degraded / tool_result / short-term-memory) through emit.

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
            # Non-core payload keys are forwarded so durable/SSE consumers
            # keep the full routing provenance instead of only
            # key/label/status/detail.
            extra = {
                extra_key: extra_value
                for extra_key, extra_value in payload.items()
                if extra_key not in {"key", "label", "status", "detail"}
            }
            emit_stage(
                payload.get("key", ""),
                payload.get("label", ""),
                payload.get("status", "running"),
                payload.get("detail", ""),
                **extra,
            )
        else:
            emit(etype, evt.get("payload") or {})

    return _on_event




def _conv_service() -> ConversationService:
    return ConversationService()


def _attachment_snapshot_view(snapshot: dict) -> AttachmentSnapshotView:

    return AttachmentSnapshotView(
        attachment_id=str(snapshot.get("attachment_id") or ""),
        filename=str(snapshot.get("filename_snapshot") or ""),
        media_type=str(snapshot.get("media_type_snapshot") or ""),
        usage_hint=str(snapshot.get("usage_hint_snapshot") or "reference"),
        content_hash=str(snapshot.get("content_hash_snapshot") or ""),
        ordinal=int(snapshot.get("ordinal") or 0),
        parse_status=str(snapshot.get("parse_status_snapshot") or "queued"),
        deleted=bool(snapshot.get("deleted")),
    )


def _turn_attachments_for(turn: ChatTurn) -> list[AttachmentSnapshotView]:
    if not getattr(turn, "required_attachment_ids", []):
        return []
    try:
        snapshots = _conv_service().list_turn_attachments(turn.id)
    except Exception:
        return []
    return [_attachment_snapshot_view(snapshot) for snapshot in snapshots]


def _turn_queue_context(turn: ChatTurn) -> dict:
    """Queue visibility fields; terminal turns never need an extra query."""
    if turn.status in {"completed", "failed", "cancelled"}:
        return {}
    try:
        return _conv_service().turn_queue_context(turn.id)
    except Exception:
        return {}


def _turn_view(
    turn: ChatTurn,
    *,
    attachments: list[AttachmentSnapshotView] | None = None,
) -> TurnView:
    queue_context = _turn_queue_context(turn)
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
        source_scope=str(getattr(turn, "source_scope", "") or "auto"),
        attachments=attachments if attachments is not None else _turn_attachments_for(turn),
        blocked_by_turn_id=queue_context.get("blocked_by_turn_id"),
        queue_reason=str(queue_context.get("queue_reason") or ""),
        wait_deadline=queue_context.get("wait_deadline"),
        knowledge_scope=getattr(turn, "knowledge_scope", None),
        knowledge_selection_hash=getattr(turn, "knowledge_selection_hash", ""),
    )


def _restricted_turn_view(turn: ChatTurn) -> TurnView:
    from dataclasses import replace

    view = _turn_view(
        replace(
            turn,
            answer="",
            summary={},
            footer="",
            metrics={},
            knowledge_scope=None,
            attachments=[],
        ),
        attachments=[],
    )
    view.source_access_restricted = True
    return view


def _message_view(
    message: ChatMessage,
    *,
    attachments: list[AttachmentSnapshotView] | None = None,
) -> MessageView:
    return MessageView(
        id=message.id,
        session_id=message.session_id,
        turn_id=getattr(message, "turn_id", None),
        role=message.role,
        content=message.content,
        footer=message.footer,
        created_at=message.created_at,
        memory_context=getattr(message, "memory_context", []),
        attachments=attachments or [],
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


@attributed("chat", "chat", "turn_id")
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
    update_context(execution_attempt_id=str(getattr(turn, "attempt_id", "") or ""))
    # Execution ownership: a positive token means serialized mode and enables
    # stale-owner rejection for every later authoritative write of this turn.
    fencing_token = int(getattr(turn, "fencing_token", 0) or 0)
    session_generation = int(getattr(turn, "session_generation", 0) or 0)
    ownership_token: int | None = fencing_token if fencing_token > 0 else None
    # P0b: a chat turn holds one CHAT slot for its whole execution.  Queued
    # turns wait up to the chat deadline before failing with backpressure.
    chat_permit = None
    if bool(getattr(src.settings, "RESOURCE_GOVERNOR_ENABLED", True)):
        try:
            from src.core.concurrency import get_governor

            chat_permit = get_governor().acquire(
                "chat",
                interactive=True,
                timeout=float(getattr(src.settings, "CHAT_QUEUE_DEADLINE_SECONDS", 600)),
            )
        except Exception:
            try:
                conv.fail_turn(
                    user.id,
                    turn_id,
                    "系统繁忙，请稍后重试",
                    worker_id=worker_id,
                    fencing_token=ownership_token,
                )
            except StaleTurnOwner:
                pass
            return
    # P0a attempt isolation: the runner writes to an attempt-scoped checkpoint
    # thread; the visible thread only moves through the guarded commit below.
    visible_thread = str(turn.session_id)
    runner_thread = visible_thread
    checkpoint_initialization_failed = False
    checkpoint_observer = None
    knowledge_scope = None
    knowledge_scope_resolver = None
    knowledge_scope_error = None
    if getattr(turn, "knowledge_scope", None):
        try:
            knowledge_scope = KnowledgeScopeSnapshot.model_validate(turn.knowledge_scope)
            knowledge_scope_resolver = KnowledgeScopeResolver(AuthService())
            knowledge_scope_resolver.revalidate(user.id, knowledge_scope)
            # Worker adapters receive the same server-owned snapshot; they
            # must never reconstruct scope from the client query or the
            # anchor-only RequestContext.
            metadata = getattr(ctx, "metadata", None)
            if isinstance(metadata, dict):
                metadata["knowledge_scope_snapshot"] = knowledge_scope.model_dump(mode="json")
        except Exception as exc:
            # A persisted snapshot records what was authorized at creation;
            # it never grants access after a revoke or department change.
            knowledge_scope_error = exc

    def _register_checkpoint_ref(saved: dict) -> None:
        """Report one durable graph-state write to the authority transaction."""
        try:
            ref = {
                "backend": str(getattr(src.settings, "AGENT_CHECKPOINT_BACKEND", "sqlite")),
                "thread_id": str(saved.get("thread_id") or ""),
                "checkpoint_ns": str(saved.get("checkpoint_ns") or ""),
                "checkpoint_id": str(saved.get("checkpoint_id") or ""),
                "attempt_id": str(getattr(turn, "attempt_id", "") or ""),
                "fencing_token": int(fencing_token),
                "generation": int(session_generation),
            }
            conv.update_recoverable_checkpoint_ref(
                turn_id,
                worker_id,
                fencing_token,
                ref,
                expected_generation=session_generation if ownership_token else None,
            )
        except StaleTurnOwner:
            pass
        except Exception:
            pass

    if ownership_token:
        try:
            from src.agents.checkpoints import create_checkpointer, prepare_attempt

            resume_ref = None
            try:
                refs = conv.get_checkpoint_refs(int(turn.session_id))
                candidate = refs.get("recoverable")
                # Only a pointer from the same generation and an older attempt
                # may be resumed; cancellation advances the generation.
                if (
                    isinstance(candidate, dict)
                    and int(candidate.get("generation") or 0) == session_generation
                    and int(candidate.get("fencing_token") or 0) < fencing_token
                ):
                    resume_ref = candidate
            except Exception:
                resume_ref = None
            runner_thread = prepare_attempt(
                create_checkpointer(),
                visible_thread,
                fencing_token,
                attempt_id=str(getattr(turn, "attempt_id", "") or ""),
                resume_ref=resume_ref,
            )
        except Exception:
            # Fenced execution must never bypass attempt isolation. Let the
            # normal guarded failure path publish a durable terminal event.
            checkpoint_initialization_failed = True
        if ownership_token and runner_thread != visible_thread:
            try:
                from src.agents.checkpoints.observer import install_checkpoint_observer

                checkpoint_observer = install_checkpoint_observer(
                    create_checkpointer(),
                    _register_checkpoint_ref,
                    thread_filter=runner_thread,
                )
            except Exception:
                checkpoint_observer = None
    # Attachment scope for this turn: refs are rebuilt server-side from the
    # frozen turn_attachments snapshots so a stale client can never widen it.
    attachment_refs: list = []
    turn_attachments = conv.list_turn_attachments(turn_id)
    if turn_attachments:
        try:
            from src.attachments.service import AttachmentService
            from src.attachments.store import AttachmentStore

            store = AttachmentStore()
            service = AttachmentService(store=store)
            for snapshot in turn_attachments:
                record = store.get_attachment(
                    attachment_id=snapshot["attachment_id"],
                    session_id=int(turn.session_id),
                    user_id=int(user.id),
                    include_deleted=True,
                )
                if record is None or record.status != "active":
                    continue
                # Re-verify ownership: the snapshot belongs to this turn's
                # session, so any mismatch fails closed.
                if record.session_id != int(turn.session_id) or record.user_id != int(user.id):
                    continue
                refs = service.build_refs([record])
                attachment_refs.extend(refs)
        except Exception:
            # Attachment plumbing must never take down a KB-only turn.
            attachment_refs = []
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
    heartbeat_interval = max(2, min(30, src.settings.CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS))
    lease_lost = threading.Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(heartbeat_interval):
            renewed = conv.touch_turn_worker(turn_id, worker_id, fencing_token=ownership_token)
            if not renewed:
                # Lease renewal failed: stop scheduling new work immediately.
                # In-flight downstream calls finish on their own deadline and
                # only a newer attempt may publish authoritative results.
                lease_lost.set()
                cancel.set()
                return

    def _append_owned_event(event_type: str, payload: dict):
        """Append with ownership guard; drop late events from superseded attempts."""
        try:
            return conv.append_turn_event(
                turn_id,
                event_type,
                payload,
                worker_id=worker_id,
                fencing_token=ownership_token,
            )
        except StaleTurnOwner:
            return None

    def _fail_owned(message: str, *, cancelled_turn: bool = False) -> None:
        # 看门狗/取消掐断时主线程可能仍堵在下游调用里；能拿到多少就抢救
        # 多少：已流出的部分答案 + 本轮已登记的证据摘要。runner 若已
        # 产出权威答案（可能带引用清洗）则优先于流式拼接的文本。
        rescued_answer = ""
        rescued_summary: dict | None = None
        try:
            get_agent_answer = getattr(pipeline, "get_last_agent_answer", None)
            authoritative = (
                str(get_agent_answer() or "").strip()
                if callable(get_agent_answer)
                else ""
            )
            streamed = strip_narration_segments(
                "".join(answer_parts), narrated
            ).strip()
            rescued_answer = (authoritative or streamed).strip()
        except Exception:
            rescued_answer = ""
        try:
            raw_summary = (
                pipeline.get_last_retrieval_summary()
                if pipeline is not None
                else None
            )
            if isinstance(raw_summary, dict) and raw_summary.get("evidence"):
                rescued_summary = raw_summary
        except Exception:
            rescued_summary = None

        def _final_events(_final: ChatTurn) -> list[tuple[str, dict]]:
            if cancelled_turn:
                return [
                    ("error", {"message": message, "cancelled": True}),
                    ("turn_end", {"status": "cancelled"}),
                ]
            return [
                ("error", {"message": message}),
                ("turn_end", {"status": "failed"}),
            ]

        try:
            try:
                finalization = conv.fail_turn_result(
                    user.id,
                    turn_id,
                    message,
                    cancelled=cancelled_turn,
                    worker_id=worker_id,
                    fencing_token=ownership_token,
                    terminal_events_builder=_final_events,
                    partial_answer=rescued_answer or None,
                    summary=rescued_summary,
                )
            except TypeError as exc:
                # 抢救参数是新增能力：若某个 ConversationService 实现尚未
                # 跟上签名，绝不能把轮次留在中间态——退回最小终态调用，
                # 至少保证看门狗能给出明确的 failed/cancelled。
                if "partial_answer" not in str(exc) and "summary" not in str(exc):
                    raise
                finalization = conv.fail_turn_result(
                    user.id,
                    turn_id,
                    message,
                    cancelled=cancelled_turn,
                    worker_id=worker_id,
                    fencing_token=ownership_token,
                    terminal_events_builder=_final_events,
                )
        except StaleTurnOwner:
            # A newer attempt owns the turn; it will finalise it.
            return
        for event in finalization.terminal_events:
            _publish_turn_event(turn_id, event.seq, event.event_type, event.payload)

    # Hard turn budget: provider retries (notably repeated 502/503/504) can
    # otherwise keep one turn in ``streaming`` for many minutes.  The deadline
    # finalises the durable turn and sets the cancel event; late worker writes
    # are fenced, so the next question is never blocked indefinitely.
    deadline_event = threading.Event()
    # Cap by the provider's own retry budget as well: a single turn should not
    # outlive ``timeout × (retries + 2)`` even if the queue deadline is larger.
    provider_retry_budget = int(getattr(src.settings, "AGENT_TIMEOUT_SECONDS", 120)) * (
        int(getattr(src.settings, "AGENT_RATE_LIMIT_MAX_RETRIES", 4)) + 2
    )
    queue_deadline = int(getattr(src.settings, "CHAT_QUEUE_DEADLINE_SECONDS", 600))
    # 未声明调用上限的 deep 回合以完整性优先，看门狗放宽到 deep 预算；
    # 其余回合保持普通 600s 预算。
    deep_deadline = max(
        queue_deadline,
        int(getattr(src.settings, "CHAT_QUEUE_DEADLINE_DEEP_SECONDS", 900)),
    )
    if str(getattr(turn, "query_mode", "") or "") == "deep":
        deadline_seconds = max(60, deep_deadline)
    else:
        deadline_seconds = max(60, min(queue_deadline, provider_retry_budget))

    def _deadline_exceeded() -> None:
        deadline_event.set()
        cancel.set()
        _fail_owned("生成超时，请稍后重试")
        # Release the session lease immediately.  The main worker thread may
        # still be blocked inside a provider/tool call; it must not keep the
        # next question queued behind a turn that is already terminal.
        try:
            conv.release_turn_ownership(
                turn_id, worker_id, fencing_token=ownership_token
            )
        except Exception:
            pass

    deadline_timer = threading.Timer(deadline_seconds, _deadline_exceeded)
    deadline_timer.daemon = True
    deadline_timer.start()

    heartbeat_thread = start_thread_with_current_context(
        heartbeat_loop,
        daemon=True,
        name=f"turn-heartbeat-{turn_id[:8]}",
    )
    start = time.monotonic()
    started_at = datetime.now(timezone.utc)
    answer_parts: list[str] = []
    # Provisional answer deltas of narration segments were already streamed;
    # the narration events carry the retracted text in stream order and it is
    # stripped from the joined answer before persistence/done.
    narrated: list[str] = []
    first_token_ms: int | None = None
    stage_started: dict[str, float] = {}
    stage_durations_ms: dict[str, list[int]] = {}
    token_usage_summary = None
    outcome_status = "failed"
    short_term_memory = _short_term_memory_summary(
        [],
        source="server_conversation_history",
        status="not_checked",
    )

    # Cancel detection: in-process event first, then the Redis fast path every
    # ~250 ms, then the durable flag at most once per second (v3 §8/§12).
    _cancel_poll = {"redis": 0.0, "db": 0.0, "hit": False}

    def cancelled() -> bool:
        if cancel.is_set():
            return True
        if _cancel_poll["hit"]:
            return True
        now = time.monotonic()
        from src.core.redis_cancel import fast_path_enabled, is_cancel_requested

        if fast_path_enabled() and now - _cancel_poll["redis"] >= 0.25:
            _cancel_poll["redis"] = now
            if is_cancel_requested(turn_id):
                _cancel_poll["hit"] = True
                return True
        if now - _cancel_poll["db"] >= 1.0:
            _cancel_poll["db"] = now
            try:
                if conv.is_turn_cancel_requested(turn_id):
                    _cancel_poll["hit"] = True
                    return True
            except Exception:
                pass
        return False

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
        event = _append_owned_event("delta", {"text": text})
        if event is not None:
            _publish_turn_event(turn_id, event.seq, "delta", event.payload)

    def emit(event_type: str, payload: dict) -> None:
        conv.touch_turn_worker(turn_id, worker_id, fencing_token=ownership_token)
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
        if event_type == "narration":
            narrated.append(str(payload.get("text") or ""))
        event = _append_owned_event(event_type, payload)
        if event is None:
            return
        _publish_turn_event(turn_id, event.seq, event_type, event.payload)

    def emit_stage(key: str, label: str, status: str = "running", detail: str = "", **extra: Any) -> None:
        now = time.monotonic()
        if status == "running":
            stage_started.setdefault(key, now)
        elif status in {"done", "error"} and key in stage_started:
            stage_durations_ms.setdefault(key, []).append(int((now - stage_started.pop(key)) * 1000))
        emit("stage", {"key": key, "label": label, "status": status, "detail": detail, **extra})

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
            "accepted_to_first_token_ms": queue_ms + first_token_ms if first_token_ms is not None else None,
            "accepted_to_terminal_ms": queue_ms + int((time.monotonic() - start) * 1000),
            "stage_durations_ms": stage_durations_ms,
            "tool_calls": tool_latencies,
            "short_term_memory": dict(short_term_memory),
            "failed": failed,
            "cancelled": cancelled,
        }

    summary: dict = {}
    footer = ""
    try:
        if knowledge_scope_error is not None:
            _fail_owned("知识库来源权限已变化，请重新创建会话")
            outcome_status = "failed"
            return
        if checkpoint_initialization_failed:
            _fail_owned("检查点初始化失败，请稍后重试")
            return
        try:
            history = conv.history_before_turn(user.id, turn_id)
        except Exception:
            short_term_memory = _short_term_memory_summary(
                [],
                source="server_conversation_history",
                status="error",
            )
            _set_short_term_memory_observation(turn_observation, short_term_memory)
            raise
        short_term_memory = _short_term_memory_summary(
            history,
            source="server_conversation_history",
        )
        _set_short_term_memory_observation(turn_observation, short_term_memory)
        # 统一执行路径：通用对话（未挂载/`__general__`）与知识库问答都走
        # deepagents agent 循环，不存在单独的直连模型旁路。
        if pipeline is None:
            raise RuntimeError("query pipeline is unavailable")
        # P2-001: stable action-receipt identity for crash-recovered turns.
        action_scope = {
            "tenant_id": "default",
            "session_generation": session_generation,
            "turn_id": turn_id,
            "plan_version": "v1",
        }
        # General-agent task attempts carry the durable task id; the runner
        # rebuilds plan/workspace/artifacts from it on every turn.
        task_id = str(
            (getattr(turn, "trace_context", {}) or {}).get("task_id") or ""
        )
        try:
            emit(
                "stage",
                {
                    "key": "task_context",
                    "label": "任务上下文",
                    "status": "running",
                    "task_id": task_id,
                    "turn_status": str(getattr(turn, "status", "") or ""),
                },
            )
        except Exception:
            pass
        gen = _query_generator(
            pipeline,
            turn.query,
            turn.kb_name,
            history,
            ctx,
            runner_thread,
            _make_event_callback(emit_stage, emit),
            turn.query_mode,
            cancelled,
            persist_thread=True,
            wiki_assist=turn.wiki_assist,
            attachments=attachment_refs,
            source_scope=str(getattr(turn, "source_scope", "") or "auto"),
            propagate_errors=True,
            attachment_user_id=int(user.id),
            conversation_session_id=int(turn.session_id),
            action_scope=action_scope,
            knowledge_scope=knowledge_scope,
            knowledge_scope_resolver=knowledge_scope_resolver,
            task_id=task_id,
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
        summary = {
            **(pipeline.get_last_retrieval_summary() or {}),
            "short_term_memory": short_term_memory,
        }
        footer = pipeline.get_last_agent_footer() or ""
        token_usage_summary = pipeline.get_last_token_usage_summary()

        _flush_deltas()
        if deadline_event.is_set():
            # The watchdog already persisted the terminal failure event.
            outcome_status = "failed"
            return
        if cancelled():
            _fail_owned("已停止生成", cancelled_turn=True)
            outcome_status = "cancelled"
            # 与 legacy 路径对齐：取消也要落一条 trace，否则日志中心看不到
            # 被停止的查询（legacy 记的是 failed + client disconnected）。
            _record_query_trace(
                user=user,
                kb_name="" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name,
                original_query=turn.query,
                thread_id=str(turn.session_id),
                summary=summary,
                latency_ms=int((time.monotonic() - start) * 1000),
                status="failed",
                error_message="已停止生成",
                turn_id=turn_id,
            )
            return

        streamed_answer = strip_narration_segments("".join(answer_parts), narrated)
        get_agent_answer = getattr(pipeline, "get_last_agent_answer", None)
        authoritative_answer = (
            str(get_agent_answer() or "").strip() if callable(get_agent_answer) else ""
        )
        # The runner may normalize/scrub or synthesize the final answer after
        # the deltas were emitted; the authoritative copy must win over the
        # joined stream so done/persistence match verification.
        answer = authoritative_answer or streamed_answer
        if not answer:
            raise RuntimeError("未生成回答")
        summary = {
            **summary,
            "query_mode": turn.query_mode,
            "short_term_memory": short_term_memory,
        }
        # Canonical committed pointer (P0-003): the authoritative transaction
        # stores it together with the answer; the physical copy happens only
        # after the business commit succeeded.
        committed_ref = None
        if ownership_token and runner_thread != visible_thread:
            try:
                from src.agents.checkpoints import attempt_checkpoint_ref as _attempt_ref
                from src.agents.checkpoints import create_checkpointer as _agent_checkpointer

                committed_ref = _attempt_ref(
                    _agent_checkpointer(),
                    runner_thread,
                    fencing_token=fencing_token,
                    generation=session_generation,
                    attempt_id=str(getattr(turn, "attempt_id", "") or ""),
                )
            except Exception:
                committed_ref = None
        terminal_attachments = _turn_attachments_for(turn)

        def _terminal_events(completed_turn: ChatTurn) -> list[tuple[str, dict]]:
            """Terminal events persisted atomically with the answer (v3 §4.7)."""
            return [
                (
                    "stage",
                    {"key": "generate", "label": "生成回答", "status": "done", "detail": "最终回答已生成"},
                ),
                (
                    "done",
                    {
                        "turn": _turn_view(
                            completed_turn, attachments=terminal_attachments
                        ).model_dump(),
                        "answer": completed_turn.answer,
                        "summary": summary,
                        "footer": footer,
                    },
                ),
                ("turn_end", {"status": "completed"}),
            ]

        try:
            from opentelemetry.trace import TraceFlags

            context = turn_observation.span.get_span_context()
            terminal_metrics = metrics(summary=summary)
            terminal_metrics["quality_trace_identity"] = {
                "trace_id": f"{context.trace_id:032x}" if context.is_valid else "",
                "span_id": f"{context.span_id:016x}" if context.is_valid else "",
                "sampling_status": "sampled" if context.is_valid and context.trace_flags & TraceFlags.SAMPLED else "not_sampled",
            }
            completion = conv.complete_turn_result(
                user.id,
                turn_id,
                answer,
                summary,
                footer,
                metrics=terminal_metrics,
                worker_id=worker_id,
                fencing_token=ownership_token,
                expected_generation=session_generation if ownership_token else None,
                committed_checkpoint_ref=committed_ref,
                terminal_events_builder=_terminal_events,
            )
            completed = completion.turn
        except StaleTurnOwner:
            # Cancel revoked the business commit, or a newer attempt took over.
            if deadline_event.is_set():
                outcome_status = "failed"
            elif cancelled():
                _fail_owned("已停止生成", cancelled_turn=True)
                outcome_status = "cancelled"
            else:
                outcome_status = "stale"
            return
        # The business commit passed ownership/generation checks; only now may
        # this attempt move the canonical checkpoint pointer (P0a).
        if ownership_token and runner_thread != visible_thread:
            try:
                from src.agents.checkpoints import commit_attempt as _commit_attempt
                from src.agents.checkpoints import create_checkpointer as _agent_checkpointer

                _commit_attempt(
                    _agent_checkpointer(),
                    visible_thread,
                    runner_thread,
                    fencing_token,
                )
            except Exception:
                try:
                    from src.observability.metrics import counter

                    counter(
                        "hdb.checkpoint.commit_failed",
                        description="Canonical checkpoint commits rejected or failed",
                    )
                except Exception:
                    pass
        # ``complete_turn_result`` is the canonical normalization boundary for
        # stale export fallbacks and persists the terminal events atomically.
        # Live subscribers receive those persisted events through the queue.
        answer = completed.answer
        turn_observation.set_output(answer, content_kind="llm")
        for event in completion.terminal_events:
            _publish_turn_event(turn_id, event.seq, event.event_type, event.payload)
        outcome_status = "completed"
        # P1-9 行为保留：trace 状态由回答文本 + 检索 summary 推导，而不是
        # 无论 no_evidence / partial_failure 都硬编码 success。
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
            turn_id=turn_id,
        )
    except Exception as exc:
        turn_observation.error(exc)
        if deadline_event.is_set():
            # The watchdog already persisted the terminal failure event.
            outcome_status = "failed"
            return
        if cancelled():
            _fail_owned("已停止生成", cancelled_turn=True)
            outcome_status = "cancelled"
        else:
            from src.core.error_friendly import friendly_error_message

            friendly_error = friendly_error_message(exc)
            _fail_owned(friendly_error)
            summary = {
                **summary,
                "short_term_memory": short_term_memory,
            }
            _record_query_trace(
                user=user,
                kb_name="" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name,
                original_query=turn.query,
                thread_id=str(turn.session_id),
                summary=summary,
                latency_ms=int((time.monotonic() - start) * 1000),
                status="failed",
                error_message=friendly_error,
                turn_id=turn_id,
            )
    finally:
        deadline_timer.cancel()
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
        _set_short_term_memory_observation(turn_observation, short_term_memory)
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
        if checkpoint_observer is not None:
            try:
                checkpoint_observer.uninstall()
            except Exception:
                pass
        try:
            conv.release_turn_ownership(turn_id, worker_id, fencing_token=ownership_token)
        except Exception:
            pass
        if chat_permit is not None:
            try:
                chat_permit.release()
            except Exception:
                pass
        _clear_turn_cancel_signal(turn_id)
        try:
            from src.core.redis_cancel import clear_cancel

            clear_cancel(turn_id)
        except Exception:
            pass


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
    try:
        knowledge_scope = resolve_session_scope(auth, user, session, body.knowledge_scope)
    except KnowledgeScopeDenied as exc:
        raise HTTPException(status_code=403, detail="knowledge_scope_denied") from exc
    except KnowledgeScopeInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MultiKnowledgeRetrievalUnavailable as exc:
        raise HTTPException(status_code=409, detail="feature_unavailable") from exc
    ctx = build_context_for_user(user, session.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if session.kb_name != GENERAL_CHAT_KB_NAME and not ctx.has_kb_permission(session.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    # P0-006: an idempotent replay returns the original turn and never counts
    # against admission or creates a second unit of work.
    replay = conv.find_turn_by_client_request(session_id, body.client_request_id or "")
    if replay is not None and knowledge_scope is None:
        messages = conv.list_messages(user.id, session_id)
        user_message = next(
            (message for message in messages if message.id == replay.user_message_id), None
        )
        if user_message is None:
            raise HTTPException(status_code=500, detail="turn message was not persisted")
        return TurnStartResponse(
            turn=_turn_view(replay),
            user_message=_message_view(
                user_message, attachments=_turn_attachments_for(replay)
            ),
        )
    from src.api.admission import admission_model_name, enforce_admission

    # Resource type follows the execution mode: deep knowledge-base turns get
    # the stricter deep bucket, general chat keeps the chat bucket.
    query_mode = "fast" if session.kb_name == GENERAL_CHAT_KB_NAME else "deep"
    enforce_admission(
        user,
        session.department_id,
        resource="deep" if query_mode == "deep" else "chat",
        model=admission_model_name(),
    )
    if conv.count_unterminal_turns(user.id, session_id=session_id) >= int(
        src.settings.ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION
    ):
        raise HTTPException(
            status_code=429,
            detail={"message": "本会话排队任务过多，请等待或取消后再试", "reason": "session_backlog"},
            headers={"Retry-After": "5"},
        )
    if conv.count_unterminal_turns(user.id) >= int(
        src.settings.ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER
    ):
        raise HTTPException(
            status_code=429,
            detail={"message": "未完成的任务过多，请稍后再试", "reason": "user_backlog"},
            headers={"Retry-After": "5"},
        )
    # Chat attachments (design §5/§14.2): resolve the explicit source scope
    # before touching attachment rows.  ``knowledge_base_only`` must not be
    # widened merely because a stale client also sent attachment ids.
    attachment_refs: list = []
    requested_attachment_ids = [
        str(item or "").strip() for item in (body.attachment_ids or []) if str(item or "").strip()
    ]
    has_attachments = bool(requested_attachment_ids)
    from src.attachments.models import (
        resolve_source_scope,
        scope_allows_attachments,
    )

    is_general_chat = session.kb_name == GENERAL_CHAT_KB_NAME
    try:
        effective_scope = resolve_source_scope(
            requested_scope=body.source_scope,
            has_attachments=has_attachments,
            is_general_chat=is_general_chat,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if has_attachments and scope_allows_attachments(effective_scope):
        if not src.settings.CHAT_ATTACHMENTS_ENABLED:
            raise HTTPException(status_code=404, detail="chat attachments are not enabled")
        from src.attachments.service import AttachmentService

        attachment_service = AttachmentService()
        attachment_refs, blocked = attachment_service.resolve_refs_for_turn(
            user_id=user.id,
            session_id=session_id,
            attachment_ids=requested_attachment_ids,
        )
        if blocked:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "one or more attachments are unavailable",
                    "error_code": "attachment_unavailable",
                    "attachment_ids": blocked[:10],
                },
            )
    trace_context = inject_trace_context()
    try:
        turn = conv.create_turn(
        user.id,
        session_id,
        body.query,
        body.client_request_id,
        query_mode,
        trace_context=trace_context,
        wiki_assist=body.wiki_assist,
        attachment_ids=[ref.attachment_id for ref in attachment_refs],
            source_scope=effective_scope,
            **({"knowledge_scope": knowledge_scope} if knowledge_scope is not None else {}),
        )
    except (KnowledgeScopeChanged, IdempotencyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KnowledgeScopeInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    messages = conv.list_messages(user.id, session_id)
    user_message = next((message for message in messages if message.id == turn.user_message_id), None)
    if user_message is None:
        raise HTTPException(status_code=500, detail="turn message was not persisted")
    return TurnStartResponse(
        turn=_turn_view(turn),
        user_message=_message_view(user_message, attachments=_turn_attachments_for(turn)),
    )


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
    if not _can_read_turn(auth, user, turn, conv):
        raise HTTPException(status_code=403, detail="knowledge_scope_denied")
    ctx = build_context_for_user(user, turn.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if turn.kb_name != GENERAL_CHAT_KB_NAME and not ctx.has_kb_permission(turn.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    # Worker polling owns execution. This endpoint remains idempotent so the
    # browser can safely call it after a refresh to ensure the turn is queued.
    # A dependency-overridden pipeline is only used by in-process API tests;
    # production never enters this branch and always relies on the worker.
    if request.app.dependency_overrides.get(get_pipeline) is not None and turn.status in {"pending", "task_pending", "streaming", "cancelling"}:
        start_thread_with_current_context(
            _run_turn,
            turn_id=turn.id,
            user=user,
            ctx=ctx,
            pipeline=_resolve_pipeline(request),
            daemon=True,
            name=f"test-chat-turn-{turn.id[:8]}",
        )
    return _turn_view(turn)


@router.get("/turns/{turn_id}", response_model=TurnView)
def get_turn(
    turn_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    turn = conv.get_turn(user.id, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    if not _can_read_turn(auth, user, turn, conv):
        return _restricted_turn_view(turn)
    return _turn_view(turn)


@router.get("/conversations/{session_id}/turns", response_model=list[TurnView])
def list_active_turns(
    session_id: int,
    include_terminal: bool = False,
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
    if include_terminal:
        # Refresh/reopen recovery needs the newest turn even when it already
        # finished; live SSE remains for running turns only.
        turns = conv.list_recent_turns(user.id, session_id)
    else:
        turns = conv.list_active_turns(user.id, session_id)
    return [
        _turn_view(turn) if _can_read_turn(auth, user, turn, conv) else _restricted_turn_view(turn)
        for turn in turns
    ]


@router.post("/turns/{turn_id}/cancel", response_model=TurnView)
def cancel_turn(
    turn_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    turn = conv.request_turn_cancel(user.id, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    # The durable flag above is authoritative; the Redis key only shortens the
    # time until the owning worker notices (v3 §12, fail-soft).
    from src.core.redis_cancel import publish_cancel

    publish_cancel(turn_id)
    # Do not create a process-local signal in an API process that does not own
    # the worker. The DB flag above is the cross-process cancellation signal.
    local_signal = _existing_turn_cancel_signal(turn_id)
    if local_signal is not None:
        local_signal.set()
    return _turn_view(turn) if _can_read_turn(auth, user, turn, conv) else _restricted_turn_view(turn)


@router.get("/turns/{turn_id}/events")
async def stream_turn_events(
    turn_id: str,
    request: Request,
    after: int = 0,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    turn = conv.get_turn(user.id, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    if not _can_read_turn(auth, user, turn, conv):
        raise HTTPException(status_code=403, detail="knowledge_scope_denied")
    try:
        last_seq = max(after, int(request.headers.get("Last-Event-ID") or 0))
    except ValueError:
        last_seq = max(0, after)

    async def event_stream():
        nonlocal last_seq
        q, unsubscribe = _subscribe_turn_events(turn_id)
        db_synced = False  # initial pass: replay anything persisted before we subscribed
        terminal_idle_polls = 0
        redis_cursor = "0-0"

        def _readable() -> bool:
            current = conv.get_turn(user.id, turn_id)
            return current is not None and _can_read_turn(auth, user, current, conv)

        def _restricted_event():
            return _sse(
                "source_access_restricted",
                {"message": "来源权限已变化", "source_access_restricted": True},
                None,
            )

        def _drain_db_events() -> list:
            return conv.list_turn_events(user.id, turn_id, last_seq)

        try:
            while True:
                if not _readable():
                    yield _restricted_event()
                    break
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
                        if not _readable():
                            yield _restricted_event()
                            return
                        last_seq = seq
                        pushed += 1
                        yield _sse(event_type, payload, seq)
                except queue.Empty:
                    pass
                if not db_synced:
                    if not _readable():
                        yield _restricted_event()
                        break
                    for event in _drain_db_events():
                        if not _readable():
                            yield _restricted_event()
                            return
                        last_seq = event.seq
                        yield _sse(event.event_type, event.payload, event.seq)
                    db_synced = True
                    continue

                current = conv.get_turn(user.id, turn_id)
                # Read-only boundary (v3 §7.3): subscribers never write terminal
                # state. A stale claim is converged by the worker reaper
                # (``requeue_stale_turns``); this stream keeps waiting and
                # replays the terminal events once they are persisted.
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
                    # Cross-process wake-up: a committed event in another
                    # process lands here in milliseconds; the DB drain below
                    # stays the replay truth (gap and lost-tail recovery).
                    try:
                        from src.core.redis_events import read_turn_events

                        redis_events, redis_cursor = read_turn_events(
                            turn_id, after_id=redis_cursor
                        )
                    except Exception:
                        redis_events = []
                    if redis_events:
                        db_synced = False
                        continue
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
    persist_thread=False,
    wiki_assist=False,
    attachments=None,
    source_scope="auto",
    propagate_errors=False,
    attachment_user_id=None,
    conversation_session_id=None,
    action_scope=None,
    knowledge_scope=None,
    knowledge_scope_resolver=None,
    task_id="",
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
        if persist_thread and "persist_thread" in params:
            kwargs["persist_thread"] = True
        if "wiki_assist" in params:
            kwargs["wiki_assist"] = wiki_assist
        if "attachments" in params:
            kwargs["attachments"] = attachments
        if "source_scope" in params:
            kwargs["source_scope"] = source_scope
        if "propagate_errors" in params:
            kwargs["propagate_errors"] = propagate_errors
        if "attachment_user_id" in params:
            kwargs["attachment_user_id"] = attachment_user_id
        if "conversation_session_id" in params:
            kwargs["conversation_session_id"] = conversation_session_id
        if action_scope is not None and "action_scope" in params:
            kwargs["action_scope"] = action_scope
        if "knowledge_scope" in params:
            kwargs["knowledge_scope"] = knowledge_scope
        if "knowledge_scope_resolver" in params:
            kwargs["knowledge_scope_resolver"] = knowledge_scope_resolver
        if "task_id" in params:
            kwargs["task_id"] = task_id
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
    if persist_thread and "persist_thread" in params:
        kwargs["persist_thread"] = True
    if "wiki_assist" in params:
        kwargs["wiki_assist"] = wiki_assist
    if "attachments" in params:
        kwargs["attachments"] = attachments
    if "source_scope" in params:
        kwargs["source_scope"] = source_scope
    if "propagate_errors" in params:
        kwargs["propagate_errors"] = propagate_errors
    if "attachment_user_id" in params:
        kwargs["attachment_user_id"] = attachment_user_id
    if "conversation_session_id" in params:
        kwargs["conversation_session_id"] = conversation_session_id
    if action_scope is not None and "action_scope" in params:
        kwargs["action_scope"] = action_scope
    if "knowledge_scope" in params:
        kwargs["knowledge_scope"] = knowledge_scope
    if "knowledge_scope_resolver" in params:
        kwargs["knowledge_scope_resolver"] = knowledge_scope_resolver
    if "task_id" in params:
        kwargs["task_id"] = task_id
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
