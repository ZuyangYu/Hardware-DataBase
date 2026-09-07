"""Chat attachment HTTP API (design §14.1).

Routes are session-scoped and ownership-checked twice: the session must
belong to the caller, and every attachment row must match the same
(session, user). Cross-user ids return 404 without leaking existence.
Uploads stream to private storage and enqueue a background parse job; the
feature flag defaults on but remains opt-out configurable.
"""

from __future__ import annotations

import src.settings
from fastapi import APIRouter, Depends, HTTPException, UploadFile

from src.api.deps import current_user, get_auth_service, reject_system_admin_kb_access
from src.api.context import build_context_for_user
from src.api.schemas import OkResponse
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
from src.attachments.service import AttachmentService

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
    _require_session(user, auth, session_id)
    service = _attachment_service()
    try:
        record = await run_upload(service, session_id=session_id, user_id=user.id, file=file,
                                  client_request_id=client_request_id, usage_hint=usage_hint)
    except AttachmentError as exc:
        raise _map_service_error(exc) from exc
    record_attachment("upload", status="ok")
    return _attachment_view(record)


async def run_upload(
    service: AttachmentService,
    *,
    session_id: int,
    user_id: int,
    file: UploadFile,
    client_request_id: str,
    usage_hint: str,
    declared_media_type: str | None = None,
):
    # Spool to an in-process temp buffer is avoided: Starlette's SpooledTemporaryFile
    # already streams to disk above a threshold, and our storage writer streams
    # chunks from it so peak memory stays bounded.
    return service.upload(
        session_id=session_id,
        user_id=user_id,
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


@router.delete("/conversations/{session_id}/attachments/{attachment_id}", response_model=OkResponse)
def delete_attachment(
    session_id: int,
    attachment_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_enabled()
    _require_session(user, auth, session_id)
    service = _attachment_service()
    try:
        service.delete_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user.id
        )
    except AttachmentError as exc:
        raise _map_service_error(exc) from exc
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
    _require_session(user, auth, session_id)
    service = _attachment_service()
    try:
        record = service.retry_parse(
            attachment_id=attachment_id, session_id=session_id, user_id=user.id
        )
    except AttachmentError as exc:
        raise _map_service_error(exc) from exc
    return _attachment_view(record)
