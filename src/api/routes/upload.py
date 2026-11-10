from __future__ import annotations

import json
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
from src.ingestion.source_groups import SOURCE_GROUPS, classify_source_group
from src.services.pipeline_directories import (
    DirectoryValidationError,
    PipelineDirectoryService,
    parse_relative_path,
)

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
    directory_id: int = Form(default=0),
    manifest: str | None = Form(default=None),
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

    backend = getattr(pipeline, "backend", None)
    store = getattr(backend, "store", None)
    if store is None:
        backend = getattr(getattr(pipeline, "documents", None), "processing_backend", None)
        store = getattr(backend, "store", None)
    directories = PipelineDirectoryService(store=store)
    department_id = ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id")
    if department_id in (None, ""):
        raise HTTPException(status_code=400, detail="department_id is required")
    try:
        directories.get(kb_name=kb_name, department_id=str(department_id), directory_id=directory_id)
        metadata = _build_file_metadata(
            files,
            manifest=manifest,
            base_directory_id=directory_id,
            source_group=source_group,
            directories=directories,
            kb_name=kb_name,
            department_id=str(department_id),
            kb_id=ctx.metadata.get("kb_id"),
            created_by=ctx.user_id,
        )
        incoming: dict[int, tuple[int, int]] = {}
        for index, item in enumerate(metadata):
            target_id = int(item["directory_id"])
            count, size = incoming.get(target_id, (0, 0))
            incoming[target_id] = (
                count + 1,
                size + int(getattr(files[index], "size", 0) or 0),
            )
        directories.assert_quota(
            kb_name=kb_name, department_id=str(department_id), incoming=incoming
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DirectoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        try:
            result = await run_in_threadpool(
                pipeline.upload_files,
                paths,
                kb_name,
                ctx=ctx,
                source_group=source_group,
                directory_id=directory_id,
                file_metadata=metadata,
            )
        except TypeError as exc:
            # Keep lightweight/third-party pipeline adapters on the legacy
            # four-argument contract; the built-in AppPipeline takes the
            # directory-aware branch above.
            if "unexpected keyword argument" not in str(exc):
                raise
            result = await run_in_threadpool(
                pipeline.upload_files,
                paths,
                kb_name,
                ctx,
                source_group,
            )
        return UploadAck(
            success_count=result.success_count,
            total_count=result.total_count,
            failed_count=result.failed_count,
            skipped_count=result.skipped_count,
            status=result.status,
            messages=result.messages,
            files=[
                {
                    "index": item.index,
                    "relative_path": item.relative_path,
                    "status": item.status,
                    "record_id": item.record_id,
                    "error_code": item.error_code,
                    "message": item.message,
                }
                for item in getattr(result, "file_results", [])
            ],
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _build_file_metadata(
    files: list[UploadFile],
    *,
    manifest: str | None,
    base_directory_id: int,
    source_group: str | None,
    directories: PipelineDirectoryService,
    kb_name: str,
    department_id: str,
    kb_id: int | None,
    created_by: str,
) -> list[dict]:
    """Validate the untrusted folder manifest and resolve logical directories."""
    if source_group not in (None, "") and source_group not in SOURCE_GROUPS:
        raise DirectoryValidationError("source_group 不合法")
    entries: list[dict]
    if manifest in (None, ""):
        entries = [
            {
                "index": index,
                "relative_path": (
                    os.path.basename(f.filename or "upload")
                    if os.path.basename(f.filename or "upload") not in {".", ".."}
                    else "upload"
                ),
            }
            for index, f in enumerate(files)
        ]
    else:
        try:
            payload = json.loads(manifest)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DirectoryValidationError("manifest 不是有效 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise DirectoryValidationError("manifest.entries 必须是数组")
        entries = payload["entries"]
        if len(entries) != len(files):
            raise DirectoryValidationError("manifest 条目数与文件数不一致")
    if len(entries) != len(files):
        raise DirectoryValidationError("manifest 条目数与文件数不一致")

    normalized: list[dict | None] = [None] * len(files)
    for fallback_index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            raise DirectoryValidationError("manifest 条目必须是对象")
        try:
            index = int(raw_entry.get("index", fallback_index))
        except (TypeError, ValueError) as exc:
            raise DirectoryValidationError("manifest.index 必须是整数") from exc
        if index < 0 or index >= len(files) or normalized[index] is not None:
            raise DirectoryValidationError("manifest.index 必须唯一且覆盖全部文件")
        relative_path = str(raw_entry.get("relative_path") or files[index].filename or "upload")
        directory_segments, filename = parse_relative_path(relative_path)
        group_value = raw_entry.get("source_group", source_group)
        if group_value in (None, ""):
            group_value = classify_source_group(filename).group
        if group_value not in SOURCE_GROUPS:
            raise DirectoryValidationError(f"source_group 不合法: {group_value}")
        resolved_directory_id = directories.ensure_path(
            kb_name=kb_name,
            department_id=department_id,
            kb_id=kb_id,
            parent_id=base_directory_id,
            segments=directory_segments,
            created_by=created_by,
        ) if directory_segments else int(base_directory_id)
        normalized[index] = {
            "directory_id": resolved_directory_id,
            "relative_path": "/".join(
                [directories.path_for_directory(
                    kb_name=kb_name,
                    department_id=department_id,
                    directory_id=base_directory_id,
                ), *directory_segments, filename]
            ).strip("/"),
            "source_group": group_value,
        }
    if any(item is None for item in normalized):
        raise DirectoryValidationError("manifest.index 必须覆盖全部文件")
    return [item for item in normalized if item is not None]
