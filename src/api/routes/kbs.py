from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from src.core.app_pipeline import AppPipeline
from src.core.auth import ROLE_SYSTEM_ADMIN, AuthService, AuthUser
from src.pipelines.document_rag.schemas import (
    DocumentRetrievalContract,
    kb_scope_key,
)

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access, require_employee
from src.api.schemas import (
    CreateDirectoryRequest,
    CreateKbRequest,
    DirectoryDeleteResponse,
    DirectoryView,
    FileView,
    KbView,
    OkResponse,
    RenameDirectoryRequest,
    SemanticSearchRequest,
    UpdateKbSharingRequest,
)
from src.services.pipeline_directories import DirectoryValidationError, PipelineDirectoryService

router = APIRouter(tags=["kbs"])


def _kb_views(user: AuthUser, kbs: list[str], auth: AuthService) -> list[KbView]:
    """构造 KB 视图，并过滤掉当前用户不可见的知识库。

    私有库(未共享)对其他员工必须连名字都不可见；system_admin 走治理视图，
    保留全部登记信息(无内容权限)。
    """
    summaries = auth.list_knowledge_base_summaries(kbs)
    perms = auth.get_kb_permissions_for_user(user)
    sysadmin = user.role == ROLE_SYSTEM_ADMIN
    views: list[KbView] = []
    for s in summaries:
        key = kb_scope_key(s.name, s.department_id)
        if not sysadmin and (not s.registered or key not in perms):
            continue
        is_owner = s.owner_user_id is not None and int(s.owner_user_id) == int(user.id)
        views.append(
            KbView(
                name=s.name,
                kb_id=s.kb_id,
                department_id=s.department_id,
                department_name=s.department_name,
                permission=perms.get(key),
                registered=s.registered,
                owner_user_id=s.owner_user_id,
                is_shared=s.is_shared,
                is_owner=is_owner,
            )
        )
    return views


@router.get("/kbs", response_model=list[KbView])
def list_kbs(
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, auth=auth)
    # system_admin has no dept-scoped KB grants (list_accessible_kbs returns []),
    # so branch to the admin view that lists every KB -- mirrors Streamlit's
    # governance/management panels. Other roles see their accessible KBs.
    if ctx.is_system_admin():
        kbs = pipeline.list_all_knowledge_bases_for_admin(ctx=ctx)
    else:
        kbs = pipeline.list_knowledge_bases(ctx=ctx)
    return _kb_views(user, kbs, auth)


@router.get("/kbs/{kb_name}/files", response_model=list[FileView])
def list_files(
    kb_name: str,
    directory_id: int | None = Query(default=None, ge=0),
    recursive: bool = Query(default=False),
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    directory_service = _directory_service(pipeline)
    if directory_id is not None:
        try:
            if recursive:
                allowed_directory_ids = directory_service.descendant_ids(
                    kb_name=kb_name,
                    department_id=_ctx_department_id(ctx),
                    directory_id=directory_id,
                )
            else:
                directory_service.get(
                    kb_name=kb_name,
                    department_id=_ctx_department_id(ctx),
                    directory_id=directory_id,
                )
                allowed_directory_ids = {int(directory_id)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if recursive and allowed_directory_ids and 0 not in allowed_directory_ids:
            infos = []
            for current_id in sorted(allowed_directory_ids):
                infos.extend(
                    pipeline.list_file_infos(
                        kb_name, ctx=ctx, directory_id=current_id
                    )
                )
        else:
            infos = pipeline.list_file_infos(
                kb_name,
                ctx=ctx,
                directory_id=None if recursive else directory_id,
            )
    else:
        allowed_directory_ids = None
        infos = pipeline.list_file_infos(kb_name, ctx=ctx)
    needle = query.strip().casefold()
    rows: list[FileView] = []
    for info in infos:
        metadata = info.metadata or {}
        info_directory_id = int(metadata.get("directory_id") or 0)
        if allowed_directory_ids is not None and info_directory_id not in allowed_directory_ids:
            continue
        relative_path = str(metadata.get("relative_path") or info.name)
        if needle and needle not in relative_path.casefold():
            continue
        rows.append(
            FileView(
                id=info.id,
                name=info.name,
                status=info.status,
                processor_kind=info.processor_kind,
                dataset_kind=info.dataset_kind,
                directory_id=info_directory_id,
                relative_path=relative_path,
                display_name=str(metadata.get("display_name") or info.name),
                metadata=metadata,
            )
        )
    return rows[offset: offset + limit]


@router.post("/kbs/{kb_name}/semantic-search")
def semantic_search(
    kb_name: str,
    body: SemanticSearchRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Semantic document retrieval through the platform RAGFlow backend."""

    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    directory_service = _directory_service(pipeline)
    allowed_ids: tuple[int, ...] | None = None
    try:
        allowed_directory_ids: set[int] | None = None
        if body.directory_id:
            allowed_directory_ids = (
                directory_service.descendant_ids(
                    kb_name=kb_name,
                    department_id=_ctx_department_id(ctx),
                    directory_id=body.directory_id,
                )
                if body.recursive
                else {int(body.directory_id)}
            )
            allowed_ids = _ragflow_record_ids(
                pipeline, kb_name, ctx, allowed_directory_ids
            )
        if body.file_id:
            infos = (
                pipeline.list_file_infos(kb_name, ctx=ctx)
                if allowed_directory_ids is None
                else [
                    info
                    for directory_id in sorted(allowed_directory_ids)
                    for info in pipeline.list_file_infos(
                        kb_name, ctx=ctx, directory_id=directory_id
                    )
                ]
            )
            info = next(
                (
                    item
                    for item in infos
                    if str(getattr(item, "id", "")) == body.file_id
                    or str(getattr(item, "name", "")) == body.file_id
                ),
                None,
            )
            if info is None:
                raise LookupError("file not found")
            record_id = int((info.metadata or {}).get("store_id") or 0)
            if not record_id:
                raise LookupError("file has no local record")
            allowed_ids = (record_id,)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    retrieve = getattr(pipeline.backend, "retrieve_with_scope", None)
    if not callable(retrieve):
        raise HTTPException(
            status_code=501, detail="semantic retrieval backend is unavailable"
        )
    outcome = retrieve(
        kb_name,
        body.query,
        contract=DocumentRetrievalContract(
            mode="chat", allowed_record_ids=allowed_ids
        ),
        top_k=body.top_k,
        ctx=ctx,
    )
    results = [_semantic_hit(item) for item in (outcome.evidence or [])]
    return {
        "kb_name": kb_name,
        "query": body.query,
        "mode": "semantic",
        "directory_id": int(body.directory_id or 0),
        "recursive": bool(body.recursive),
        "status": outcome.status,
        "error_code": outcome.error_code,
        "results": results,
        "result_count": len(results),
        "files_searched": len(allowed_ids) if allowed_ids is not None else 0,
        "chunks_searched": 0,
    }


def _ragflow_record_ids(
    pipeline: AppPipeline, kb_name: str, ctx, directory_ids: set[int]
) -> tuple[int, ...]:
    records: set[int] = set()
    for directory_id in sorted(directory_ids):
        for info in pipeline.list_file_infos(
            kb_name, ctx=ctx, directory_id=directory_id
        ):
            metadata = info.metadata or {}
            if str(metadata.get("processor_kind") or "") != "ragflow":
                continue
            record_id = int(metadata.get("store_id") or 0)
            if record_id:
                records.add(record_id)
    return tuple(sorted(records))


def _semantic_hit(item) -> dict:
    metadata = dict(getattr(item, "metadata", {}) or {})
    locator = dict(getattr(item, "locator", {}) or metadata.get("locator") or {})
    record_id = int(metadata.get("local_record_id") or metadata.get("store_id") or 0)
    chunk_id = str(metadata.get("chunk_id") or locator.get("chunk_id") or "")
    chunk_index = int(
        metadata.get("chunk_index") or locator.get("chunk_index") or 0
    )
    return {
        "file_id": f"ragflow:{record_id}" if record_id else "",
        "file_name": str(
            metadata.get("relative_path")
            or getattr(item, "source_name", "")
            or ""
        ),
        "chunk_index": chunk_index,
        "rag_id": chunk_id,
        "locator": locator,
        "score": float(getattr(item, "score", 0.0) or 0.0),
        "snippet": str(getattr(item, "content", "") or "")[:400],
        "citation_id": (
            f"{metadata.get('kb_id') or ''}:{record_id}:{chunk_id or chunk_index}"
        ),
    }


def _ctx_department_id(ctx) -> str:
    value = ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id")
    if value in (None, ""):
        raise HTTPException(status_code=400, detail="department_id is required")
    return str(value)


def _directory_service(pipeline: AppPipeline) -> PipelineDirectoryService:
    backend = getattr(pipeline, "backend", None)
    store = getattr(backend, "store", None)
    if store is None:
        store = getattr(getattr(pipeline, "documents", None), "processing_backend", None)
        store = getattr(store, "store", None)
    return PipelineDirectoryService(store=store)


@router.get("/kbs/{kb_name}/directories", response_model=list[DirectoryView])
def list_directories(
    kb_name: str,
    parent_id: int = Query(default=0, ge=0),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    try:
        return _directory_service(pipeline).list_children(
            kb_name=kb_name,
            department_id=_ctx_department_id(ctx),
            parent_id=parent_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/kbs/{kb_name}/directories/{directory_id}",
    response_model=DirectoryView,
)
def get_directory(
    kb_name: str,
    directory_id: int,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    try:
        return _directory_service(pipeline).get(
            kb_name=kb_name,
            department_id=_ctx_department_id(ctx),
            directory_id=directory_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/kbs/{kb_name}/directories", response_model=DirectoryView)
def create_directory(
    kb_name: str,
    body: CreateDirectoryRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    try:
        return _directory_service(pipeline).create(
            kb_name=kb_name,
            department_id=_ctx_department_id(ctx),
            kb_id=ctx.metadata.get("kb_id"),
            parent_id=body.parent_id,
            name=body.name,
            created_by=ctx.user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DirectoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/kbs/{kb_name}/directories/{directory_id}", response_model=DirectoryView)
def rename_directory(
    kb_name: str,
    directory_id: int,
    body: RenameDirectoryRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "admin"):
        raise HTTPException(status_code=403, detail="admin permission required")
    try:
        directory_service = _directory_service(pipeline)
        if body.parent_id is not None:
            return directory_service.move(
                kb_name=kb_name,
                department_id=_ctx_department_id(ctx),
                directory_id=directory_id,
                parent_id=body.parent_id,
                name=body.name,
            )
        return directory_service.rename(
            kb_name=kb_name,
            department_id=_ctx_department_id(ctx),
            directory_id=directory_id,
            name=body.name,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DirectoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/kbs/{kb_name}/directories/{directory_id}", response_model=DirectoryDeleteResponse)
def delete_directory(
    kb_name: str,
    directory_id: int,
    recursive: bool = Query(default=False),
    confirm: bool = Query(default=False),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "admin"):
        raise HTTPException(status_code=403, detail="admin permission required")
    try:
        if recursive:
            if int(directory_id) == 0:
                raise DirectoryValidationError("根目录不能删除")
            if not confirm:
                raise HTTPException(
                    status_code=400,
                    detail="recursive directory deletion requires confirm=true",
                )
            result = pipeline.delete_directory_recursive(kb_name, directory_id, ctx=ctx)
            if not result.get("ok"):
                errors = "; ".join(str(item) for item in result.get("errors", [])[:5])
                raise HTTPException(
                    status_code=409,
                    detail=f"{result.get('message', '递归删除失败')}: {errors}".rstrip(": "),
                )
            return DirectoryDeleteResponse(**result)
        _directory_service(pipeline).delete_empty(
            kb_name=kb_name,
            department_id=_ctx_department_id(ctx),
            directory_id=directory_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DirectoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DirectoryDeleteResponse(ok=True, message="目录已删除", deleted_directories=1)


@router.post("/kbs", response_model=OkResponse)
def create_kb(
    body: CreateKbRequest,
    user=Depends(require_employee),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, auth=auth)
    ok, msg = pipeline.create_kb(body.name, ctx=ctx)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return OkResponse(ok=True, message=msg)


@router.patch("/kbs/{kb_name}/sharing", response_model=OkResponse)
def update_kb_sharing(
    kb_name: str,
    body: UpdateKbSharingRequest,
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
):
    """切换部门共享状态；仅所有者可操作，服务端同时落审计。

    共享后本部门其他员工获得该知识库完整 admin 权限；改回私有则立即收回。
    """
    ctx = build_context_for_user(user, kb_name, auth=auth)
    kb_id = ctx.metadata.get("kb_id")
    try:
        auth.set_knowledge_base_sharing(
            kb_name,
            user,
            body.is_shared,
            department_id=user.department_id,
            kb_id=int(kb_id) if kb_id is not None else None,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    message = f"知识库 '{kb_name}' 已设为部门共享" if body.is_shared else f"知识库 '{kb_name}' 已改为私有"
    return OkResponse(ok=True, message=message)


@router.delete("/kbs/{kb_name}", response_model=OkResponse)
def delete_kb(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Delete a knowledge base and all its documents / archives / indexes.

    Requires ``admin`` permission on the KB; the pipeline additionally enforces
    that only the registered owner may delete a knowledge base.
    """
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "admin"):
        raise HTTPException(status_code=403, detail="admin permission required")
    ok, msg = pipeline.delete_knowledge_base(kb_name, ctx=ctx)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return OkResponse(ok=True, message=msg)


@router.delete("/kbs/{kb_name}/files/{filename}", response_model=OkResponse)
def delete_file(
    kb_name: str,
    filename: str,
    directory_id: int | None = Query(default=None, ge=0),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "admin"):
        raise HTTPException(status_code=403, detail="admin permission required")
    msg = pipeline.delete_document(
        filename, kb_name, ctx=ctx, directory_id=directory_id
    )
    return OkResponse(ok=True, message=msg)
