from __future__ import annotations

import os
import shutil
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import UploadAck

router = APIRouter(tags=["upload"])

# Per-request upload cap (bytes across all files). Guards against a single
# oversized multipart body exhausting memory before the registry ever sees it.
MAX_UPLOAD_BYTES = int(os.getenv("HDB_API_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
_CHUNK = 1024 * 1024  # 1 MB stream-and-write chunk


def _safe_base(filename: str | None, used: set[str]) -> str:
    """Return a non-traversal, unique basename within the temp dir.

    ``os.path.basename("..")`` returns ``..`` on POSIX, which would let a
    malicious ``Content-Disposition`` filename escape the temp dir. Reject
    empty/dot/dotdot and de-dup same-named files by appending a counter to
    the stem so multi-part extensions (``.tar.gz``) survive as ``a_1.tar.gz``
    rather than being split at the first dot.
    """
    base = os.path.basename(filename or "upload") or "upload"
    if base in (".", ".."):
        base = "upload"
    if base not in used:
        used.add(base)
        return base
    stem, ext = os.path.splitext(base)
    i = 1
    while f"{stem}_{i}{ext}" in used:
        i += 1
    unique = f"{stem}_{i}{ext}"
    used.add(unique)
    return unique


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
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")

    tmp_dir = tempfile.mkdtemp(prefix="hdb_api_upload_")
    paths: list[str] = []
    total_bytes = 0
    used_names: set[str] = set()
    try:
        for f in files:
            base = _safe_base(f.filename, used_names)
            path = os.path.join(tmp_dir, base)
            # Stream to disk in 1 MB chunks instead of f.read() into memory, so
            # a large PDF/EDF doesn't allocate its full size as one bytes blob.
            with open(path, "wb") as wb:
                while True:
                    chunk = await f.read(_CHUNK)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"upload exceeds {MAX_UPLOAD_BYTES} bytes",
                        )
                    wb.write(chunk)
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
