"""Chat attachment HTTP API (design §14.1).

Routes are session-scoped and ownership-checked twice: the session must
belong to the caller, and every attachment row must match the same
(session, user). Cross-user ids return 404 without leaking existence.
Uploads stream to private storage and enqueue a background parse job; the
feature flag defaults on but remains opt-out configurable.
"""

from __future__ import annotations

import src.settings
from functools import partial
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.api.deps import (
    current_user,
    get_auth_service,
    reject_system_admin_kb_access,
    require_employee,
)
from src.api.context import build_context_for_user
from src.api.schemas import OkResponse
from src.attachments.audit import begin_attachment_audit, finish_attachment_audit
from src.core.auth import AuthService, AuthUser
from src.core.conversation import ConversationService
from src.observability.metrics import record_attachment

from src.attachments.models import (
    AttachmentError,
    AttachmentNotFound,
    AttachmentPermissionError,
    AttachmentQuotaExceeded,
    AttachmentUnsupportedType,
    PARSE_STATUS_QUEUED,
)
from src.attachments.service import AttachmentService, revision_change_summary

router = APIRouter(tags=["attachments"])


def _conv_service() -> ConversationService:
    return ConversationService()


def _attachment_service() -> AttachmentService:
    return AttachmentService()


def _require_enabled() -> None:
    if not src.settings.CHAT_ATTACHMENTS_ENABLED:
        raise HTTPException(status_code=404, detail="chat attachments are not enabled")


def _require_session(user: AuthUser, auth: AuthService, session_id: int):
    conv = _conv_service()
    session = conv.get_session(user.id, session_id)
    if session is None:
        # 404 (not 403) so foreign session ids do not leak existence.
        raise HTTPException(status_code=404, detail="session not found")
    reject_system_admin_kb_access(build_context_for_user(user, session.kb_name, auth=auth))
    return session


def _map_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AttachmentNotFound):
        return HTTPException(status_code=404, detail="attachment not found")
    if isinstance(exc, AttachmentPermissionError):
        return HTTPException(status_code=404, detail="attachment not found")
    if isinstance(exc, AttachmentUnsupportedType):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, AttachmentQuotaExceeded):
        return HTTPException(status_code=413, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _attachment_view(record) -> dict:
    manifest = dict(getattr(record, "manifest", {}) or {})
    degraded_reasons = manifest.get("degraded_reasons") or []
    if not isinstance(degraded_reasons, list):
        degraded_reasons = [str(degraded_reasons)]
    return {
        "attachment_id": record.attachment_id,
        "asset_id": record.asset_id,
        "filename": record.filename,
        "media_type": record.media_type,
        "extension": record.extension,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "usage_hint": record.usage_hint,
        "status": record.status,
        "parse_status": record.parse_status or PARSE_STATUS_QUEUED,
        "error_code": record.error_code or "",
        "error_message": record.error_message or "",
        "manifest": manifest,
        "degraded_reasons": [str(reason) for reason in degraded_reasons],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "expires_at": record.expires_at,
    }


@router.post("/conversations/{session_id}/attachments", status_code=201)
async def upload_attachment(
    session_id: int,
    file: UploadFile,
    client_request_id: str = "",
    usage_hint: str = "reference",
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_enabled()
    session = _require_session(user, auth, session_id)
    service = _attachment_service()
    attempt = begin_attachment_audit(
        action="attachment.upload",
        target_type="chat_attachment",
        target_id=f"session:{session_id}",
        actor=user,
        kb_name=session.kb_name,
        metadata={
            "session_id": session_id,
            "filename": str(getattr(file, "filename", "") or "")[:500],
            "usage_hint": str(usage_hint or "")[:100],
        },
    )
    try:
        record = await run_upload(
            service,
            session_id=session_id,
            user_id=user.id,
            tenant_id=user.tenant_id,
            file=file,
            client_request_id=client_request_id,
            usage_hint=usage_hint,
        )
    except AttachmentError as exc:
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={
                "session_id": session_id,
                "filename": str(getattr(file, "filename", "") or "")[:500],
                "usage_hint": str(usage_hint or "")[:100],
            },
        )
        raise _map_service_error(exc) from exc
    finish_attachment_audit(
        attempt,
        success=True,
        target_id=record.attachment_id,
        metadata={
            "session_id": session_id,
            "filename": record.filename,
            "size_bytes": record.size_bytes,
            "media_type": record.media_type,
            "sha256": record.sha256,
            "parse_status": record.parse_status,
            "usage_hint": record.usage_hint,
        },
    )
    record_attachment("upload", status="ok")
    return _attachment_view(record)


async def run_upload(
    service: AttachmentService,
    *,
    session_id: int,
    user_id: int,
    tenant_id: str = "default",
    file: UploadFile,
    client_request_id: str,
    usage_hint: str,
    declared_media_type: str | None = None,
):
    # Spool to an in-process temp buffer is avoided: Starlette's SpooledTemporaryFile
    # already streams to disk above a threshold, and our storage writer streams
    # chunks from it so peak memory stays bounded.
    return await run_in_threadpool(
        partial(
            service.upload,
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            filename=file.filename or "attachment",
            stream=file.file,
            client_request_id=client_request_id,
            usage_hint=usage_hint,
            declared_media_type=(
                declared_media_type
                if declared_media_type is not None
                else getattr(file, "content_type", None)
            ),
        )
    )


@router.get("/conversations/{session_id}/attachments")
def list_attachments(
    session_id: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_enabled()
    _require_session(user, auth, session_id)
    service = _attachment_service()
    records = service.list_attachments(session_id=session_id, user_id=user.id)
    return [_attachment_view(record) for record in records]


@router.get("/conversations/{session_id}/attachments/{attachment_id}")
def get_attachment(
    session_id: int,
    attachment_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_enabled()
    _require_session(user, auth, session_id)
    service = _attachment_service()
    try:
        record = service.get_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user.id
        )
    except AttachmentError as exc:
        raise _map_service_error(exc) from exc
    return _attachment_view(record)


@router.get("/conversations/{session_id}/attachments/{attachment_id}/download")
def download_attachment(
    session_id: int,
    attachment_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    """Download an attachment after the same session/user ACL check as reads."""
    _require_enabled()
    _require_session(user, auth, session_id)
    service = _attachment_service()
    try:
        record, content = service.read_source_bytes(
            attachment_id=attachment_id,
            session_id=session_id,
            user_id=user.id,
        )
    except AttachmentError as exc:
        raise _map_service_error(exc) from exc
    filename = quote(str(record.filename or "attachment"), safe="")
    return Response(
        content=content,
        media_type=record.media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.delete("/conversations/{session_id}/attachments/{attachment_id}", response_model=OkResponse)
def delete_attachment(
    session_id: int,
    attachment_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_enabled()
    session = _require_session(user, auth, session_id)
    service = _attachment_service()
    attempt = begin_attachment_audit(
        action="attachment.delete",
        target_type="chat_attachment",
        target_id=attachment_id,
        actor=user,
        kb_name=session.kb_name,
        metadata={"session_id": session_id},
    )
    try:
        service.delete_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user.id
        )
    except AttachmentError as exc:
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={"session_id": session_id},
        )
        raise _map_service_error(exc) from exc
    finish_attachment_audit(
        attempt,
        success=True,
        metadata={"session_id": session_id},
    )
    record_attachment("cleanup", status="deleted")
    return OkResponse(ok=True)


@router.post("/conversations/{session_id}/attachments/{attachment_id}/retry")
def retry_attachment(
    session_id: int,
    attachment_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_enabled()
    session = _require_session(user, auth, session_id)
    service = _attachment_service()
    attempt = begin_attachment_audit(
        action="attachment.retry_parse",
        target_type="chat_attachment",
        target_id=attachment_id,
        actor=user,
        kb_name=session.kb_name,
        metadata={"session_id": session_id},
    )
    try:
        record = service.retry_parse(
            attachment_id=attachment_id, session_id=session_id, user_id=user.id
        )
    except AttachmentError as exc:
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={"session_id": session_id},
        )
        raise _map_service_error(exc) from exc
    finish_attachment_audit(
        attempt,
        success=True,
        target_id=record.attachment_id,
        metadata={
            "session_id": session_id,
            "filename": record.filename,
            "parse_status": record.parse_status,
            "error_code": record.error_code,
        },
    )
    return _attachment_view(record)


class AttachmentReleaseRequestIn(BaseModel):
    comment: str = Field(default="", max_length=2000)


class AttachmentReleaseReviewIn(BaseModel):
    decision: str = Field(default="approve")
    comment: str = Field(default="", max_length=2000)


def _revision_view(revision) -> dict:
    return {
        "revision_id": revision.revision_id,
        "parent_attachment_id": revision.parent_attachment_id,
        "child_attachment_id": revision.child_attachment_id,
        "session_id": revision.session_id,
        "operation": revision.operation,
        "status": revision.status,
        "release_state": revision.release_state,
        "release_requested_by": revision.release_requested_by,
        "release_requested_at": revision.release_requested_at,
        "release_comment": revision.release_comment,
        "reviewed_by": revision.reviewed_by,
        "reviewed_at": revision.reviewed_at,
        "review_comment": revision.review_comment,
        "released_attachment_id": revision.released_attachment_id,
        "details": dict(revision.details or {}),
        "change_summary": revision_change_summary(revision),
        # Bounded before/after so a reviewer can inspect the exact edit
        # source without downloading the candidate.
        "before": str(revision.before or "")[:8000],
        "after": str(revision.after or "")[:8000],
        "parent_content_hash": revision.parent_content_hash,
        "child_content_hash": revision.child_content_hash,
        "created_at": revision.created_at,
        "updated_at": revision.updated_at,
    }


def _reviewer_ctx(user: AuthUser, auth: AuthService, session_id: int):
    """Resolve a governance reviewer's context for one conversation.

    Review is intentionally not owner-scoped: a department KB manager must be
    able to approve/reject a candidate created by another user in the same
    KB.  The session's KB is resolved first, then KB write permission and the
    system-admin content ban are enforced.
    """

    session = _conv_service().get_session_unscoped(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    ctx = build_context_for_user(user, session.kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(session.kb_name, "write"):
        raise HTTPException(
            status_code=403,
            detail="knowledge base write permission is required to review releases",
        )
    return session


@router.get(
    "/conversations/{session_id}/attachments/revisions/{revision_id}/candidate-download"
)
def download_attachment_revision_candidate(
    session_id: int,
    revision_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    """Download a candidate for release review (KB write permission, not owner)."""

    _require_enabled()
    _reviewer_ctx(user, auth, session_id)
    service = _attachment_service()
    revision = service.get_attachment_revision(
        revision_id=revision_id, session_id=session_id
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="attachment revision not found")
    try:
        record, content = service.read_source_bytes(
            attachment_id=revision.child_attachment_id,
            session_id=session_id,
            user_id=revision.user_id,
        )
    except AttachmentError as exc:
        raise _map_service_error(exc) from exc
    filename = quote(str(record.filename or "candidate"), safe="")
    return Response(
        content=content,
        media_type=record.media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post("/attachments/reparse-stale")
def reparse_stale_attachments(
    limit: int = 200,
    extension: list[str] | None = Query(default=None),
    actor: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
):
    """Backfill derived parser data for stale assets in manageable KBs.

    Only assets whose session belongs to a KB the department manager can
    write are enqueued; source bytes are never touched.
    """

    _require_enabled()
    service = _attachment_service()
    conv = _conv_service()
    extensions = [
        item.strip().lower() for item in (extension or []) if item.strip()
    ] or None
    assets = service.list_stale_assets(extensions=extensions, limit=limit)
    attempt = begin_attachment_audit(
        action="attachment.reparse_stale",
        target_type="attachment_batch",
        target_id="reparse-stale",
        actor=actor,
        metadata={
            "scanned": len(assets),
            "limit": limit,
            "extensions": [str(item)[:50] for item in (extensions or [])[:50]],
        },
    )
    queued: list[str] = []
    try:
        for asset in assets:
            session = conv.get_session_unscoped(asset.session_id)
            if session is None or not session.kb_name:
                continue
            ctx = build_context_for_user(actor, session.kb_name, auth=auth)
            if not ctx.has_kb_permission(session.kb_name, "write"):
                continue
            if service.reparse_asset(asset):
                queued.append(asset.asset_id)
    except Exception as exc:
        finish_attachment_audit(attempt, success=False, error_message=str(exc))
        raise
    finish_attachment_audit(
        attempt,
        success=True,
        metadata={
            "scanned": len(assets),
            "queued": len(queued),
            "limit": limit,
            "extensions": [str(item)[:50] for item in (extensions or [])[:50]],
        },
    )
    return {
        "scanned": len(assets),
        "queued": len(queued),
        "queued_asset_ids": queued,
    }


@router.get("/attachment-releases/pending")
def list_pending_attachment_releases(
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    """Pending attachment release reviews for the caller's writable KBs.

    The queue is cross-session: a department KB manager reviews candidates
    created by other users in the same KB.  Items outside the caller's write
    permission are silently omitted (no existence leak).
    """

    _require_enabled()
    service = _attachment_service()
    conv = _conv_service()
    items: list[dict] = []
    for revision in service.list_pending_attachment_releases():
        session = conv.get_session_unscoped(revision.session_id)
        if session is None or not session.kb_name:
            continue
        ctx = build_context_for_user(user, session.kb_name, auth=auth)
        if not ctx.has_kb_permission(session.kb_name, "write"):
            continue
        items.append(_pending_release_view(revision, session, service))
    return items


def _pending_release_view(revision, session, service) -> dict:
    def _name(attachment_id: str) -> str:
        try:
            return service.get_attachment(
                attachment_id=attachment_id,
                session_id=revision.session_id,
                user_id=revision.user_id,
            ).filename
        except Exception:  # noqa: BLE001 - review queue must stay fail-soft
            return attachment_id

    return {
        "revision_id": revision.revision_id,
        "session_id": revision.session_id,
        "kb_name": session.kb_name or "",
        "session_title": getattr(session, "title", "") or "",
        "owner_user_id": revision.user_id,
        "child_attachment_id": revision.child_attachment_id,
        "candidate_filename": _name(revision.child_attachment_id),
        "parent_filename": _name(revision.parent_attachment_id),
        "operation": revision.operation,
        "change_summary": revision_change_summary(revision),
        "release_state": revision.release_state,
        "release_comment": revision.release_comment,
        "release_requested_by": revision.release_requested_by,
        "release_requested_at": revision.release_requested_at,
        # Reviewer-authorized route: the owner-scoped download endpoint would
        # 404 for a KB manager who does not own the conversation.
        "candidate_download_url": (
            f"/api/v1/conversations/{revision.session_id}/attachments/revisions/"
            f"{revision.revision_id}/candidate-download"
        ),
    }


@router.post(
    "/conversations/{session_id}/attachments/revisions/{revision_id}/release-request"
)
def request_attachment_release(
    session_id: int,
    revision_id: str,
    payload: AttachmentReleaseRequestIn,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    """Record explicit release-review intent; never releases by itself."""

    _require_enabled()
    session = _require_session(user, auth, session_id)
    service = _attachment_service()
    attempt = begin_attachment_audit(
        action="attachment.release_request",
        target_type="attachment_revision",
        target_id=revision_id,
        actor=user,
        kb_name=session.kb_name,
        metadata={
            "session_id": session_id,
            "source": "api",
            "comment_length": len(payload.comment or ""),
        },
    )
    try:
        revision = service.request_attachment_release(
            revision_id=revision_id,
            session_id=session_id,
            user_id=user.id,
            comment=payload.comment,
        )
    except AttachmentError as exc:
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={"session_id": session_id, "source": "api"},
        )
        raise _map_service_error(exc) from exc
    except ValueError as exc:
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={"session_id": session_id, "source": "api"},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finish_attachment_audit(
        attempt,
        success=True,
        target_id=revision.revision_id,
        metadata={
            "session_id": session_id,
            "operation": revision.operation,
            "release_state": revision.release_state,
            "comment_length": len(payload.comment or ""),
            "source": "api",
        },
    )
    return _revision_view(revision)


@router.post(
    "/conversations/{session_id}/attachments/revisions/{revision_id}/release-review"
)
def review_attachment_release(
    session_id: int,
    revision_id: str,
    payload: AttachmentReleaseReviewIn,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    """Approve (creates a separate released copy) or reject a pending request."""

    _require_enabled()
    session = _reviewer_ctx(user, auth, session_id)
    decision = str(payload.decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="decision must be approve or reject")
    service = _attachment_service()
    attempt = begin_attachment_audit(
        action="attachment.release_review",
        target_type="attachment_revision",
        target_id=revision_id,
        actor=user,
        kb_name=session.kb_name,
        metadata={
            "session_id": session_id,
            "decision": decision,
            "source": "api",
        },
    )
    try:
        revision = service.review_attachment_release(
            revision_id=revision_id,
            session_id=session_id,
            reviewer_id=user.id,
            approve=decision == "approve",
            comment=payload.comment,
        )
    except AttachmentError as exc:
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={
                "session_id": session_id,
                "decision": decision,
                "source": "api",
            },
        )
        raise _map_service_error(exc) from exc
    except ValueError as exc:
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={
                "session_id": session_id,
                "decision": decision,
                "source": "api",
            },
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finish_attachment_audit(
        attempt,
        success=True,
        target_id=revision.revision_id,
        metadata={
            "session_id": session_id,
            "decision": decision,
            "operation": revision.operation,
            "release_state": revision.release_state,
            "comment_length": len(payload.comment or ""),
            "source": "api",
        },
    )
    return _revision_view(revision)


@router.get("/conversations/{session_id}/attachments/revisions/{revision_id}")
def get_attachment_revision(
    session_id: int,
    revision_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_enabled()
    session = _conv_service().get_session_unscoped(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    is_owner = session.user_id == user.id
    if not is_owner:
        _reviewer_ctx(user, auth, session_id)
    service = _attachment_service()
    revision = service.get_attachment_revision(
        revision_id=revision_id,
        session_id=session_id,
        user_id=session.user_id if is_owner else None,
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="attachment revision not found")
    return _revision_view(revision)
