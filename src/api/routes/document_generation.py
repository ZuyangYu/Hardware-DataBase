"""Document-generation API: governed authoring over the shared AppPipeline.

Mirrors the frontend/src/pages/DocumentGenerationPage.tsx (upload template / create task
/ runs & download). system_admin is rejected (governance role, no KB content).
Endpoints are thin: they build a RequestContext and delegate to AppPipeline.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

import src.settings
from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    AgentHumanDecisionRequest,
    AnalyzeTemplateFromAttachmentRequest,
    AnswerGenerationSessionRequest,
    ConfirmTemplateRequest,
    CompleteDocumentRevisionRequest,
    ConvertDocumentArtifactRequest,
    CreatePlanProposalRequest,
    CreateDocumentRevisionRequest,
    CreateGenerationSessionRequest,
    CreateWorkOrderRequest,
    DeleteDocumentWorkOrderRequest,
    DocumentReviewDecisionRequest,
    FeedbackRequest,
    IcdResolutionRequest,
    PlanProposalView,
    TemplateAnalysisReviewView,
    TemplateAnalysisView,
    TemplateMappingCorrectionRequest,
    TemplateReviewSuggestionView,
    TemplateReviewUnitView,
    TemplateSuggestionView,
    TemplateUnitView,
)
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.template_analysis import TemplateMappingCorrection

router = APIRouter(tags=["document-generation"])

_DOCUMENT_DOWNLOAD_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "markdown": "text/markdown; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _ctx(user: AuthUser, auth: AuthService, kb: str):
    if not kb.strip():
        raise HTTPException(status_code=400, detail="knowledge base is required")
    ctx = build_context_for_user(user, kb, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    ctx.metadata["document_template_kb_name"] = kb
    return ctx


def _write_ctx(user: AuthUser, auth: AuthService, kb: str):
    ctx = _ctx(user, auth, kb)
    if not ctx.has_kb_permission(kb, "write"):
        raise HTTPException(status_code=403, detail="write permission required")
    # This API is the controlled Workbench/management entry point.  Keep the
    # origin explicit so DocumentTask does not infer a Chat turn from a
    # synthetic RequestContext session id.
    ctx.metadata["document_task_origin"] = "workbench"
    return ctx


def _analysis_view(analysis) -> TemplateAnalysisView:
    decision = getattr(analysis, "activation_decision", None)
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
        reason_codes=list(getattr(decision, "reason_codes", []) or []) if decision is not None else [],
    )


def _analysis_review_view(analysis) -> TemplateAnalysisReviewView:
    """Project a correction review to safe metadata, never OOXML locations or values."""
    decision = getattr(analysis, "activation_decision", None)
    return TemplateAnalysisReviewView(
        analysis_id=analysis.analysis_id,
        template_version_id=analysis.template_version_id,
        content_hash=analysis.content_hash,
        format=analysis.format,
        status=analysis.status,
        units=[
            TemplateReviewUnitView(
                unit_id=unit.unit_id,
                label=unit.label,
                writable=unit.writable,
                blocked_reason=unit.blocked_reason,
                structural_role_hint=unit.structural_role_hint,
                candidate_for_auto_fill=unit.candidate_for_auto_fill,
            )
            for unit in analysis.units
        ],
        suggestions=[
            TemplateReviewSuggestionView(
                semantic_unit_id=suggestion.semantic_unit_id,
                label=suggestion.label,
                confidence=suggestion.confidence,
                target_unit_ids=list(suggestion.target_unit_ids),
                retrieval_terms=list(suggestion.retrieval_terms),
                value_shape=suggestion.value_shape,
                overwrite_basis=suggestion.overwrite_basis,
            )
            for suggestion in analysis.suggestions
        ],
        locked_unit_ids=list(analysis.locked_unit_ids),
        reason_codes=list(decision.reason_codes) if decision is not None else [],
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
    ctx = _write_ctx(user, auth, kb)
    content = file.file.read()
    try:
        analysis = pipeline.analyze_document_template(
            ctx, filename=file.filename or "template", content=content, template_name=template_name,
        )
        auto_activated = False
        decision = getattr(analysis, "activation_decision", None)
        if (
            src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES
            and analysis.status == "ready_for_confirmation"
            and decision is not None
            and decision.status == "auto_accepted"
        ):
            pipeline.confirm_document_template(
                ctx,
                analysis_id=analysis.analysis_id,
                display_name=template_name,
            )
            auto_activated = True
    except PermissionError as exc:
        # 写操作权限失败应为 403，而非 400（区分"无权"与"请求非法"）。
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _analysis_view(analysis).model_copy(update={"auto_activated": auto_activated})


@router.post(
    "/document-generation/templates/analyze-from-attachment",
    response_model=TemplateAnalysisView,
)
def analyze_template_from_attachment(
    body: AnalyzeTemplateFromAttachmentRequest | None = Body(default=None),
    kb: str | None = None,
    session_id: int | None = None,
    attachment_id: str | None = None,
    template_name: str | None = None,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Create a template from a session attachment without a browser re-upload.

    Permissions are intentionally stricter than plain attachments (design
    §13.4): the target KB write permission is re-checked here, independent of
    the session-scope ACL that allowed the upload. The legacy multipart
    analyze endpoint keeps working unchanged.
    """
    if body is not None:
        kb = body.kb
        session_id = body.session_id
        attachment_id = body.attachment_id
        template_name = body.template_name
    if not kb or session_id is None or not attachment_id or not template_name:
        raise HTTPException(status_code=422, detail="kb, session_id, attachment_id and template_name are required")
    if not src.settings.CHAT_ATTACHMENTS_ENABLED:
        raise HTTPException(status_code=404, detail="chat attachments are not enabled")
    _write_ctx(user, auth, kb)
    from src.attachments.models import AttachmentError, AttachmentNotFound
    from src.attachments.service import AttachmentService

    service = AttachmentService()
    try:
        record, content = service.read_source_bytes(
            attachment_id=attachment_id, session_id=session_id, user_id=user.id
        )
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail="attachment not found") from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = record.filename
    try:
        analysis = pipeline.analyze_document_template(
            _write_ctx(user, auth, kb),
            filename=filename,
            content=content,
            template_name=template_name,
            origin_source_type="chat_attachment",
            origin_attachment_id=record.attachment_id,
            origin_session_id=record.session_id,
            origin_content_hash=record.sha256,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Attachment conversion must have exactly the same activation semantics as
    # the regular template-upload endpoint.  Returning a draft context here
    # makes the following chat generation request fail at schema validation;
    # safe, auto-accepted mappings are therefore activated immediately while
    # ambiguous mappings remain a human-review gate.
    auto_activated = False
    decision = getattr(analysis, "activation_decision", None)
    if (
        src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES
        and analysis.status == "ready_for_confirmation"
        and decision is not None
        and decision.status == "auto_accepted"
    ):
        try:
            pipeline.confirm_document_template(
                _write_ctx(user, auth, kb),
                analysis_id=analysis.analysis_id,
                display_name=template_name,
            )
            auto_activated = True
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _analysis_view(analysis).model_copy(update={"auto_activated": auto_activated})


@router.get(
    "/document-generation/templates/{analysis_id}/review",
    response_model=TemplateAnalysisReviewView,
)
def template_analysis_review(
    analysis_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        analysis = pipeline.get_document_template_analysis_for_review(ctx, analysis_id=analysis_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _analysis_review_view(analysis)


@router.post(
    "/document-generation/templates/{analysis_id}/corrections",
    response_model=TemplateAnalysisView,
)
def correct_template_analysis(
    analysis_id: str,
    kb: str,
    payload: TemplateMappingCorrectionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        source_analysis = pipeline.get_document_template_analysis_for_review(
            ctx,
            analysis_id=analysis_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    selected_ids = set(payload.selected_suggestion_ids)
    if len(selected_ids) != len(payload.selected_suggestion_ids):
        raise HTTPException(status_code=400, detail="selected suggestion ids must be unique")
    suggestions_by_id = {
        suggestion.semantic_unit_id: suggestion
        for suggestion in source_analysis.suggestions
    }
    if not selected_ids <= suggestions_by_id.keys():
        raise HTTPException(status_code=400, detail="selected suggestion is not in the source analysis")
    suggestions = [
        suggestion
        for suggestion in source_analysis.suggestions
        if suggestion.semantic_unit_id in selected_ids
    ]
    units_by_id = {unit.unit_id: unit for unit in source_analysis.units}
    approved_overwrite_unit_ids = [
        unit_id
        for suggestion in suggestions
        if suggestion.overwrite_basis == "sample_value"
        for unit_id in suggestion.target_unit_ids
        if units_by_id[unit_id].structural_role_hint == "sample_value"
    ]
    correction = TemplateMappingCorrection(
        analysis_id=analysis_id,
        expected_content_hash=payload.expected_content_hash,
        suggestions=suggestions,
        locked_unit_ids=payload.locked_unit_ids,
        approved_overwrite_unit_ids=approved_overwrite_unit_ids,
        actor_id=ctx.user_id,
        comment=payload.comment,
    )
    try:
        corrected = pipeline.correct_document_template_analysis(ctx, correction=correction)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _analysis_view(corrected)


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
        kwargs = {"analysis_id": analysis_id, "display_name": payload.display_name}
        if payload.execution_mode is not None:
            kwargs["execution_mode"] = payload.execution_mode
        return pipeline.confirm_document_template(ctx, **kwargs)
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


@router.post("/document-generation/sessions")
def create_generation_session(
    kb: str,
    payload: CreateGenerationSessionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.create_document_generation_session(
            ctx,
            knowledge_base_name=kb,
            template_version_id=payload.template_version_id,
            contract_version=payload.contract_version,
            purpose=payload.purpose,
            output_policy=payload.output_policy,
            output_spec=payload.output_spec,
            document_schema_id=payload.document_schema_id,
            document_schema_version=payload.document_schema_version,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/document-generation/sessions/{session_id}")
def get_generation_session(
    session_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.get_document_generation_session(ctx, session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/document-generation/sessions/{session_id}/plan-proposals",
    response_model=PlanProposalView,
)
def create_plan_proposal(
    session_id: str,
    kb: str,
    payload: CreatePlanProposalRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Compile a frozen proposal without creating a WorkOrder or job."""

    ctx = _write_ctx(user, auth, kb)
    try:
        proposal = pipeline.create_document_plan_proposal(
            ctx,
            session_id,
            client_request_id=payload.client_request_id,
            expected_output_spec_version=payload.expected_output_spec_version,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return proposal


@router.get(
    "/document-generation/plans/{plan_id}/versions/{version}",
    response_model=PlanProposalView,
)
def get_plan_proposal(
    plan_id: str,
    version: int,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        proposal = pipeline.get_document_plan(ctx, plan_id, version)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail="document plan not found")
    return proposal


@router.post("/document-generation/sessions/{session_id}/messages")
def answer_generation_session(
    session_id: str,
    kb: str,
    payload: AnswerGenerationSessionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.answer_document_generation_session(
            ctx,
            session_id,
            question_id=payload.question_id,
            answer=payload.answer,
            client_request_id=payload.client_request_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/document-generation/sessions/{session_id}/confirm")
def confirm_generation_session(
    session_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.confirm_document_generation_session(ctx, session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/document-generation/work-orders")
def create_work_order(
    kb: str,
    payload: CreateWorkOrderRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        kwargs = {
            "template_version_id": payload.template_version_id,
            "document_schema_id": payload.document_schema_id,
            "document_schema_version": payload.document_schema_version,
            "generation_session_id": payload.generation_session_id,
        }
        if payload.client_request_id is not None:
            kwargs["idempotency_key"] = payload.client_request_id
        if payload.execution_mode is not None:
            kwargs["execution_mode"] = payload.execution_mode
        return pipeline.prepare_knowledge_base_document_generation(
            ctx,
            knowledge_base_name=kb,
            **kwargs,
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


@router.get("/document-generation/tasks/{task_id}/projection")
def document_task_projection(
    task_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Read the task aggregate shared by Chat and the Workbench."""
    ctx = _ctx(user, auth, kb)
    try:
        projection = pipeline.get_document_task_projection(ctx, task_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if projection is None:
        raise HTTPException(status_code=404, detail="document task not found")
    return projection


@router.get("/document-generation/tasks")
def list_document_task_projections(
    kb: str,
    conversation_id: str | None = None,
    session_id: int | None = None,
    limit: int = 100,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """List the same task projections used to restore a Chat task card."""
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.list_document_task_projections(
            ctx,
            knowledge_base_name=kb,
            conversation_id=(conversation_id if conversation_id is not None else session_id),
            limit=max(1, min(limit, 200)),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/document-generation/tasks/{task_id}/reviews")
def list_document_task_reviews(
    task_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.list_document_reviews(ctx, task_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/document-generation/reviews/{review_id}")
def get_document_review(
    review_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        review = pipeline.get_document_review(ctx, review_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="document review not found")
    return review


@router.post("/document-generation/reviews/{review_id}/decision")
def submit_document_review_decision(
    review_id: str,
    kb: str,
    payload: DocumentReviewDecisionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.submit_document_review_decision(
            ctx,
            review_id,
            subject_hash=payload.subject_hash,
            decision=payload.decision,
            client_request_id=payload.client_request_id,
            status=payload.status,
            decision_metadata=payload.decision_metadata,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/document-generation/tasks/{task_id}/revisions")
def list_document_task_revisions(
    task_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        return pipeline.list_document_revisions(ctx, task_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/document-generation/revisions/{revision_id}")
def get_document_revision(
    revision_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _ctx(user, auth, kb)
    try:
        revision = pipeline.get_document_revision(ctx, revision_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if revision is None:
        raise HTTPException(status_code=404, detail="artifact revision not found")
    return revision


@router.post("/document-generation/tasks/{task_id}/revisions")
def create_document_revision(
    task_id: str,
    kb: str,
    payload: CreateDocumentRevisionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.create_document_revision(
            ctx,
            task_id,
            parent_artifact_id=payload.parent_artifact_id,
            request_type=payload.request_type,
            request=payload.request,
            changed_fields=payload.changed_fields,
            changed_sections=payload.changed_sections,
            client_request_id=payload.client_request_id,
            metadata=payload.metadata,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/document-generation/revisions/{revision_id}/complete")
def complete_document_revision(
    revision_id: str,
    kb: str,
    payload: CompleteDocumentRevisionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Commit a controlled worker's child artifact and revalidation result."""
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.complete_document_revision(
            ctx,
            revision_id,
            child_artifact_id=payload.child_artifact_id,
            revalidation_status=payload.revalidation_status,
            revalidation_result=payload.revalidation_result,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/document-generation/tasks/{task_id}/resume")
def resume_document_task(
    task_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Resume a document generation task through its task identity."""
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.resume_document_task(ctx, task_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _safe_chat_task_status(status: dict) -> dict:
    """Project only status/card fields needed by the chat task center."""
    return {
        "task_id": status.get("task_id"),
        "work_order_id": status.get("work_order_id"),
        "status": status.get("status"),
        "phase": status.get("phase"),
        "scope_type": status.get("scope_type"),
        "knowledge_base_name": status.get("knowledge_base_name"),
        "target_format": status.get("target_format"),
        "next_actions": list(status.get("next_actions") or []),
        "unit_statuses": dict(status.get("unit_statuses") or {}),
        "error_code": status.get("error_code"),
        "error_message": status.get("error_message"),
        "retryable": status.get("retryable"),
        "artifacts": [
            {
                "artifact_id": str(item.get("artifact_id")),
                "stage": str(item.get("stage")),
                "output_format": str(item.get("output_format") or ""),
                # The generic Artifact namespace is a compatibility adapter
                # over the legacy document store.  Keep the old fields above
                # so existing clients remain unchanged.
                "preview_url": f"/api/v1/artifacts/document/{item.get('artifact_id')}/preview",
                "download_url": f"/api/v1/artifacts/document/{item.get('artifact_id')}/download",
            }
            for item in (status.get("artifacts") or [])[:8]
            if isinstance(item, dict) and str(item.get("artifact_id") or "").strip()
        ],
    }


def _safe_chat_task_projection(projection: dict) -> dict:
    """Adapt the aggregate projection to the legacy chat-task envelope."""
    work_order = projection.get("work_order") or {}
    run = projection.get("run") or {}
    return _safe_chat_task_status({
        "task_id": projection.get("task_id"),
        "work_order_id": projection.get("work_order_id"),
        "status": projection.get("status"),
        "phase": work_order.get("phase") or projection.get("status"),
        "scope_type": "knowledge_base" if projection.get("knowledge_base_name") else "project",
        "knowledge_base_name": projection.get("knowledge_base_name"),
        "target_format": work_order.get("target_format"),
        "next_actions": projection.get("next_actions") or [],
        "error_code": projection.get("error_code"),
        "error_message": projection.get("error_message"),
        "retryable": projection.get("retryable"),
        "artifacts": projection.get("artifacts") or [],
        "harness_run": run,
        "clarification_session_id": projection.get("generation_session_id"),
    })


@router.get("/document-generation/chat-tasks")
def list_chat_document_tasks(
    session_id: int | None = None,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Return durable document tasks that can be projected into chat cards.

    ``session_id`` is optional so a future global task center can reconcile all
    sessions.  Every work order is re-authorized with its persisted KB before
    its status or artifact IDs are returned.
    """
    task_store = getattr(
        getattr(getattr(pipeline, "document_generation", None), "task_service", None),
        "store",
        None,
    )
    tasks: list[dict] = []
    seen_task_ids: set[str] = set()
    seen_work_orders: set[str] = set()
    if (
        src.settings.DOCUMENT_TASK_READ_ENABLED
        and callable(getattr(task_store, "list_for_owner", None))
        and callable(getattr(pipeline, "get_document_task_projection", None))
    ):
        for task in task_store.list_for_owner(
            tenant_id="default",
            user_id=user.username,
            limit=200,
        ):
            if getattr(task, "origin", None) != "chat":
                continue
            persisted_conversation_id = str(getattr(task, "conversation_id", None) or "").strip()
            if not persisted_conversation_id.isdecimal():
                continue
            if session_id is not None and int(persisted_conversation_id) != session_id:
                continue
            task_id = str(getattr(task, "task_id", None) or "").strip()
            kb_name = str(getattr(task, "knowledge_base_name", None) or "").strip()
            if not task_id or not kb_name:
                continue
            try:
                ctx = _ctx(user, auth, kb_name)
                projection = pipeline.get_document_task_projection(ctx, task_id)
            except (HTTPException, PermissionError, KeyError, ValueError):
                continue
            if not isinstance(projection, dict):
                continue
            work_order_id = str(projection.get("work_order_id") or "").strip()
            seen_task_ids.add(task_id)
            if work_order_id:
                seen_work_orders.add(work_order_id)
            tasks.append({
                "session_id": int(persisted_conversation_id),
                "task_id": task_id,
                "work_order_id": work_order_id,
                "kb_name": kb_name,
                "job_status": str(projection.get("status") or ""),
                "created_at": getattr(task, "created_at", None),
                "updated_at": getattr(task, "updated_at", None),
                "status": _safe_chat_task_projection(projection),
            })

    job_store = getattr(pipeline, "document_job_store", None)
    if not callable(getattr(job_store, "list_chat_session_jobs", None)):
        job_store = DocumentAuthoringJobStore()
    jobs = job_store.list_chat_session_jobs(
        tenant_id="default",
        # DocumentContext and the worker persist the authenticated username as
        # the durable job owner; keep the projection on that same identity.
        user_id=user.username,
        session_id=session_id,
    )
    for job in jobs:
        persisted_session_id = str(job.session_id or "").strip()
        if not persisted_session_id.isdecimal():
            # Workbench-only jobs use a synthetic session key and should not
            # appear as chat replies.
            continue
        if session_id is not None and int(persisted_session_id) != session_id:
            continue
        work_order_id = str(job.work_order_id or job.payload.get("work_order_id") or "").strip()
        kb_name = str(job.payload.get("knowledge_base_name") or "").strip()
        if not work_order_id or not kb_name:
            continue
        # Generation and its derived PDF/PPTX conversion share one work order.
        # Jobs are returned newest-first, so expose one reconciled card instead
        # of duplicating the same task in the chat stream.
        if work_order_id in seen_work_orders or str(job.task_id or "").strip() in seen_task_ids:
            continue
        seen_work_orders.add(work_order_id)
        try:
            ctx = _ctx(user, auth, kb_name)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                # Permission revocation should hide the task, not leak its
                # work-order ID or artifact existence.
                continue
            raise
        try:
            status = pipeline.get_document_run_status(work_order_id, ctx)
        except (PermissionError, KeyError, ValueError):
            # The work order may have been deleted or become inaccessible
            # between the job query and this projection; omit it without
            # disclosing whether an artifact ever existed.
            continue
        if not isinstance(status, dict):
            continue
        task_id = (
            job.task_id or status.get("task_id")
            if src.settings.DOCUMENT_TASK_READ_ENABLED
            else None
        )
        tasks.append({
            "session_id": int(persisted_session_id),
            "task_id": task_id,
            "work_order_id": work_order_id,
            "kb_name": kb_name,
            "job_status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "status": _safe_chat_task_status(status),
        })
    return tasks


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
    ctx = _write_ctx(user, auth, kb)
    try:
        run_id = pipeline.submit_knowledge_base_document_generation(ctx, work_order_id)
    except PermissionError as exc:
        # 写操作权限失败应为 403，而非 400（区分"无权"与"请求非法"）。
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"work_order_id": work_order_id, "run_id": run_id}


@router.post("/document-generation/work-orders/{work_order_id}/resume")
def resume_work_order(
    work_order_id: str,
    kb: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        run_id = pipeline.resume_knowledge_base_document_generation(ctx, work_order_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"work_order_id": work_order_id, "run_id": run_id}


@router.delete("/document-generation/work-orders/{work_order_id}")
def delete_work_order(
    work_order_id: str,
    kb: str,
    payload: DeleteDocumentWorkOrderRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.delete_knowledge_base_document_work_order(
            ctx,
            work_order_id,
            reason=payload.reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    ctx = _write_ctx(user, auth, kb)
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
    ctx = _write_ctx(user, auth, kb)
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
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.cancel_harness_run(ctx, run_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/document-generation/harness-runs/{run_id}/agent-decision")
def agent_human_decision(
    run_id: str,
    kb: str,
    payload: AgentHumanDecisionRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Approve/reject a pending low-confidence field proposal."""
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.resolve_knowledge_base_harness_human_decision(
            ctx,
            run_id,
            pending_event_id=payload.pending_event_id,
            proposal_hash=payload.proposal_hash,
            decision=payload.decision,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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


@router.post("/document-generation/artifacts/{artifact_id}/convert")
def artifact_convert(
    artifact_id: str,
    kb: str,
    payload: ConvertDocumentArtifactRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Queue PDF/PPTX conversion of an already generated template artifact."""
    ctx = _write_ctx(user, auth, kb)
    try:
        job = pipeline.submit_document_artifact_conversion(
            ctx,
            artifact_id,
            target_format=payload.target_format,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": job.job_id,
        "operation": job.operation,
        "status": job.status,
        "source_artifact_id": artifact_id,
        "target_format": payload.target_format,
    }


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
    target_format = "bin"
    document_generation = getattr(pipeline, "document_generation", None)
    store = getattr(document_generation, "store", None)
    get_artifact = getattr(store, "get_artifact", None)
    get_work_order = getattr(store, "get_work_order", None)
    if callable(get_artifact):
        artifact = get_artifact(artifact_id)
        if artifact is not None and callable(get_work_order):
            order = get_work_order(getattr(artifact, "work_order_id", ""))
            target_format = str(
                getattr(artifact, "output_format", None)
                or getattr(order, "target_format", "")
                or "bin"
            ).strip().lower()
    target_format = re.sub(r"[^a-z0-9]+", "", target_format) or "bin"
    safe_artifact_id = re.sub(r"[^A-Za-z0-9._-]+", "-", artifact_id).strip(".-") or "artifact"
    filename = f"document-{safe_artifact_id}.{target_format}"
    return Response(
        content=content,
        media_type=_DOCUMENT_DOWNLOAD_MEDIA_TYPES.get(target_format, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/document-generation/artifacts/{artifact_id}/feedback")
def artifact_feedback(
    artifact_id: str,
    kb: str,
    payload: FeedbackRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _write_ctx(user, auth, kb)
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
    ctx = _write_ctx(user, auth, kb)
    try:
        return pipeline.approve_document_artifact(ctx, artifact_id, comment=payload.comment)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
