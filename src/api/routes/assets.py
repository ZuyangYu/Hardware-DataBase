"""Reviewable AI extraction and hardware asset master-data endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import AssetCandidateView, AssetDetailView, AssetSourceLinkView, AssetView, ConfirmAssetCandidateRequest, GenerateAssetCandidateRequest, OkResponse
from src.core.app_pipeline import AppPipeline
from src.core.assets import AssetService, AssetSource, classify_asset_source
from src.core.auth import AuthService, AuthUser
from src.pipelines.document_rag.schemas import TASK_STATUS_COMPLETED, normalize_parse_status

router = APIRouter(tags=["assets"])


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


@router.get("/kbs/{kb_name}/assets", response_model=list[AssetView])
def list_assets(kb_name: str, query: str = "", user: AuthUser = Depends(current_user), auth: AuthService = Depends(get_auth_service)):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    return [AssetView(**row) for row in AssetService().list_assets(kb_id=kb_id, department_id=department_id, query=query)]


@router.get("/kbs/{kb_name}/assets/{asset_id}", response_model=AssetDetailView)
def get_asset(asset_id: int, kb_name: str, user: AuthUser = Depends(current_user), auth: AuthService = Depends(get_auth_service)):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    asset = AssetService().get_asset(asset_id=asset_id, kb_id=kb_id, department_id=department_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return AssetDetailView(**asset)


@router.get("/kbs/{kb_name}/asset-candidates", response_model=list[AssetCandidateView])
def list_candidates(
    kb_name: str,
    status: str = Query(default="pending", pattern="^(pending|accepted|rejected)?$"),
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    return [AssetCandidateView(**row) for row in AssetService().list_candidates(kb_id=kb_id, department_id=department_id, status=status)]


@router.get("/kbs/{kb_name}/asset-sources", response_model=list[AssetSourceLinkView])
def list_asset_sources(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    files = []
    for item in pipeline.list_file_infos(kb_name, ctx=ctx):
        profile = classify_asset_source(item.name, item.processor_kind, item.dataset_kind)
        files.append(
            {
                "id": item.id,
                "name": item.name,
                "status": normalize_parse_status(item.status, item.processor_kind),
                "processor_kind": item.processor_kind,
                "dataset_kind": item.dataset_kind,
                "source_category": profile.category,
                "extraction_target": profile.extraction_target,
                "asset_eligible": profile.asset_eligible,
            }
        )
    return [
        AssetSourceLinkView(**row)
        for row in AssetService().list_file_links(kb_id=kb_id, department_id=department_id, files=files)
    ]


@router.post("/kbs/{kb_name}/asset-candidates/generate", response_model=AssetCandidateView)
def generate_candidate(
    kb_name: str,
    body: GenerateAssetCandidateRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    info = next((item for item in pipeline.list_file_infos(kb_name, ctx=ctx) if item.id == body.file_id), None)
    if info is None:
        raise HTTPException(status_code=404, detail="file not found")
    if normalize_parse_status(getattr(info, "status", ""), getattr(info, "processor_kind", "")) != TASK_STATUS_COMPLETED:
        raise HTTPException(status_code=409, detail="file is not parsed yet")
    profile = classify_asset_source(info.name, getattr(info, "processor_kind", ""), getattr(info, "dataset_kind", ""))
    if not profile.asset_eligible:
        raise HTTPException(status_code=409, detail="该资料属于硬件需求/约束来源，不应直接生成资产候选")
    result = pipeline.get_parse_result(kb_name, body.file_id, ctx=ctx)
    excerpt = "\n\n".join(chunk.content for chunk in (getattr(result, "chunks", []) or [])[:8])[:12000]
    candidate, _used_llm = AssetService().generate_candidate(
        kb_id=kb_id,
        department_id=department_id,
        kb_name=kb_name,
        source=AssetSource(
            file_id=info.id,
            file_name=info.name,
            processor_kind=getattr(info, "processor_kind", ""),
            dataset_kind=getattr(info, "dataset_kind", ""),
            metadata=getattr(info, "metadata", None),
            excerpt=excerpt,
            locator="parsed chunks 1-8" if excerpt else "file metadata",
            source_category=profile.category,
            extraction_target=profile.extraction_target,
        ),
    )
    return AssetCandidateView(**candidate)


@router.post("/kbs/{kb_name}/asset-candidates/{candidate_id}/accept", response_model=AssetView)
def accept_candidate(
    kb_name: str,
    candidate_id: int,
    body: ConfirmAssetCandidateRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    try:
        asset = AssetService().accept_candidate(
            candidate_id=candidate_id, kb_id=kb_id, department_id=department_id,
            actor_user_id=user.id, overrides=body.model_dump(exclude_none=True),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AssetView(**asset)


@router.post("/kbs/{kb_name}/asset-candidates/{candidate_id}/reject", response_model=OkResponse)
def reject_candidate(candidate_id: int, kb_name: str, user: AuthUser = Depends(current_user), auth: AuthService = Depends(get_auth_service)):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    try:
        AssetService().reject_candidate(candidate_id=candidate_id, kb_id=kb_id, department_id=department_id, actor_user_id=user.id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return OkResponse(ok=True, message="候选已忽略")
