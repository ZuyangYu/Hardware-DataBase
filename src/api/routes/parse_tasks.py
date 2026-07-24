"""Parse-task management endpoints.

Reads / stops / bulk-clears the async parse tasks tracked by the pipeline.
RAGFlow parsing itself doesn't support pause/resume — those endpoints exist
for API symmetry but the pipeline returns a "not supported" message.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline
from src.api.schemas import OkResponse, ParseTaskView

router = APIRouter(tags=["parse-tasks"])


def _task_view(task) -> ParseTaskView:
    return ParseTaskView(
        id=task.id,
        kb_name=task.kb_name,
        source_path=getattr(task, "source_path", ""),
        original_name=getattr(task, "original_name", ""),
        source_group=getattr(task, "source_group", ""),
        created_by=getattr(task, "created_by", ""),
        status=getattr(task, "status", ""),
        progress=getattr(task, "progress", 0),
        stage=getattr(task, "stage", ""),
        message=getattr(task, "message", ""),
        result=getattr(task, "result", ""),
        document_id=getattr(task, "document_id", ""),
        created_at=getattr(task, "created_at", None),
        updated_at=getattr(task, "updated_at", None),
        started_at=getattr(task, "started_at", None),
        finished_at=getattr(task, "finished_at", None),
    )


@router.get("/kbs/{kb_name}/parse-tasks", response_model=list[ParseTaskView])
def list_parse_tasks(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """List active parse tasks for a KB (auto-refreshes remote status)."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    tasks = pipeline.list_parse_tasks(kb_name, ctx=ctx) or []
    return [_task_view(t) for t in tasks]


# NOTE: static path segments MUST be registered before parameterised ones,
# otherwise ``/parse-tasks/finished`` gets swallowed by ``{task_id}``.
@router.delete("/kbs/{kb_name}/parse-tasks/finished", response_model=OkResponse)
def clear_finished_parse_tasks(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Clear terminal (completed / failed) parse tasks from the ledger."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    pipeline.clear_finished_parse_tasks(kb_name, ctx=ctx)
    return OkResponse(ok=True, message="finished tasks cleared")


@router.delete("/kbs/{kb_name}/parse-tasks/{task_id}", response_model=OkResponse)
def delete_parse_task(
    kb_name: str,
    task_id: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Stop and remove a parse task (deletes the remote doc + local archive)."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    msg = pipeline.delete_parse_task(task_id, ctx=ctx)
    return OkResponse(ok=True, message=msg or "task deleted")


@router.post("/kbs/{kb_name}/parse-tasks/{task_id}/pause", response_model=OkResponse)
def pause_parse_task(
    kb_name: str,
    task_id: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Pause a parse task. RAGFlow returns "not supported"."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    msg = pipeline.pause_parse_task(task_id, ctx=ctx)
    return OkResponse(ok=True, message=msg or "paused")


@router.post("/kbs/{kb_name}/parse-tasks/{task_id}/resume", response_model=OkResponse)
def resume_parse_task(
    kb_name: str,
    task_id: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Resume a paused parse task. RAGFlow returns "not supported"."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    msg = pipeline.resume_parse_task(task_id, ctx=ctx)
    return OkResponse(ok=True, message=msg or "resumed")