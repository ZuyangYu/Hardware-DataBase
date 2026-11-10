"""Durable folder-import batches (P1)."""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    CreateDirectoryImportPlanRequest,
    DirectoryImportFileView,
    DirectoryImportJobView,
    OkResponse,
)
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.services.directory_imports import (
    DirectoryImportService,
    cleanup_staging_dir,
    create_staging_dir,
)
from src.services.pipeline_directories import (
    DirectoryValidationError,
    PipelineDirectoryService,
    parse_relative_path,
)

router = APIRouter(tags=["directory-imports"])

MAX_IMPORT_FILES = int(os.getenv("HDB_DIRECTORY_IMPORT_MAX_FILES", "2000"))
MAX_IMPORT_BYTES = int(os.getenv("HDB_DIRECTORY_IMPORT_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
_CHUNK = 1024 * 1024


def _department_id(ctx) -> str:
    value = ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id")
    if value in (None, ""):
        raise HTTPException(status_code=400, detail="department_id is required")
    return str(value)


def _store(pipeline: AppPipeline):
    backend = getattr(pipeline, "backend", None)
    store = getattr(backend, "store", None)
    if store is None:
        store = getattr(getattr(pipeline, "documents", None), "processing_backend", None)
        store = getattr(store, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="pipeline document store unavailable")
    return store


def _job_view(job: dict, files: list[dict] | None = None) -> DirectoryImportJobView:
    return DirectoryImportJobView(
        **{key: job.get(key) for key in DirectoryImportJobView.model_fields if key != "files"},
        files=[
            DirectoryImportFileView(
                **{key: row.get(key) for key in DirectoryImportFileView.model_fields}
            )
            for row in (files or [])
        ],
    )


@router.post("/kbs/{kb_name}/directory-imports", response_model=DirectoryImportJobView)
async def create_directory_import(
    kb_name: str,
    files: list[UploadFile] = File(...),
    directory_id: int = Form(default=0),
    source_group: str | None = Form(default=None),
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
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(status_code=413, detail=f"folder import exceeds {MAX_IMPORT_FILES} files")
    department_id = _department_id(ctx)
    store = _store(pipeline)
    directories = PipelineDirectoryService(store=store)
    try:
        directories.get(kb_name=kb_name, department_id=department_id, directory_id=directory_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    entries = _manifest_entries(files, manifest, source_group)
    staging = create_staging_dir()
    stored: list[dict] = []
    total_bytes = 0
    try:
        os.makedirs(staging, exist_ok=True)
        for index, upload in enumerate(files):
            entry = entries[index]
            logical_name = os.path.basename(
                str(entry.get("relative_path") or upload.filename or "upload")
            ) or "upload"
            target = os.path.join(staging, f"{index:05d}_{logical_name}")
            with open(target, "wb") as handle:
                while True:
                    chunk = await upload.read(_CHUNK)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_IMPORT_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"folder import exceeds {MAX_IMPORT_BYTES} bytes",
                        )
                    handle.write(chunk)
            stored.append(
                {
                    **entry,
                    "staged_path": target,
                    "file_size": os.path.getsize(target),
                }
            )
        service = DirectoryImportService(store=store, pipeline=pipeline, auth=auth)
        job = service.create_job(
            kb_name=kb_name,
            department_id=department_id,
            kb_id=ctx.metadata.get("kb_id"),
            directory_id=directory_id,
            created_by=str(ctx.user_id or user.username),
            entries=stored,
        )
    except (DirectoryValidationError, ValueError) as exc:
        cleanup_staging_dir(staging)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        cleanup_staging_dir(staging)
        raise
    except Exception as exc:  # noqa: BLE001 - do not leave staged bytes on failure
        cleanup_staging_dir(staging)
        raise HTTPException(status_code=500, detail=f"directory import creation failed: {exc}") from exc
    audit = getattr(pipeline, "_audit", None)
    if callable(audit):
        try:
            audit(
                "directory_import_started",
                ctx,
                target_type="directory_import",
                target_id=str(job["id"]),
                kb_name=kb_name,
                metadata={"total_files": job["total_files"], "directory_id": directory_id},
            )
        except Exception:
            pass
    return _job_view(job)


def _manifest_entries(
    files: list[UploadFile],
    manifest: str | None,
    source_group: str | None,
) -> list[dict]:
    if manifest in (None, ""):
        raw_entries = [
            {
                "index": index,
                "relative_path": os.path.basename(upload.filename or "upload"),
                "source_group": source_group,
            }
            for index, upload in enumerate(files)
        ]
    else:
        try:
            payload = json.loads(manifest)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="manifest 不是有效 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise HTTPException(status_code=400, detail="manifest.entries 必须是数组")
        raw_entries = payload["entries"]
        if len(raw_entries) != len(files):
            raise HTTPException(status_code=400, detail="manifest 条目数与文件数不一致")
    normalized: list[dict | None] = [None] * len(files)
    for fallback_index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="manifest 条目必须是对象")
        try:
            index = int(raw.get("index", fallback_index))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="manifest.index 必须是整数") from exc
        if index < 0 or index >= len(files) or normalized[index] is not None:
            raise HTTPException(status_code=400, detail="manifest.index 必须唯一且覆盖全部文件")
        relative_path = str(raw.get("relative_path") or files[index].filename or "upload")
        try:
            parse_relative_path(relative_path)
        except DirectoryValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        normalized[index] = {
            "relative_path": relative_path,
            "source_group": str(raw.get("source_group") or source_group or ""),
        }
    if any(item is None for item in normalized):
        raise HTTPException(status_code=400, detail="manifest.index 必须覆盖全部文件")
    return [item for item in normalized if item is not None]


@router.post("/kbs/{kb_name}/directory-imports/plan", response_model=DirectoryImportJobView)
def create_directory_import_plan(
    kb_name: str,
    body: CreateDirectoryImportPlanRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Create an empty batch; files can follow one at a time (file-level resume)."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    if len(body.entries) > MAX_IMPORT_FILES:
        raise HTTPException(status_code=413, detail=f"folder import exceeds {MAX_IMPORT_FILES} files")
    store = _store(pipeline)
    service = DirectoryImportService(store=store, pipeline=pipeline, auth=auth)
    try:
        job = service.create_job(
            kb_name=kb_name,
            department_id=_department_id(ctx),
            kb_id=ctx.metadata.get("kb_id"),
            directory_id=body.directory_id,
            created_by=str(ctx.user_id or user.username),
            entries=[
                {
                    "relative_path": item.relative_path,
                    "source_group": item.source_group,
                    "staged_path": "",
                    "file_size": 0,
                }
                for item in body.entries
            ],
        )
    except (DirectoryValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    files = store.list_directory_import_files(
        job["id"], kb_name, _department_id(ctx)
    )
    return _job_view(job, files)


@router.put(
    "/kbs/{kb_name}/directory-imports/{job_id}/files/{file_id}/content",
    response_model=DirectoryImportFileView,
)
async def upload_directory_import_file(
    kb_name: str,
    job_id: int,
    file_id: int,
    file: UploadFile = File(...),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Attach or replace one staged file, enabling file-level resume."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    store = _store(pipeline)
    department_id = _department_id(ctx)
    job = store.get_directory_import_job(job_id, kb_name, department_id)
    if job is None:
        raise HTTPException(status_code=404, detail="directory import job not found")
    rows = store.list_directory_import_files(job_id, kb_name, department_id, limit=5000)
    row = next((item for item in rows if int(item["id"]) == int(file_id)), None)
    if row is None:
        raise HTTPException(status_code=404, detail="directory import file not found")
    staging = os.path.dirname(str(row.get("staged_path") or "")) or create_staging_dir()
    os.makedirs(staging, exist_ok=True)
    original_name = os.path.basename(
        str(row.get("original_name") or row.get("relative_path") or "upload")
    ) or "upload"
    target = os.path.join(staging, f"{int(file_id):05d}_{original_name}")
    total = 0
    with open(target, "wb") as handle:
        while True:
            chunk = await file.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_IMPORT_BYTES:
                raise HTTPException(status_code=413, detail="file exceeds import size limit")
            handle.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="file content is empty")
    store.update_directory_import_file_stage(
        int(file_id), int(job_id), target, file_size=total
    )
    refreshed = store.list_directory_import_files(job_id, kb_name, department_id, limit=5000)
    updated = next(item for item in refreshed if int(item["id"]) == int(file_id))
    return DirectoryImportFileView(
        **{key: updated.get(key) for key in DirectoryImportFileView.model_fields}
    )


@router.get("/kbs/{kb_name}/directory-imports", response_model=list[DirectoryImportJobView])
def list_directory_imports(
    kb_name: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    store = _store(pipeline)
    rows = store.list_directory_import_jobs(
        kb_name, _department_id(ctx), limit=limit, offset=offset
    )
    return [_job_view(row) for row in rows]


@router.get("/kbs/{kb_name}/directory-imports/{job_id}", response_model=DirectoryImportJobView)
def get_directory_import(
    kb_name: str,
    job_id: int,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    store = _store(pipeline)
    department_id = _department_id(ctx)
    job = store.get_directory_import_job(job_id, kb_name, department_id)
    if job is None:
        raise HTTPException(status_code=404, detail="directory import job not found")
    files = store.list_directory_import_files(job_id, kb_name, department_id)
    return _job_view(job, files)


@router.post("/kbs/{kb_name}/directory-imports/{job_id}/retry", response_model=DirectoryImportJobView)
def retry_directory_import(
    kb_name: str,
    job_id: int,
    file_id: int | None = Query(default=None, ge=1),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    store = _store(pipeline)
    department_id = _department_id(ctx)
    job = store.retry_directory_import(
        job_id, kb_name, department_id, file_id=file_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="directory import job not found")
    files = store.list_directory_import_files(job_id, kb_name, department_id)
    return _job_view(job, files)


@router.post("/kbs/{kb_name}/directory-imports/{job_id}/cancel", response_model=DirectoryImportJobView)
def cancel_directory_import(
    kb_name: str,
    job_id: int,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    store = _store(pipeline)
    department_id = _department_id(ctx)
    job = store.request_directory_import_cancel(job_id, kb_name, department_id)
    if job is None:
        raise HTTPException(status_code=404, detail="directory import job not found")
    files = store.list_directory_import_files(job_id, kb_name, department_id)
    return _job_view(job, files)


@router.delete("/kbs/{kb_name}/directory-imports/{job_id}", response_model=OkResponse)
def delete_directory_import(
    kb_name: str,
    job_id: int,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Delete one import-history record; imported files are never removed."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    store = _store(pipeline)
    department_id = _department_id(ctx)
    try:
        staged_paths = store.delete_directory_import_job(job_id, kb_name, department_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    for staged in staged_paths:
        try:
            os.remove(staged)
        except OSError:
            pass
    for root in {os.path.dirname(path) for path in staged_paths}:
        cleanup_staging_dir(root)
    return OkResponse(ok=True, message="导入批次记录已删除；已导入文件不受影响")


__all__ = ["router"]
