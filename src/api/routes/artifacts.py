"""Conversation-scoped workspace artifacts.

The user-facing model is "outputs live in the conversation", not "files live
on a path".  Workspace paths stay internal to the agent; the browser only sees
artifact metadata and an id-based download URL.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from src.api.deps import current_user
from src.core.auth import AuthUser
from src.core.conversation import ConversationService
from src.core.workspace_store import get_workspace_store

router = APIRouter(tags=["artifacts"])


def _conv_service() -> ConversationService:
    return ConversationService()


def _workspace_store():
    return get_workspace_store()


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
    workspace_store=Depends(_workspace_store),
) -> dict:
    """List artifacts produced by one session in its workspace."""

    session = conv.get_session(user.id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    workspace = workspace_store.get_workspace_for_session(session_id)
    if workspace is None or workspace.user_id != user.id:
        return {"items": []}
    items = [
        _artifact_view(artifact)
        for artifact in workspace_store.list_artifacts(
            workspace.id, session_id=session_id
        )
    ]
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": items}


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    workspace_store=Depends(_workspace_store),
) -> Response:
    artifact = workspace_store.get_artifact(artifact_id)
    if artifact is None:
        # Result exports intentionally use the same public download URL shape
        # as conversation workspace artifacts.  This router is registered
        # first, so delegate only when the workspace store has no matching
        # artifact; existing workspace downloads keep their original path.
        from src.api.routes.exports import artifact_download
        from src.core.auth import AuthService
        from src.result_exports.store import ResultExportStore

        export_store = ResultExportStore()
        if export_store.get_artifact(user.id, artifact_id) is not None:
            return artifact_download(
                artifact_id,
                user=user,
                auth=AuthService(),
                store=export_store,
            )
        raise HTTPException(status_code=404, detail="artifact not found")
    workspace = workspace_store.get_workspace(
        artifact.workspace_id,
        user.id,
    )
    if workspace is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    path = Path(artifact.path).resolve()
    root = Path(workspace.workspace_dir).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found")
    return FileResponse(
        str(path),
        media_type=artifact.mime,
        filename=artifact.name.replace("/", "_"),
    )


__all__ = ["router"]
