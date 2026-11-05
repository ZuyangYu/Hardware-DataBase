"""Conversation-scoped artifacts.

The user-facing model is "outputs live in the conversation", not "files live
on a path".  Workspace paths stay internal to the agent; the browser only
sees artifact metadata and an id-based download URL.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from src.api.deps import current_user
from src.core.auth import AuthUser
from src.core.conversation import ConversationService
from src.core.task_store import get_task_store

router = APIRouter(tags=["artifacts"])


def _conv_service() -> ConversationService:
    return ConversationService()


def _task_store():
    return get_task_store()


def _artifact_view(artifact) -> dict:
    return {
        "id": artifact.id,
        "turn_id": artifact.turn_id,
        "name": artifact.name,
        "kind": artifact.kind,
        "mime": artifact.mime,
        "size_bytes": artifact.size_bytes,
        "version": artifact.version,
        "created_at": artifact.created_at,
        "download_url": f"/api/v1/artifacts/{artifact.id}/download",
    }


@router.get("/conversations/{session_id}/artifacts")
def list_conversation_artifacts(
    session_id: int,
    user: AuthUser = Depends(current_user),
    conv: ConversationService = Depends(_conv_service),
    store=Depends(_task_store),
) -> dict:
    """List every artifact produced in one conversation (newest first)."""

    session = conv.get_session(user.id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    items: list[dict] = []
    seen: set[str] = set()
    for task in store.list_tasks_for_session(user.id, session_id):
        for artifact in store.list_artifacts(task.id):
            if artifact.id in seen:
                continue
            seen.add(artifact.id)
            item = _artifact_view(artifact)
            item["task_id"] = task.id
            items.append(item)
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": items}


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    store=Depends(_task_store),
) -> FileResponse:
    artifact = store.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    task = store.get_task(artifact.task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="artifact not found")
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
