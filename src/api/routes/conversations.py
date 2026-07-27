from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.auth import AuthUser
from src.core.conversation import ConversationService

from src.api.deps import current_user
from src.api.schemas import (
    AddMessageRequest,
    CreateSessionRequest,
    MessageView,
    OkResponse,
    SessionView,
)

router = APIRouter(tags=["conversations"])


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
        created_at=m.created_at,
    )


@router.get("/conversations", response_model=list[SessionView])
def list_sessions(
    kb_name: str | None = None,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    return [_session_view(s) for s in conv.list_sessions(user.id, kb_name)]


@router.post("/conversations", response_model=SessionView)
def create_session(
    body: CreateSessionRequest,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.create_session(user.id, body.kb_name, body.title)
    return _session_view(s)


@router.get("/conversations/{session_id}", response_model=SessionView)
def get_session(
    session_id: int,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _session_view(s)


@router.delete("/conversations/{session_id}", response_model=OkResponse)
def delete_session(
    session_id: int,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    existed = conv.delete_session(user.id, session_id)
    if not existed:
        raise HTTPException(status_code=404, detail="session not found")
    return OkResponse(ok=True, message="session deleted")


@router.post("/conversations/{session_id}/clear", response_model=OkResponse)
def clear_session(
    session_id: int,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    s = conv.get_session(user.id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    conv.clear_session(user.id, session_id)
    return OkResponse(ok=True, message="session cleared")


@router.get("/conversations/{session_id}/messages", response_model=list[MessageView])
def list_messages(
    session_id: int,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    # list_messages returns [] when session doesn't exist (safe empty)
    return [_message_view(m) for m in conv.list_messages(user.id, session_id)]


@router.post("/conversations/{session_id}/messages", response_model=MessageView)
def add_message(
    session_id: int,
    body: AddMessageRequest,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
):
    if body.role not in ("user", "assistant", "system"):
        raise HTTPException(status_code=400, detail="role must be user, assistant, or system")
    try:
        m = conv.add_message(user.id, session_id, body.role, body.content)
    except PermissionError:
        raise HTTPException(status_code=404, detail="session not found")
    return _message_view(m)