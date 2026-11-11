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
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
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
    """List active parse tasks for a KB (auto-refreshes remote status).

    Requires write -- the Streamlit parse-task panel is only shown to users
    with write on the KB (employees managing ingestion). read-only access
    see file status via GET /kbs/{kb}/files instead.
    """
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    tasks = pipeline.list_parse_tasks(kb_name, ctx=ctx) or []
    return [_task_view(t) for t in tasks]


@router.get(
    "/kbs/{kb_name}/parse-tasks/{task_id}", response_model=ParseTaskView
)
def get_parse_task(
    kb_name: str,
    task_id: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Read one parse task by its task id (read permission)."""

    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    tasks = pipeline.list_parse_tasks(kb_name, ctx=ctx) or []
    task = next(
        (
            item
            for item in tasks
            if str(getattr(item, "id", "")) == str(task_id)
            or str(getattr(item, "document_id", "")) == str(task_id)
        ),
        None,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="parse task not found")
    return _task_view(task)


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
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    pipeline.clear_finished_parse_tasks(kb_name, ctx=ctx)
    return OkResponse(ok=True, message="finished tasks cleared")


@router.post("/kbs/{kb_name}/parse-tasks/requeue-dead-letters", response_model=OkResponse)
def requeue_dead_letter_parse_tasks(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Re-queue dead-lettered parse records so the worker can retry them.

    Background parsing fails into ``dead_letter`` after exhausting its retry
    budget; transient causes (remote backend hiccups, stale workers) leave
    recoverable records stranded.  This resets them to ``queued``.
    """
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    names = pipeline.requeue_dead_letter_parse_tasks(kb_name, ctx=ctx)
    message = f"requeued {len(names)} dead-letter task(s)" if names else "no dead-letter tasks"
    return OkResponse(ok=True, message=message)


@router.post(
    "/kbs/{kb_name}/parse-tasks/{task_id}/retry", response_model=OkResponse
)
def retry_parse_task(
    kb_name: str,
    task_id: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Re-submit one parse task without re-uploading its source file."""

    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    result = pipeline.documents.retry_parse_task(task_id, ctx=ctx)
    if not getattr(result, "ok", False):
        raise HTTPException(
            status_code=400,
            detail=getattr(result, "message", "parse retry failed"),
        )
    return OkResponse(ok=True, message=getattr(result, "message", "parse retried"))


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
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    msg = pipeline.delete_parse_task(task_id, ctx=ctx)
    return OkResponse(ok=True, message=msg or "task deleted")


