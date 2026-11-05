"""General-agent task API.

Tasks are the durable unit: they own a plan, a workspace and artifacts, and
one or more chat turns execute them.  This route reuses the existing turn
queue (worker polling, fencing, SSE events) and only adds the task-level
bookkeeping.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.agents.task_definitions import resolve_definition
from src.api.context import build_context_for_user
from src.api.deps import (
    current_user,
    get_auth_service,
    reject_system_admin_kb_access,
)
from src.core.auth import AuthService, AuthUser
from src.core.conversation import GENERAL_CHAT_KB_NAME, ConversationService
from src.core.task_store import Task, TaskArtifact, TaskStore, get_task_store

router = APIRouter(tags=["tasks"])

SourceScope = Literal[
    "auto",
    "attachment_only",
    "knowledge_base_only",
    "attachment_and_knowledge_base",
]


def _conv_service() -> ConversationService:
    return ConversationService()


def _task_store():
    return get_task_store()


class CreateTaskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    kb_name: str = Field(default=GENERAL_CHAT_KB_NAME, max_length=200)
    session_id: int | None = None
    definition_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=200)
    source_scope: SourceScope = "auto"


class ContinueTaskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    source_scope: SourceScope = "auto"


def _ensure_task_readable(
    task: Task,
    *,
    user: AuthUser,
    auth: AuthService,
    conv: ConversationService,
) -> None:
    session = conv.get_session(user.id, task.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="task session not found")
    ctx = build_context_for_user(user, session.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if session.kb_name != GENERAL_CHAT_KB_NAME and not ctx.has_kb_permission(
        session.kb_name, "read"
    ):
        raise HTTPException(status_code=403, detail="read permission required")


def _turn_projection(turn) -> dict:
    if turn is None:
        return {}
    summary = dict(getattr(turn, "summary", {}) or {})
    budget = dict(summary.get("budget") or {})
    return {
        "id": turn.id,
        "status": turn.status,
        "query": turn.query,
        "answer": turn.answer or "",
        "error_message": turn.error_message or "",
        "created_at": turn.created_at,
        "finished_at": turn.finished_at,
        "stopped_reason": budget.get("stopped_reason"),
        "partial_answer": bool(budget.get("partial_answer")),
    }


def _artifact_projection(artifact: TaskArtifact) -> dict:
    return {
        "id": artifact.id,
        "name": artifact.name,
        "kind": artifact.kind,
        "mime": artifact.mime,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "version": artifact.version,
        "created_at": artifact.created_at,
        "download_url": f"/api/v1/tasks/{artifact.task_id}/artifacts/{artifact.id}/download",
    }


def _task_projection(
    task: Task,
    *,
    conv: ConversationService,
    store: TaskStore,
) -> dict:
    turn = None
    if task.current_turn_id:
        turn = conv.get_turn(task.user_id, task.current_turn_id)
    effective_status = task.status
    turn_status = str(getattr(turn, "status", "") or "") if turn else ""
    if effective_status in {"queued", "running"}:
        if turn_status == "completed":
            effective_status = "completed"
        elif turn_status == "failed":
            effective_status = "failed"
        elif turn_status == "cancelled":
            effective_status = "cancelled"
        elif turn_status in {"streaming", "cancelling"}:
            effective_status = "running"
    return {
        "id": task.id,
        "title": task.title,
        "kb_name": task.kb_name,
        "session_id": task.session_id,
        "definition_id": task.definition_id,
        "status": effective_status,
        "query": task.query,
        "plan": list(task.plan or []),
        "verification": dict(task.verification or {}),
        "error_message": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "finished_at": task.finished_at,
        "current_turn": _turn_projection(turn),
        "turns": store.list_turns(task.id),
        "artifacts": [
            _artifact_projection(item) for item in store.list_artifacts(task.id)
        ],
    }


def _create_turn_for_task(
    *,
    task: Task,
    query: str,
    user: AuthUser,
    conv: ConversationService,
    source_scope: str,
):
    turn = conv.create_turn(
        user.id,
        task.session_id,
        query,
        query_mode="deep",
        source_scope=source_scope,
        trace_context={"task_id": task.id},
    )
    return turn


@router.get("/task-definitions")
def list_task_definitions(
    user: AuthUser = Depends(current_user),
) -> dict:
    """List optional external task definitions (skills) for the workbench."""

    from src.agents.task_definitions import load_definitions

    items = []
    for definition in load_definitions().values():
        items.append(
            {
                "id": definition.id,
                "title": definition.title,
                "goal": definition.goal,
                "deliverable": definition.deliverable,
                "coverage": list(definition.coverage),
                "skill": definition.skill,
                "applies_when": list(definition.applies_when),
            }
        )
    return {"items": items}


@router.post("/tasks", status_code=201)
def create_task(
    body: CreateTaskRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> dict:
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query must not be empty")
    definition = resolve_definition(body.definition_id)
    if body.definition_id and definition is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown task definition: {body.definition_id}",
        )

    if body.session_id is not None:
        session = conv.get_session(user.id, body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
    else:
        session = conv.create_session(
            user.id,
            body.kb_name or GENERAL_CHAT_KB_NAME,
            title=(body.title or query[:40] or "新任务"),
        )

    ctx = build_context_for_user(user, session.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if session.kb_name != GENERAL_CHAT_KB_NAME and not ctx.has_kb_permission(
        session.kb_name, "read"
    ):
        raise HTTPException(status_code=403, detail="read permission required")

    task = store.create_task(
        user_id=user.id,
        session_id=session.id,
        kb_name=session.kb_name,
        title=body.title or query[:40] or "新任务",
        definition_id=definition.id if definition else "",
        query=query,
        plan=[],
    )
    try:
        turn = _create_turn_for_task(
            task=task,
            query=query,
            user=user,
            conv=conv,
            source_scope=body.source_scope,
        )
    except (ValueError, PermissionError) as exc:
        store.set_status(task.id, "failed", error_message=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.set_current_turn(task.id, turn.id)
    store.add_turn(task.id, turn.id, "initial")
    refreshed = store.get_task(task.id, user.id) or task
    return {
        "task": _task_projection(refreshed, conv=conv, store=store),
        "turn": {"id": turn.id, "status": turn.status},
    }


@router.get("/tasks")
def list_tasks(
    limit: int = 50,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> dict:
    tasks = store.list_tasks(user.id, limit=limit)
    return {
        "items": [
            _task_projection(task, conv=conv, store=store) for task in tasks
        ]
    }


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> dict:
    task = store.get_task(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _ensure_task_readable(task, user=user, auth=auth, conv=conv)
    return _task_projection(task, conv=conv, store=store)


@router.post("/tasks/{task_id}/messages", status_code=201)
def continue_task(
    task_id: str,
    body: ContinueTaskRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> dict:
    task = store.get_task(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _ensure_task_readable(task, user=user, auth=auth, conv=conv)
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query must not be empty")
    try:
        turn = _create_turn_for_task(
            task=task,
            query=query,
            user=user,
            conv=conv,
            source_scope=body.source_scope,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.set_current_turn(task.id, turn.id)
    store.add_turn(task.id, turn.id, "followup")
    store.set_status(task.id, "queued")
    refreshed = store.get_task(task.id, user.id) or task
    return {
        "task": _task_projection(refreshed, conv=conv, store=store),
        "turn": {"id": turn.id, "status": turn.status},
    }


@router.post("/tasks/{task_id}/stop")
def stop_task(
    task_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> dict:
    task = store.get_task(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _ensure_task_readable(task, user=user, auth=auth, conv=conv)
    if task.current_turn_id:
        try:
            conv.request_turn_cancel(user.id, task.current_turn_id)
        except Exception:
            pass
    store.set_status(task.id, "cancelled", finished=True)
    refreshed = store.get_task(task.id, user.id) or task
    return {"task": _task_projection(refreshed, conv=conv, store=store)}


@router.get("/tasks/{task_id}/events")
def list_task_events(
    task_id: str,
    limit: int = 200,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> dict:
    """Aggregate turn events across the task for the workbench timeline."""

    task = store.get_task(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _ensure_task_readable(task, user=user, auth=auth, conv=conv)
    items: list[dict] = []
    for entry in store.list_turns(task.id)[-10:]:
        turn_id = str(entry["turn_id"])
        try:
            events = conv.list_turn_events(task.session_id, turn_id)
        except Exception:
            continue
        for event in events:
            items.append(
                {
                    "turn_id": turn_id,
                    "seq": int(getattr(event, "seq", 0) or 0),
                    "type": str(getattr(event, "event_type", "") or ""),
                    "payload": dict(getattr(event, "payload", {}) or {}),
                }
            )
    bounded = max(1, min(int(limit), 1000))
    return {"items": items[-bounded:]}


@router.get("/tasks/{task_id}/artifacts")
def list_task_artifacts(
    task_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> dict:
    task = store.get_task(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _ensure_task_readable(task, user=user, auth=auth, conv=conv)
    return {
        "items": [
            _artifact_projection(item) for item in store.list_artifacts(task.id)
        ]
    }


@router.get("/tasks/{task_id}/artifacts/{artifact_id}/download")
def download_task_artifact(
    task_id: str,
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(_conv_service),
    store: TaskStore = Depends(_task_store),
) -> FileResponse:
    task = store.get_task(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _ensure_task_readable(task, user=user, auth=auth, conv=conv)
    artifact = store.get_artifact(artifact_id)
    if artifact is None or artifact.task_id != task.id:
        raise HTTPException(status_code=404, detail="artifact not found")
    from pathlib import Path

    path = Path(artifact.path).resolve()
    root = Path(task.workspace_dir).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found")
    return FileResponse(
        str(path),
        media_type=artifact.mime,
        filename=artifact.name.replace("/", "_"),
    )


__all__ = ["router"]
