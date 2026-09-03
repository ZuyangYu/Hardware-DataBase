"""Generic export API for completed chat/agent results."""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    CreateExportRequest,
    ExportArtifactView,
    ExportBatchView,
    ExportJobView,
    ExportSourceRef,
    LegacyArtifactView,
)
from src.core.auth import AuthService, AuthUser
from src.core.app_logs import AppLogService
from src.core.conversation import GENERAL_CHAT_KB_NAME, ChatTurn, ConversationService
from src.document_authoring.artifact_preview import preview_artifact
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.observability import metrics as observability_metrics
from src.result_exports.content import envelope_from_turn
from src.result_exports.models import Artifact, ExportJob, enabled_export_formats, is_export_format_enabled
from src.result_exports.store import ResultExportStore

router = APIRouter(tags=["exports"])
_RENDER_OPTION_KEYS = frozenset({"theme", "include_charts"})
_PRESENTATION_THEMES = frozenset({"light", "dark", "blue"})
_LEGACY_MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "markdown": "text/markdown; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}


def get_export_store() -> ResultExportStore:
    return ResultExportStore()


def get_conversation_service() -> ConversationService:
    return ConversationService()


def _record_export_audit(
    user: AuthUser,
    *,
    action: str,
    target_type: str,
    target_id: str,
    success: bool = True,
    error_message: str = "",
    metadata: dict | None = None,
    kb_name: str = "",
) -> None:
    """Audit export operations without making the result path depend on logs."""
    try:
        AppLogService().record_audit(
            action=action,
            actor=user,
            target_type=target_type,
            target_id=target_id,
            kb_name=kb_name,
            success=success,
            error_message=error_message[:500],
            metadata=metadata or {},
        )
    except Exception:
        # Audit storage must be fail-soft for downloads and worker hand-offs.
        pass


def _validate_render_options(options: dict) -> dict:
    unknown = sorted(set(options) - _RENDER_OPTION_KEYS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported export options: {', '.join(unknown)}")
    normalized = dict(options)
    if "theme" in normalized:
        theme = str(normalized["theme"] or "").strip().lower()
        if theme not in _PRESENTATION_THEMES:
            raise HTTPException(status_code=400, detail=f"unsupported presentation theme: {theme}")
        normalized["theme"] = theme
    if "include_charts" in normalized and not isinstance(normalized["include_charts"], bool):
        raise HTTPException(status_code=400, detail="include_charts must be a boolean")
    return normalized


def _authorize_kb_turn(user: AuthUser, turn: ChatTurn, auth: AuthService) -> None:
    if (
        turn.department_id not in (None, "")
        and (
            user.department_id in (None, "")
            or str(turn.department_id) != str(user.department_id)
        )
    ):
        raise HTTPException(status_code=403, detail="department scope no longer permits this result")
    if turn.kb_name == GENERAL_CHAT_KB_NAME:
        return
    ctx = build_context_for_user(user, turn.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(turn.kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")


def _authorize_snapshot(user: AuthUser, snapshot, auth: AuthService) -> None:
    if (
        snapshot.department_id not in (None, "")
        and (
            user.department_id in (None, "")
            or str(snapshot.department_id) != str(user.department_id)
        )
    ):
        raise HTTPException(status_code=403, detail="department scope no longer permits this result")
    kb_name = str(
        snapshot.knowledge_base_name
        or (snapshot.envelope.metadata or {}).get("knowledge_base")
        or ""
    ).strip()
    if not kb_name:
        return
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")


def _resolve_turn(
    user: AuthUser,
    source: ExportSourceRef,
    conv: ConversationService,
) -> ChatTurn:
    if source.kind == "turn":
        turn = conv.get_turn(user.id, source.id)
    else:
        try:
            message_id = int(source.id)
        except (TypeError, ValueError):
            turn = None
        else:
            turn = conv.get_turn_by_message(user.id, message_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="result source not found")
    if turn.status != "completed":
        raise HTTPException(status_code=409, detail="only completed turns can be exported")
    return turn


def _artifact_view(
    artifact: Artifact | None,
    *,
    snapshot_id: str = "",
    turn_id: str | None = None,
    available: bool = True,
) -> ExportArtifactView | None:
    if artifact is None:
        return None
    return ExportArtifactView(
        artifact_id=artifact.artifact_id,
        export_job_id=artifact.export_job_id,
        session_id=int(artifact.session_id) if artifact.session_id.isdecimal() else 0,
        format=artifact.format,
        filename=artifact.filename,
        mime_type=artifact.mime_type,
        size=artifact.size,
        sha256=artifact.sha256,
        preview=artifact.preview,
        created_at=artifact.created_at,
        expires_at=artifact.expires_at,
        preview_url=(
            f"/api/v1/artifacts/{artifact.artifact_id}/preview"
            if available else ""
        ),
        download_url=(
            f"/api/v1/artifacts/{artifact.artifact_id}/download"
            if available else ""
        ),
        tenant_id=artifact.tenant_id,
        department_id=artifact.department_id,
        knowledge_base_name=artifact.knowledge_base_name,
        snapshot_id=snapshot_id,
        turn_id=turn_id,
        available=available,
    )


def _job_view(job: ExportJob, store: ResultExportStore, user: AuthUser) -> ExportJobView:
    artifact = store.get_artifact(user.id, job.artifact_id) if job.artifact_id else None
    snapshot = store.get_snapshot(user.id, job.snapshot_id)
    return ExportJobView(
        export_job_id=job.export_job_id,
        snapshot_id=job.snapshot_id,
        session_id=int(job.session_id) if job.session_id.isdecimal() else 0,
        turn_id=snapshot.turn_id if snapshot is not None else None,
        format=job.format,
        content_shape=job.content_shape,
        status=job.status,
        attempt=job.attempt,
        error_message=job.error_message,
        artifact=_artifact_view(
            artifact,
            snapshot_id=job.snapshot_id,
            turn_id=snapshot.turn_id if snapshot is not None else None,
        ),
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
        tenant_id=job.tenant_id,
        department_id=job.department_id,
        knowledge_base_name=job.knowledge_base_name,
    )


def _legacy_components(pipeline):
    document_generation = getattr(pipeline, "document_generation", None)
    store = getattr(document_generation, "store", None)
    if store is None:
        store = DocumentAuthoringStore()
    return document_generation, store


def _legacy_context(user: AuthUser, auth: AuthService, order):
    kb_name = str(getattr(order, "knowledge_base_name", "") or "").strip()
    if not kb_name:
        if str(getattr(order, "scope_type", "project") or "project") != "project":
            raise HTTPException(status_code=404, detail="artifact not found")
        if not str(getattr(order, "project_id", "") or "").strip():
            raise HTTPException(status_code=404, detail="artifact not found")
        # Project-scoped work orders do not carry a KB name.  Build the same
        # identity context and let the document service's project capability
        # check authorize the artifact below.
        ctx = build_context_for_user(user, auth=auth)
        reject_system_admin_kb_access(ctx)
        return ctx
    owner_department = getattr(order, "resource_department_id", None)
    if owner_department not in (None, "") and (
        user.department_id in (None, "") or str(owner_department) != str(user.department_id)
    ):
        raise HTTPException(status_code=403, detail="department scope no longer permits this artifact")
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    return ctx


def _legacy_artifact_payload(
    artifact_id: str,
    *,
    user: AuthUser,
    auth: AuthService,
    pipeline,
) -> tuple[LegacyArtifactView, bytes]:
    document_generation, store = _legacy_components(pipeline)
    get_artifact = getattr(store, "get_artifact", None)
    get_work_order = getattr(store, "get_work_order", None)
    if not callable(get_artifact) or not callable(get_work_order):
        raise HTTPException(status_code=404, detail="artifact not found")
    artifact = get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    order = get_work_order(getattr(artifact, "work_order_id", ""))
    if order is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    ctx = _legacy_context(user, auth, order)
    capability = (
        "download_approved_release"
        if getattr(artifact, "stage", "") == "approved_release"
        else "download_review_candidate"
    )
    require_capability = getattr(document_generation, "require_work_order_capability", None)
    if callable(require_capability):
        try:
            require_capability(ctx, order, capability)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    document_reader = getattr(document_generation, "download_document_artifact", None)
    store_reader = getattr(store, "read_artifact_content", None)
    if not callable(document_reader) and not callable(store_reader):
        raise HTTPException(status_code=404, detail="artifact file is unavailable")
    try:
        content = document_reader(ctx, artifact_id) if callable(document_reader) else store_reader(artifact_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (KeyError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="artifact file is unavailable") from exc
    target_format = str(getattr(order, "target_format", "bin") or "bin").strip().lower()
    extension = "md" if target_format == "markdown" else target_format
    safe_extension = re.sub(r"[^a-z0-9]+", "", extension) or "bin"
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", artifact_id).strip(".-") or "artifact"
    created_at = getattr(artifact, "created_at", "")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    view = LegacyArtifactView(
        artifact_id=artifact_id,
        work_order_id=str(getattr(artifact, "work_order_id", "")),
        stage=str(getattr(artifact, "stage", "")),
        format=target_format,
        filename=f"document-{safe_id}.{safe_extension}",
        mime_type=_LEGACY_MIME_TYPES.get(target_format, "application/octet-stream"),
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        preview=preview_artifact(content, target_format),
        created_at=str(created_at),
        preview_url=f"/api/v1/artifacts/document/{artifact_id}/preview",
        download_url=f"/api/v1/artifacts/document/{artifact_id}/download",
        tenant_id=str(getattr(order, "tenant_id", None) or getattr(artifact, "tenant_id", None) or "default"),
        department_id=(
            str(getattr(order, "resource_department_id"))
            if getattr(order, "resource_department_id", None) not in (None, "")
            else None
        ),
        knowledge_base_name=str(getattr(order, "knowledge_base_name", "") or ""),
    )
    return view, content


@router.get("/artifacts", response_model=list[ExportArtifactView])
def list_artifact_history(
    session_id: int | None = None,
    snapshot_id: str | None = None,
    artifact_format: str | None = Query(default=None, alias="format"),
    limit: int = Query(default=100, ge=1, le=200),
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: ResultExportStore = Depends(get_export_store),
):
    """Return append-only artifact history, including expired metadata.

    Expired entries remain useful for a revision timeline but are marked
    unavailable and never expose a working download surface.
    """

    try:
        entries = store.list_artifact_history(
            user.id,
            session_id=session_id,
            snapshot_id=snapshot_id,
            format=artifact_format,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    visible: list[ExportArtifactView] = []
    for entry in entries:
        snapshot = store.get_snapshot(user.id, entry.snapshot_id)
        if snapshot is None:
            continue
        try:
            _authorize_snapshot(user, snapshot, auth)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                continue
            raise
        view = _artifact_view(
            entry.artifact,
            snapshot_id=entry.snapshot_id,
            turn_id=entry.turn_id,
            available=entry.available,
        )
        if view is not None:
            visible.append(view)
    return visible


def _authorized_job(
    user: AuthUser,
    job: ExportJob,
    store: ResultExportStore,
    auth: AuthService,
):
    snapshot = store.get_snapshot(user.id, job.snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="export job not found")
    _authorize_snapshot(user, snapshot, auth)
    return snapshot


@router.post("/exports", response_model=ExportBatchView, status_code=202)
def create_exports(
    body: CreateExportRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    conv: ConversationService = Depends(get_conversation_service),
    store: ResultExportStore = Depends(get_export_store),
):
    formats = list(dict.fromkeys(body.formats))
    if len(formats) != len(body.formats):
        raise HTTPException(status_code=400, detail="formats must be unique")
    disabled_formats = [format_name for format_name in formats if not is_export_format_enabled(format_name)]
    if disabled_formats:
        raise HTTPException(
            status_code=409,
            detail=f"export formats are disabled: {', '.join(disabled_formats)}",
        )
    if len(json.dumps(body.options, ensure_ascii=False, default=str)) > 10_000:
        raise HTTPException(status_code=400, detail="export options are too large")
    render_options = _validate_render_options(body.options)
    snapshot_source = None
    try:
        if body.source_ref.kind == "snapshot":
            snapshot_source = store.get_snapshot(user.id, body.source_ref.id)
            if snapshot_source is None:
                raise HTTPException(status_code=404, detail="result source not found")
            _authorize_snapshot(user, snapshot_source, auth)
            turn = None
        else:
            turn = _resolve_turn(user, body.source_ref, conv)
            _authorize_kb_turn(user, turn, auth)
    except HTTPException as exc:
        _record_export_audit(
            user,
            action="create_export",
            target_type="export_source",
            target_id=body.source_ref.id,
            success=False,
            error_message=str(exc.detail),
            metadata={"formats": formats, "source_kind": body.source_ref.kind},
        )
        raise
    # Keep one canonical immutable snapshot per completed turn. Presentation
    # choices belong to each ExportJob so another format/title can reuse it.
    envelope = snapshot_source.envelope if snapshot_source is not None else envelope_from_turn(turn)
    render_options = dict(render_options)
    render_options["render_title"] = body.title
    render_options["include_citations"] = body.include_citations
    try:
        if snapshot_source is not None:
            session_id = snapshot_source.session_id
            turn_id = snapshot_source.turn_id
            assistant_message_id = snapshot_source.assistant_message_id
            department_id = snapshot_source.department_id
            tenant_id = snapshot_source.tenant_id
            source_kb_name = snapshot_source.knowledge_base_name
        else:
            session_id = turn.session_id
            turn_id = turn.id
            assistant_message_id = turn.assistant_message_id
            department_id = user.department_id
            export_context = build_context_for_user(user, turn.kb_name, auth=auth)
            tenant_id = str(getattr(export_context, "tenant_id", None) or "default")
            source_kb_name = "" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name
        request_id = (body.client_request_id or "").strip() or f"export-{uuid.uuid4().hex}"
        snapshot, jobs = store.enqueue_turn_exports(
            owner_user_id=user.id,
            tenant_id=tenant_id,
            department_id=department_id,
            knowledge_base_name=source_kb_name,
            session_id=session_id,
            turn_id=turn_id,
            assistant_message_id=assistant_message_id,
            envelope=envelope,
            formats=formats,
            content_shape=body.content_shape,
            client_request_id=request_id,
            title=body.title,
            include_citations=body.include_citations,
            options=render_options,
        )
    except (ValueError, KeyError) as exc:
        _record_export_audit(
            user,
            action="create_export",
            target_type="result_snapshot",
            target_id=body.source_ref.id,
            success=False,
            error_message=str(exc),
            metadata={"formats": formats},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_export_audit(
        user,
        action="create_export",
        target_type="result_snapshot",
        target_id=snapshot.snapshot_id,
        metadata={"formats": formats, "job_count": len(jobs), "source_kind": body.source_ref.kind},
        kb_name=(turn.kb_name if turn is not None else source_kb_name),
    )
    return ExportBatchView(
        snapshot_id=snapshot.snapshot_id,
        session_id=int(session_id) if str(session_id).isdecimal() else 0,
        source_ref=body.source_ref,
        jobs=[_job_view(job, store, user) for job in jobs],
    )


@router.get("/exports/formats", response_model=list[str])
def list_export_formats(user: AuthUser = Depends(current_user)):
    """Expose rollout-filtered formats so clients do not offer dead actions."""

    del user
    return list(enabled_export_formats())


@router.get("/exports", response_model=list[ExportJobView])
def list_exports(
    session_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=64, ge=1, le=200),
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: ResultExportStore = Depends(get_export_store),
):
    try:
        jobs = store.list_export_jobs(user.id, session_id=session_id, status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    visible: list[ExportJobView] = []
    for job in jobs:
        try:
            _authorized_job(user, job, store, auth)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                continue
            raise
        visible.append(_job_view(job, store, user))
    return visible


@router.get("/exports/{export_job_id}", response_model=ExportJobView)
def get_export(
    export_job_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: ResultExportStore = Depends(get_export_store),
):
    job = store.get_export_job(user.id, export_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="export job not found")
    try:
        _authorized_job(user, job, store, auth)
    except HTTPException as exc:
        _record_export_audit(
            user, action="get_export", target_type="export_job", target_id=export_job_id,
            success=False, error_message=str(exc.detail),
        )
        raise
    return _job_view(job, store, user)


@router.post("/exports/{export_job_id}/retry", response_model=ExportJobView, status_code=202)
def retry_export(
    export_job_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: ResultExportStore = Depends(get_export_store),
):
    job = store.get_export_job(user.id, export_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="export job not found")
    try:
        _authorized_job(user, job, store, auth)
    except HTTPException as exc:
        _record_export_audit(
            user, action="retry_export", target_type="export_job", target_id=export_job_id,
            success=False, error_message=str(exc.detail),
        )
        raise
    updated = store.retry(user.id, export_job_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="export job not found")
    _record_export_audit(user, action="retry_export", target_type="export_job", target_id=export_job_id)
    return _job_view(updated, store, user)


@router.post("/exports/{export_job_id}/cancel", response_model=ExportJobView)
def cancel_export(
    export_job_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: ResultExportStore = Depends(get_export_store),
):
    job = store.get_export_job(user.id, export_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="export job not found")
    try:
        _authorized_job(user, job, store, auth)
    except HTTPException as exc:
        _record_export_audit(
            user, action="cancel_export", target_type="export_job", target_id=export_job_id,
            success=False, error_message=str(exc.detail),
        )
        raise
    updated = store.cancel(user.id, export_job_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="export job not found")
    _record_export_audit(user, action="cancel_export", target_type="export_job", target_id=export_job_id)
    return _job_view(updated, store, user)


@router.get("/artifacts/document/{artifact_id}/preview", response_model=LegacyArtifactView)
def legacy_artifact_preview(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    pipeline=Depends(get_pipeline),
):
    view, _content = _legacy_artifact_payload(
        artifact_id, user=user, auth=auth, pipeline=pipeline,
    )
    _record_export_audit(
        user,
        action="preview_document_artifact_unified",
        target_type="document_artifact",
        target_id=artifact_id,
        kb_name=view.knowledge_base_name,
    )
    return view


@router.get("/artifacts/document/{artifact_id}/download")
def legacy_artifact_download(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    pipeline=Depends(get_pipeline),
):
    view, content = _legacy_artifact_payload(
        artifact_id, user=user, auth=auth, pipeline=pipeline,
    )
    _record_export_audit(
        user,
        action="download_document_artifact_unified",
        target_type="document_artifact",
        target_id=artifact_id,
        kb_name=view.knowledge_base_name,
        metadata={"format": view.format, "size": view.size, "sha256": view.sha256},
    )
    filename = re.sub(r"[\r\n\"]+", "-", view.filename)
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip(".-") or "document.bin"
    return Response(
        content=content,
        media_type=view.mime_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{quote(filename, safe='._-')}"
            )
        },
    )


@router.get("/artifacts/{artifact_id}/preview", response_model=ExportArtifactView)
def artifact_preview(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: ResultExportStore = Depends(get_export_store),
):
    artifact = store.get_artifact(user.id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    snapshot = store.get_snapshot_for_artifact(user.id, artifact_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        _authorize_snapshot(user, snapshot, auth)
    except HTTPException as exc:
        _record_export_audit(
            user, action="preview_export_artifact", target_type="export_artifact", target_id=artifact_id,
            success=False, error_message=str(exc.detail),
        )
        raise
    try:
        content = store.read_artifact(user.id, artifact_id)
    except (KeyError, PermissionError, ValueError) as exc:
        _record_export_audit(
            user, action="preview_export_artifact", target_type="export_artifact", target_id=artifact_id,
            success=False, error_message=str(exc),
        )
        raise HTTPException(status_code=404, detail="artifact file is unavailable") from exc
    parsed_preview = artifact.preview
    if artifact.format in {"xlsx", "docx"}:
        # Office previews are parsed from the authenticated bytes and bounded
        # by the shared safe preview parser; renderer metadata remains as a
        # fallback when a package is malformed.
        parsed_preview = {**parsed_preview, **preview_artifact(content, artifact.format)}
    view = _artifact_view(
        artifact,
        snapshot_id=snapshot.snapshot_id,
        turn_id=snapshot.turn_id,
    )
    assert view is not None
    view = view.model_copy(update={"preview": parsed_preview})
    _record_export_audit(user, action="preview_export_artifact", target_type="export_artifact", target_id=artifact_id)
    return view


@router.get("/artifacts/{artifact_id}/download")
def artifact_download(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    store: ResultExportStore = Depends(get_export_store),
):
    artifact = store.get_artifact(user.id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    snapshot = store.get_snapshot_for_artifact(user.id, artifact_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        _authorize_snapshot(user, snapshot, auth)
    except HTTPException as exc:
        _record_export_audit(
            user, action="download_export_artifact", target_type="export_artifact", target_id=artifact_id,
            success=False, error_message=str(exc.detail),
        )
        raise
    try:
        content = store.read_artifact(user.id, artifact_id)
    except (KeyError, PermissionError, ValueError) as exc:
        _record_export_audit(
            user, action="download_export_artifact", target_type="export_artifact", target_id=artifact_id,
            success=False, error_message=str(exc),
        )
        raise HTTPException(status_code=404, detail="artifact file is unavailable") from exc
    try:
        observability_metrics.record_export_download(format=artifact.format, status="succeeded")
    except Exception:
        pass
    filename = re.sub(r"[\r\n\"]+", "-", artifact.filename)
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip(".-") or f"export.{artifact.format}"
    _record_export_audit(
        user,
        action="download_export_artifact",
        target_type="export_artifact",
        target_id=artifact_id,
        metadata={"format": artifact.format, "size": artifact.size, "sha256": artifact.sha256},
    )
    return Response(
        content=content,
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{quote(filename, safe='._-')}"
            )
        },
    )
