"""Document-generation API: governed authoring over the shared AppPipeline.

Mirrors the frontend/src/pages/DocumentGenerationPage.tsx (upload template / create task
/ runs & download). system_admin is rejected (governance role, no KB content).
Endpoints are thin: they build a RequestContext and delegate to AppPipeline.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

import src.settings
from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    AgentHumanDecisionRequest,
    AnswerGenerationSessionRequest,
    ConfirmTemplateRequest,
    CreateGenerationSessionRequest,
    CreateWorkOrderRequest,
    DeleteDocumentWorkOrderRequest,
    FeedbackRequest,
    IcdResolutionRequest,
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
        reason_codes=list(decision.reason_codes) if decision is not None else [],
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
            purpose=payload.purpose,
            output_policy=payload.output_policy,
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


def _safe_chat_task_status(status: dict) -> dict:
    """Project only status/card fields needed by the chat task center."""
    return {
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
    tasks: list[dict] = []
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
        tasks.append({
            "session_id": int(persisted_session_id),
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
            target_format = str(getattr(order, "target_format", "") or "bin").strip().lower()
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
