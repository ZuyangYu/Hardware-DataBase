"""Document-generation API: governed authoring over the shared AppPipeline.

Mirrors the Streamlit document_generation_page (upload template / create task
/ runs & download). system_admin is rejected (governance role, no KB content).
Endpoints are thin: they build a RequestContext and delegate to AppPipeline.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    ConfirmTemplateRequest,
    CreateWorkOrderRequest,
    FeedbackRequest,
    IcdResolutionRequest,
    TemplateAnalysisView,
    TemplateSuggestionView,
    TemplateUnitView,
)
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

router = APIRouter(tags=["document-generation"])


def _ctx(user: AuthUser, auth: AuthService, kb: str):
    ctx = build_context_for_user(user, kb, auth=auth)
    reject_system_admin_kb_access(ctx)
    if kb and not ctx.has_kb_permission(kb, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    return ctx


def _analysis_view(analysis) -> TemplateAnalysisView:
    return TemplateAnalysisView(
        analysis_id=analysis.analysis_id,
        template_version_id=analysis.template_version_id,
        format=analysis.format,
        status=analysis.status,
        units=[
            TemplateUnitView(
                unit_id=u.unit_id, label=getattr(u, "label", ""),
                writable=u.writable, blocked_reason=getattr(u, "blocked_reason", None),
            )
            for u in analysis.units
        ],
        suggestions=[
            TemplateSuggestionView(
                semantic_unit_id=s.semantic_unit_id, label=s.label, confidence=s.confidence,
            )
            for s in analysis.suggestions
        ],
    )


@router.post("/document-generation/templates/analyze", response_model=TemplateAnalysisView)
def analyze_template(
    kb: str,
    file: UploadFile = File(...),
    template_name: str = Form(...),
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    content = file.file.read()
    try:
        analysis = pipeline.analyze_document_template(
            ctx, filename=file.filename or "template", content=content, template_name=template_name,
        )
    except PermissionError as exc:
        # 写操作权限失败应为 403，而非 400（区分"无权"与"请求非法"）。
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _analysis_view(analysis)


@router.get("/document-generation/templates/{template_version_id}/sanitization")
def template_sanitization(
    template_version_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    return pipeline.get_document_template_sanitization_summary(ctx, template_version_id)


@router.post("/document-generation/templates/{analysis_id}/confirm")
def confirm_template(
    analysis_id: str,
    kb: str,
    payload: ConfirmTemplateRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.confirm_document_template(
            ctx, analysis_id=analysis_id, display_name=payload.display_name,
        )
    except PermissionError as exc:
        # 写操作权限失败应为 403，而非 400（区分"无权"与"请求非法"）。
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/document-generation/options")
def options(
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    return pipeline.list_knowledge_base_document_generation_options(ctx)


@router.post("/document-generation/work-orders")
def create_work_order(
    kb: str,
    payload: CreateWorkOrderRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.prepare_knowledge_base_document_generation(
            ctx,
            knowledge_base_name=kb,
            template_version_id=payload.template_version_id,
            document_schema_id=payload.document_schema_id,
            document_schema_version=payload.document_schema_version,
        )
    except PermissionError as exc:
        # 写操作权限失败应为 403，而非 400（区分"无权"与"请求非法"）。
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/document-generation/work-orders")
def list_work_orders(
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    orders = pipeline.list_knowledge_base_document_work_orders(ctx, kb)
    return [order.model_dump() if hasattr(order, "model_dump") else dict(vars(order)) for order in orders]


@router.get("/document-generation/work-orders/{work_order_id}/status")
def work_order_status(
    work_order_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    status = pipeline.get_document_run_status(work_order_id, ctx)
    if status is None:
        raise HTTPException(status_code=404, detail="work order not found")
    return status


@router.post("/document-generation/work-orders/{work_order_id}/generate")
def generate_work_order(
    work_order_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        run_id = pipeline.submit_knowledge_base_document_generation(ctx, work_order_id)
    except PermissionError as exc:
        # 写操作权限失败应为 403，而非 400（区分"无权"与"请求非法"）。
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"work_order_id": work_order_id, "run_id": run_id}


@router.get("/document-generation/work-orders/{work_order_id}/icd-scope-review")
def icd_scope_review(
    work_order_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    return pipeline.get_icd_scope_review(ctx, work_order_id)


@router.post("/document-generation/work-orders/{work_order_id}/icd-scope-resolution")
def icd_scope_resolution(
    work_order_id: str,
    kb: str,
    payload: IcdResolutionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        pipeline.submit_icd_scope_resolution(
            ctx,
            work_order_id,
            resolutions=[item.model_dump() for item in payload.resolutions],
            comment=payload.comment,
        )
        run_id = pipeline.submit_knowledge_base_document_generation(ctx, work_order_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"work_order_id": work_order_id, "run_id": run_id}


@router.post("/document-generation/harness-runs/{run_id}/pause")
def pause_harness(
    run_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.pause_harness_run(ctx, run_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/document-generation/harness-runs/{run_id}/cancel")
def cancel_harness(
    run_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.cancel_harness_run(ctx, run_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/document-generation/artifacts/{artifact_id}/preview")
def artifact_preview(
    artifact_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.preview_document_artifact(ctx, artifact_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/document-generation/artifacts/{artifact_id}/download")
def artifact_download(
    artifact_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        content = pipeline.download_document_artifact(ctx, artifact_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=content, media_type="application/octet-stream")


@router.post("/document-generation/artifacts/{artifact_id}/feedback")
def artifact_feedback(
    artifact_id: str,
    kb: str,
    payload: FeedbackRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.submit_document_feedback(ctx, artifact_id, comment=payload.comment)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/document-generation/artifacts/{artifact_id}/approve")
def artifact_approve(
    artifact_id: str,
    kb: str,
    payload: FeedbackRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.approve_document_artifact(ctx, artifact_id, comment=payload.comment)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc