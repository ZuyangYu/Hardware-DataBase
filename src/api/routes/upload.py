from __future__ import annotations

import os
import shutil
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline
from src.api.schemas import UploadAck

router = APIRouter(tags=["upload"])


@router.post("/kbs/{kb_name}/files", response_model=UploadAck)
async def upload_files(
    kb_name: str,
    files: list[UploadFile] = File(...),
    source_group: str | None = Form(default=None),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")

    tmp_dir = tempfile.mkdtemp(prefix="hdb_api_upload_")
    paths: list[str] = []
    try:
        for f in files:
            base = os.path.basename(f.filename or "upload")
            path = os.path.join(tmp_dir, base)
            with open(path, "wb") as wb:
                wb.write(await f.read())
            paths.append(path)
        # upload_files is a blocking call (RAGFlow HTTP); run off the event loop.
        result = await run_in_threadpool(pipeline.upload_files, paths, kb_name, ctx, source_group)
        return UploadAck(
            success_count=result.success_count,
            total_count=result.total_count,
            failed_count=result.failed_count,
            skipped_count=result.skipped_count,
            status=result.status,
            messages=result.messages,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
