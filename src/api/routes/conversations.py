from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.auth import AuthService, AuthUser
from src.core.conversation import GENERAL_CHAT_KB_NAME, ConversationService

from src.api.context import build_context_for_user
from src.services.knowledge_scope_access import can_read_turn, resolve_session_scope
from src.services.knowledge_scope import KnowledgeScopeDenied, KnowledgeScopeInvalid
from src.agents.tools.retrieval_executor import MultiKnowledgeRetrievalUnavailable
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    AddMessageRequest,
    CreateSessionRequest,
    EditMessageRequest,
    MessageView,
    OkResponse,
    SessionMemorySettingsUpdate,
    SessionMemorySummary,
    SessionView,
)

router = APIRouter(tags=["conversations"])


def _conv_service() -> ConversationService:
    return ConversationService()


def _can_read_turn(auth, user, turn, conv: ConversationService) -> bool:
    return can_read_turn(
        auth,
        user,
        turn,
        dependency_loader=lambda inherited_id: conv.get_turn(user.id, str(inherited_id)),
    )


def _session_view(s, *, user=None, auth=None, scope=None) -> SessionView:
    restricted = False
    if scope is None and getattr(s, "knowledge_selection", None) and user is not None:
        try:
            scope = resolve_session_scope(auth, user, s, execute=False)
        except (KnowledgeScopeDenied, KnowledgeScopeInvalid):
            restricted = True
    return SessionView(
        id=s.id,
        user_id=s.user_id,
        kb_name=s.kb_name,
        title=s.title,
        created_at=s.created_at,
        updated_at=s.updated_at,
        document_context=None if restricted else getattr(s, "document_context", None),
        knowledge_scope=scope,
        knowledge_selection_hash=getattr(s, "knowledge_selection_hash", ""),
        source_access_restricted=restricted,
    )


def _attachment_snapshot_view(snapshot: dict):
    from src.api.schemas import AttachmentSnapshotView

    return AttachmentSnapshotView(
        attachment_id=str(snapshot.get("attachment_id") or ""),
        filename=str(snapshot.get("filename_snapshot") or ""),
        media_type=str(snapshot.get("media_type_snapshot") or ""),
        usage_hint=str(snapshot.get("usage_hint_snapshot") or "reference"),
        content_hash=str(snapshot.get("content_hash_snapshot") or ""),
        ordinal=int(snapshot.get("ordinal") or 0),
        parse_status=str(snapshot.get("parse_status_snapshot") or "queued"),
        deleted=bool(snapshot.get("deleted", False)),
    )


def _message_view(
    m,
    cards: list[dict] | None = None,
    trace_steps: list[dict] | None = None,
    plan: dict | None = None,
    budget: dict | None = None,
    verification: dict | None = None,
    subagents: list[dict] | None = None,
) -> MessageView:
    attachments = []
    if m.turn_id and m.role == "user":
        try:
            snapshots = _conv_service().list_turn_attachments(str(m.turn_id))
            attachments = [_attachment_snapshot_view(snapshot) for snapshot in snapshots]
        except Exception:
            attachments = []
    return MessageView(
        id=m.id,
        session_id=m.session_id,
        turn_id=getattr(m, "turn_id", None),
        role=m.role,
        content=m.content,
        footer=m.footer,
        created_at=m.created_at,
        edited_at=getattr(m, "edited_at", None),
        redacted=bool(getattr(m, "redacted", False)),
        memory_context=getattr(m, "memory_context", []),
        document_context=getattr(m, "document_context", None),
        attachments=attachments,
        citations=getattr(m, "citations", []) or [],
        cards=list(cards or []),
        trace_steps=list(trace_steps or []),
        plan=plan,
        budget=budget,
        verification=verification,
        subagents=list(subagents or []),
    )


_TRACE_STEP_IGNORED_KEYS = frozenset({"permission", "generate"})


def _trace_steps_from_events(events) -> list[dict]:
    """把持久化的 turn 事件折叠成与前端实时轨迹同形的步骤列表。

    stage / tool_started / tool_result 三类事件驱动实时执行轨迹；刷新页面后
    用同一规则从 chat_turn_events 重建，避免轨迹只在当次流式会话里可见。
    """
    from src.agents.tools.runtime import tool_label

    steps: list[dict] = []
    active: dict[str, int] = {}
    for event in events:
        event_type = str(getattr(event, "event_type", "") or "")
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        if event_type == "tool_started":
            name = str(payload.get("tool_name") or "").strip()
            if not name:
                continue
            active[name] = len(steps)
            steps.append(
                {
                    "key": f"{name}#{len(steps) + 1}",
                    "label": tool_label(name),
                    "status": "running",
                    "detail": str(payload.get("query") or ""),
                }
            )
        elif event_type == "tool_result":
            name = str(payload.get("tool_name") or "").strip()
            if not name:
                continue
            hits = int(payload.get("hit_count") or 0)
            latency = payload.get("latency_ms")
            detail = f"{hits} 条命中"
            if isinstance(latency, (int, float)):
                detail += f" · {int(latency)}ms"
            step = {
                "key": f"{name}#{len(steps) + 1}",
                "label": tool_label(name),
                "status": "error" if payload.get("status") == "failed" else "done",
                "detail": detail,
            }
            index = active.get(name)
            if index is not None and 0 <= index < len(steps) and steps[index]["key"].startswith(f"{name}#"):
                steps[index] = {**steps[index], **step, "key": steps[index]["key"]}
            else:
                steps.append(step)
        elif event_type == "stage":
            key = str(payload.get("key") or "").strip()
            if not key or key in _TRACE_STEP_IGNORED_KEYS:
                continue
            step = {
                "key": key,
                "label": str(payload.get("label") or key),
                "status": str(payload.get("status") or "done"),
                "detail": str(payload.get("detail") or ""),
            }
            for existing in steps:
                if existing["key"] == key:
                    existing.update(step)
                    break
            else:
                steps.append(step)
    return steps



def _ensure_kb_read(user: AuthUser, kb_name: str, auth: AuthService) -> None:
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if kb_name == GENERAL_CHAT_KB_NAME:
        return
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")


def _ensure_history_access(user: AuthUser, auth: AuthService) -> None:
    """Conversation history is owned by its user, not their current department.

    Current KB permission is still required before creating a session or turn;
    this guard only keeps governance-only system admins out of chat history.
    """
    reject_system_admin_kb_access(build_context_for_user(user, auth=auth))


@router.get("/conversations", response_model=list[SessionView])
def list_sessions(
    kb_name: str | None = None,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    _ensure_history_access(user, auth)
    return [_session_view(s, user=user, auth=auth) for s in conv.list_sessions(user.id, kb_name)]


@router.post("/conversations", response_model=SessionView)
def create_session(
    body: CreateSessionRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    scope = None
    if body.knowledge_scope is not None:
        from types import SimpleNamespace
        try:
            scope = resolve_session_scope(auth, user, SimpleNamespace(kb_name=body.kb_name, knowledge_selection=None), body.knowledge_scope)
        except KnowledgeScopeDenied as exc:
            raise HTTPException(status_code=403, detail="knowledge_scope_denied") from exc
        except KnowledgeScopeInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except MultiKnowledgeRetrievalUnavailable as exc:
            raise HTTPException(status_code=409, detail="feature_unavailable") from exc
    _ensure_kb_read(user, body.kb_name, auth)
    ctx = build_context_for_user(user, body.kb_name, auth=auth)
    s = conv.create_session(
        user.id,
        body.kb_name,
        body.title,
        department_id=ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id"),
        kb_id=ctx.metadata.get("kb_id"),
        **({"knowledge_selection": body.knowledge_scope} if scope is not None else {}),
    )
    return _session_view(s, scope=scope)


@router.get("/conversations/{session_id}", response_model=SessionView)
def get_session(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    return _session_view(s, user=user, auth=auth)


@router.delete("/conversations/{session_id}", response_model=OkResponse)
def delete_session(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    pipeline=Depends(get_pipeline),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    conv.delete_session(user.id, session_id)
    pipeline.forget_agent_thread(str(session_id))
    return OkResponse(ok=True, message="session deleted")


@router.get("/conversations/{session_id}/messages", response_model=list[MessageView])
def list_messages(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    messages = conv.list_messages(user.id, session_id)
    cards_by_turn: dict[str, list[dict]] = {}
    try:
        cards_by_turn = conv.list_session_document_cards(user.id, session_id)
    except Exception:
        # History rehydration is best-effort: a card projection failure must
        # never break the message list itself.
        cards_by_turn = {}
    assistant_turns = {
        str(m.turn_id) for m in messages if m.role == "assistant" and m.turn_id
    }
    trace_by_turn: dict[str, list[dict]] = {}
    for turn_id in assistant_turns:
        try:
            trace_by_turn[turn_id] = _trace_steps_from_events(
                conv.list_turn_events(user.id, turn_id)
            )
        except Exception:
            # Trace rehydration is best-effort: a projection failure must
            # never break the message list itself.
            trace_by_turn[turn_id] = []
    views: list[MessageView] = []
    for m in messages:
        owning_turn = None
        if m.turn_id:
            owning_turn = conv.get_turn(user.id, str(m.turn_id))
            if owning_turn is not None and not _can_read_turn(auth, user, owning_turn, conv):
                views.append(MessageView(id=m.id, session_id=m.session_id, turn_id=m.turn_id,
                    role=m.role, content="来源权限已变化", footer="", created_at=m.created_at,
                    redacted=True, source_access_restricted=True))
                continue
        turn_id = str(getattr(m, "turn_id", "") or "")
        cards: list[dict] = []
        if turn_id and (m.role == "assistant" or turn_id not in assistant_turns):
            cards = cards_by_turn.get(turn_id, [])
        trace_steps = trace_by_turn.get(turn_id, []) if m.role == "assistant" else []
        summary = getattr(owning_turn, "summary", None) if m.role == "assistant" else None
        summary = summary if isinstance(summary, dict) else {}
        views.append(
            _message_view(
                m,
                cards=cards,
                trace_steps=trace_steps,
                plan=summary.get("plan") if isinstance(summary.get("plan"), dict) else None,
                budget=summary.get("budget") if isinstance(summary.get("budget"), dict) else None,
                verification=(
                    summary.get("verification")
                    if isinstance(summary.get("verification"), dict)
                    else None
                ),
                subagents=(
                    summary.get("subagents")
                    if isinstance(summary.get("subagents"), list)
                    else []
                ),
            )
        )
    return views


@router.post("/conversations/{session_id}/messages", response_model=MessageView)
def add_message(
    session_id: int,
    body: AddMessageRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    if body.role not in ("user", "assistant", "system"):
        raise HTTPException(status_code=400, detail="role must be user, assistant, or system")
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    try:
        m = conv.add_message(user.id, session_id, body.role, body.content)
    except PermissionError:
        raise HTTPException(status_code=404, detail="session not found")
    return _message_view(m)


@router.patch("/conversations/{session_id}/messages/{message_id}", response_model=MessageView)
def edit_message(
    session_id: int,
    message_id: int,
    body: EditMessageRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    """Edit or redact a raw message; invalidates sourced memory atomically."""
    if not body.redact and not (body.content and body.content.strip()):
        raise HTTPException(status_code=400, detail="content is required unless redact=true")
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    try:
        m = conv.edit_message(
            user.id,
            session_id,
            message_id,
            content=None if body.redact else body.content,
            redact=body.redact,
            reason=body.reason,
            request_id=body.request_id,
        )
    except PermissionError:
        raise HTTPException(status_code=404, detail="session not found")
    except KeyError:
        raise HTTPException(status_code=404, detail="message not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _message_view(m)


@router.get("/conversations/{session_id}/memory-summary", response_model=SessionMemorySummary)
def get_session_memory_summary(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    try:
        summary = conv.get_session_memory_settings(user.id, session_id)
    except PermissionError:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionMemorySummary(**summary)


@router.put("/conversations/{session_id}/memory-settings", response_model=SessionMemorySummary)
def put_session_memory_settings(
    session_id: int,
    body: SessionMemorySettingsUpdate,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    """Toggle per-session automatic Project Memory extraction."""
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    try:
        summary = conv.set_session_auto_extract(user.id, session_id, body.auto_extract)
    except PermissionError:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionMemorySummary(**summary)
