from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.pipelines.document_rag.schemas import kb_scope_key

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, require_dept_admin
from src.api.schemas import CreateKbRequest, FileView, KbView, OkResponse

router = APIRouter(tags=["kbs"])


def _kb_views(user: AuthUser, kbs: list[str], auth: AuthService) -> list[KbView]:
    summaries = auth.list_knowledge_base_summaries(kbs)
    perms = auth.get_kb_permissions_for_user(user)
    views: list[KbView] = []
    for s in summaries:
        key = kb_scope_key(s.name, s.department_id)
        views.append(
            KbView(
                name=s.name,
                kb_id=s.kb_id,
                department_id=s.department_id,
                department_name=s.department_name,
                permission=perms.get(key),
                registered=s.registered,
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
        )
        for info in infos
    ]


@router.post("/kbs", response_model=OkResponse)
def create_kb(
    body: CreateKbRequest,
    user=Depends(require_dept_admin),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = build_context_for_user(user, auth=auth)
    ok, msg = pipeline.create_kb(body.name, ctx=ctx)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return OkResponse(ok=True, message=msg)


@router.delete("/kbs/{kb_name}", response_model=OkResponse)
def delete_kb(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Delete a knowledge base and all its documents / archives / indexes.

    Requires ``admin`` permission on the KB (implicit for the owning dept_admin).
    """
    ctx = build_context_for_user(user, kb_name, auth=auth)
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
    if not ctx.has_kb_permission(kb_name, "admin"):
        raise HTTPException(status_code=403, detail="admin permission required")
    msg = pipeline.delete_document(filename, kb_name, ctx=ctx)
    return OkResponse(ok=True, message=msg)
