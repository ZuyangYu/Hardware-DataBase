"""Document asset center endpoints: version chain, lifecycle and wiki links."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    AddDocAssetVersionRequest,
    CreateDocAssetLinkRequest,
    DocAssetDetailView,
    DocAssetLinkView,
    DocAssetView,
    OkResponse,
    UpdateDocAssetRequest,
)
from src.core.auth import AuthService, AuthUser
from src.core.doc_assets import DocumentAssetError, DocumentAssetService
from src.core.app_pipeline import AppPipeline
from src.pipelines.document_rag.schemas import TASK_STATUS_COMPLETED, normalize_parse_status

router = APIRouter(tags=["doc-assets"])


def _scope(user: AuthUser, kb_name: str, permission: str, auth: AuthService):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, permission):
        raise HTTPException(status_code=403, detail=f"{permission} permission required")
    kb_id = ctx.metadata.get("kb_id")
    department_id = ctx.metadata.get("resource_department_id")
    if kb_id is None or department_id is None:
        raise HTTPException(status_code=404, detail="knowledge base scope not found")
    return ctx, int(kb_id), int(department_id)


def _service() -> DocumentAssetService:
    return DocumentAssetService()


def _resolve_file(pipeline: AppPipeline, ctx, kb_name: str, file_id: str):
    info = next(
        (item for item in pipeline.list_file_infos(kb_name, ctx=ctx) if item.id == file_id),
        None,
    )
    if info is None:
        raise HTTPException(status_code=404, detail="file not found")
    status = normalize_parse_status(getattr(info, "status", ""), getattr(info, "processor_kind", ""))
    content_hash = str((getattr(info, "metadata", {}) or {}).get("content_hash") or "")
    return info, status, content_hash


@router.get("/kbs/{kb_name}/doc-assets", response_model=list[DocAssetView])
def list_doc_assets(
    kb_name: str,
    query: str = "",
    status: str = Query(default="", pattern="^(draft|effective|in_revision|obsolete)?$"),
    category: str = "",
    project: str = "",
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    return [
        DocAssetView(**row)
        for row in _service().list_assets(
            kb_id=kb_id, department_id=department_id, query=query, status=status,
            category=category, project=project,
        )
    ]


@router.get("/kbs/{kb_name}/doc-assets/{asset_id}", response_model=DocAssetDetailView)
def get_doc_asset(
    kb_name: str, asset_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    asset = _service().get_asset(asset_id=asset_id, kb_id=kb_id, department_id=department_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="doc asset not found")
    return DocAssetDetailView(**asset)


@router.patch("/kbs/{kb_name}/doc-assets/{asset_id}", response_model=DocAssetView)
def update_doc_asset(
    kb_name: str, asset_id: int, body: UpdateDocAssetRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    fields = body.model_dump(exclude_none=True, exclude_unset=True)
    try:
        asset = _service().update_info(
            asset_id=asset_id, kb_id=kb_id, department_id=department_id,
            actor_user_id=user.id, fields=fields,
        )
    except Exception as exc:  # noqa: BLE001
        _reraise(exc)
    return DocAssetView(**asset)


@router.post("/kbs/{kb_name}/doc-assets/{asset_id}/versions", response_model=DocAssetView)
def add_doc_asset_version(
    kb_name: str, asset_id: int, body: AddDocAssetVersionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    info, status, content_hash = _resolve_file(pipeline, ctx, kb_name, body.file_id)
    try:
        asset = _service().add_version(
            asset_id=asset_id, kb_id=kb_id, department_id=department_id, actor_user_id=user.id,
            file_id=info.id, file_name=info.name, content_hash=content_hash,
            parse_status=status, note=body.note,
        )
    except Exception as exc:  # noqa: BLE001
        _reraise(exc)
    _service().absorb_shadow_for_file(
        kb_id=kb_id, department_id=department_id, file_id=info.id, exclude_asset_id=asset_id
    )
    return DocAssetView(**asset)


@router.post("/kbs/{kb_name}/doc-assets/backfill", response_model=OkResponse)
def backfill_doc_assets(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """One shadow asset per parsed file not yet registered (idempotent)."""
    ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    files = []
    for item in pipeline.list_file_infos(kb_name, ctx=ctx):
        if normalize_parse_status(getattr(item, "status", ""), getattr(item, "processor_kind", "")) != TASK_STATUS_COMPLETED:
            continue
        files.append({
            "file_id": str(item.id),
            "file_name": str(item.name),
            "processor_kind": str(getattr(item, "processor_kind", "") or ""),
            "content_hash": str((getattr(item, "metadata", {}) or {}).get("content_hash") or ""),
            "parse_status": normalize_parse_status(getattr(item, "status", ""), getattr(item, "processor_kind", "")),
        })
    result = DocumentAssetService().backfill_shadow_assets(
        kb_id=kb_id, kb_name=kb_name, department_id=department_id,
        actor_user_id=user.id, files=files,
    )
    return OkResponse(ok=True, message=f"补录 {result['created']} 个影子资产, 跳过 {result['skipped']} 个(已登记)")


@router.post("/kbs/{kb_name}/doc-assets/{asset_id}/links", response_model=DocAssetLinkView)
def create_doc_asset_link(
    kb_name: str, asset_id: int, body: CreateDocAssetLinkRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    try:
        link = _service().add_link(
            department_id=department_id, from_asset_id=asset_id, to_asset_id=body.to_asset_id,
            rel_type=body.rel_type, note=body.note, source="manual",
            status="confirmed", actor_user_id=user.id,
        )
    except Exception as exc:  # noqa: BLE001
        _reraise(exc)
    return DocAssetLinkView(**link)


@router.delete("/kbs/{kb_name}/doc-assets/{asset_id}/links/{link_id}", response_model=OkResponse)
def delete_doc_asset_link(
    kb_name: str, asset_id: int, link_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    service = _service()
    link = service.get_asset(
        asset_id=asset_id, kb_id=kb_id, department_id=department_id
    )
    if link is None:
        raise HTTPException(status_code=404, detail="doc asset not found")
    outgoing = {item["id"] for item in link.get("links_out", [])}
    if link_id not in outgoing:
        raise HTTPException(status_code=404, detail="link not found on this asset")
    if not service.remove_link(department_id=department_id, link_id=link_id):
        raise HTTPException(status_code=404, detail="link not found")
    return OkResponse(ok=True, message="关联已移除")


def _reraise(exc: Exception) -> None:
    """Map service-level errors onto HTTP semantics, re-raising as needed."""
    if isinstance(exc, DocumentAssetError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise exc
