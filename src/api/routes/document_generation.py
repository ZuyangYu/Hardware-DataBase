"""Document-generation API: governed authoring over the shared AppPipeline.

Mirrors the Streamlit document_generation_page (upload template / create task
/ runs & download). system_admin is rejected (governance role, no KB content).
Endpoints are thin: they build a RequestContext and delegate to AppPipeline.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import ConfirmTemplateRequest, TemplateAnalysisView, TemplateSuggestionView, TemplateUnitView
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
    except (PermissionError, ValueError, KeyError) as exc:
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