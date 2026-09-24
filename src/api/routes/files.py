"""File-level operations beyond the basic list/upload/delete in kbs.py.

Currently exposes the parse-result (chunks) endpoint. The other file ops
live in kbs.py / upload.py alongside their KB parents.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import ChunkView, ParseResultView

router = APIRouter(tags=["files"])


@router.get("/kbs/{kb_name}/files/{file_id}/chunks", response_model=ParseResultView)
def get_file_chunks(
    kb_name: str,
    file_id: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Return the parsed chunks for a document (RAGFlow-parsed content)."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    result = pipeline.get_parse_result(kb_name, file_id, ctx=ctx)
    if result is None:
        raise HTTPException(status_code=404, detail="parse result not found")
    return ParseResultView(
        document_id=result.document_id,
        file_name=result.file_name,
        chunk_count=result.chunk_count,
        chunks=[
            ChunkView(index=c.index, content=c.content, metadata=c.metadata)
            for c in result.chunks
        ],
        backend=result.backend,
    )