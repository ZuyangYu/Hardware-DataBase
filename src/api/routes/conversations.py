from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.auth import AuthService, AuthUser
from src.core.conversation import ConversationService

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, reject_system_admin_kb_access
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
GENERAL_CHAT_KB_NAME = "__general__"


def _conv_service() -> ConversationService:
    return ConversationService()


def _session_view(s) -> SessionView:
    return SessionView(
        id=s.id,
        user_id=s.user_id,
        kb_name=s.kb_name,
        title=s.title,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _message_view(m) -> MessageView:
    return MessageView(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        footer=m.footer,
        created_at=m.created_at,
        edited_at=getattr(m, "edited_at", None),
        redacted=bool(getattr(m, "redacted", False)),
        memory_context=getattr(m, "memory_context", []),
    )


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
    return [_session_view(s) for s in conv.list_sessions(user.id, kb_name)]


@router.post("/conversations", response_model=SessionView)
def create_session(
    body: CreateSessionRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    _ensure_kb_read(user, body.kb_name, auth)
    ctx = build_context_for_user(user, body.kb_name, auth=auth)
    s = conv.create_session(
        user.id,
        body.kb_name,
        body.title,
        department_id=ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id"),
        kb_id=ctx.metadata.get("kb_id"),
    )
    return _session_view(s)


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
    return _session_view(s)


@router.delete("/conversations/{session_id}", response_model=OkResponse)
def delete_session(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    conv.delete_session(user.id, session_id)
    return OkResponse(ok=True, message="session deleted")


@router.post("/conversations/{session_id}/clear", response_model=OkResponse)
def clear_session(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_history_access(user, auth)
    conv.clear_session(user.id, session_id)
    return OkResponse(ok=True, message="session cleared")


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
    return [_message_view(m) for m in conv.list_messages(user.id, session_id)]


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
