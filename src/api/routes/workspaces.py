"""Workspace-first API surface.

A workspace is the durable container; sessions are conversations inside it.
The legacy ``kb_name`` session field is still populated for compatibility, but
workspace kind/binding is the authoritative tool configuration.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service
from src.api.routes.conversations import _ensure_kb_read, _session_view
from src.core.auth import AuthService, AuthUser
from src.core.conversation import ConversationService
from src.core.workspace_store import WorkspaceStore, get_workspace_store

router = APIRouter(tags=["workspaces"])


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "general"
    kb_name: str = ""
    kb_id: int | None = None


class WorkspaceView(BaseModel):
    id: str
    tenant_id: str
    user_id: int
    kind: str
    name: str
    kb_name: str
    kb_id: int | None = None
    created_at: str
    updated_at: str
    session_count: int = 0


def _workspace_view(store: WorkspaceStore, workspace) -> WorkspaceView:
    return WorkspaceView(
        id=workspace.id,
        tenant_id=workspace.tenant_id,
        user_id=workspace.user_id,
        kind=workspace.kind,
        name=workspace.name,
        kb_name=workspace.kb_name,
        kb_id=workspace.kb_id,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        session_count=len(store.list_session_ids(workspace.id)),
    )


def _conv_service() -> ConversationService:
    return ConversationService()


@router.get("/workspaces", response_model=list[WorkspaceView])
def list_workspaces(
    user: AuthUser = Depends(current_user),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> list[WorkspaceView]:
    return [_workspace_view(store, item) for item in store.list_workspaces(user.id)]


@router.post("/workspaces", response_model=WorkspaceView, status_code=201)
def create_workspace(
    body: CreateWorkspaceRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> WorkspaceView:
    kind = str(body.kind or "general").strip().lower()
    if kind not in {"general", "knowledge"}:
        raise HTTPException(status_code=422, detail="kind must be general or knowledge")
    kb_name = str(body.kb_name or "").strip()
    kb_id = body.kb_id
    if kind == "knowledge":
        if not kb_name:
            raise HTTPException(status_code=422, detail="knowledge workspace requires kb_name")
        _ensure_kb_read(user, kb_name, auth)
        ctx = build_context_for_user(user, kb_name, auth=auth)
        kb_id = ctx.metadata.get("kb_id") or kb_id
    else:
        kb_name = ""
    workspace = store.create_workspace(
        user_id=user.id,
        kind=kind,
        name=body.name,
        kb_name=kb_name,
        kb_id=kb_id,
    )
    return _workspace_view(store, workspace)


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceView)
def get_workspace(
    workspace_id: str,
    user: AuthUser = Depends(current_user),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> WorkspaceView:
    workspace = store.get_workspace(workspace_id, user.id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return _workspace_view(store, workspace)


@router.get("/workspaces/{workspace_id}/sessions")
def list_workspace_sessions(
    workspace_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: WorkspaceStore = Depends(get_workspace_store),
    conv: ConversationService = Depends(_conv_service),
):
    workspace = store.get_workspace(workspace_id, user.id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    sessions = []
    for session_id in store.list_session_ids(workspace_id):
        session = conv.get_session(user.id, session_id)
        if session is not None:
            sessions.append(
                _session_view(
                    session,
                    user=user,
                    auth=auth,
                    workspace_id=workspace.id,
                )
            )
    return sessions
