from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.app_pipeline import AppPipeline
from src.core.auth import ROLE_SYSTEM_ADMIN, AuthService, AuthUser
from src.pipelines.document_rag.schemas import kb_scope_key

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access, require_employee
from src.api.schemas import (
    CreateKbRequest,
    FileView,
    KbView,
    OkResponse,
    UpdateKbSharingRequest,
)

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
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    infos = pipeline.list_file_infos(kb_name, ctx=ctx)
    return [
        FileView(
            id=info.id,
            name=info.name,
            status=info.status,
            processor_kind=info.processor_kind,
            dataset_kind=info.dataset_kind,
            metadata=info.metadata or {},
        )
        for info in infos
    ]


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
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "admin"):
        raise HTTPException(status_code=403, detail="admin permission required")
    msg = pipeline.delete_document(filename, kb_name, ctx=ctx)
    return OkResponse(ok=True, message=msg)
