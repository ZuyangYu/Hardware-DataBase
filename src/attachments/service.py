"""Attachment lifecycle operations with session-scope ACL (design §5/§14).

Every public method re-verifies ``user_id + session_id (+ attachment_id)``
against the stored row. Upload enforces extension/size/quota limits, streams
to private storage, dedupes assets per (tenant, user, session, sha256), and
enqueues a parse job. Deletion is soft; asset cleanup follows the last
reference. The service never widens scope and never exposes local paths.
"""

from __future__ import annotations


import src.settings
from src.attachments import storage
from src.attachments.models import (
    AttachmentNotFound,
    AttachmentQuotaExceeded,
    AttachmentRecord,
    AttachmentRef,
    AttachmentUnsupportedType,
    PARSE_STATUS_QUEUED,
    PARSE_STATUS_FAILED,
    REJECTED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    USAGE_HINT_DATA,
    USAGE_HINT_REFERENCE,
    safe_filename,
    split_extension,
)
from src.attachments.store import AttachmentStore

_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".edf": "application/x-edif",
    ".edif": "application/x-edif",
}


def _mime_for_extension(extension: str) -> str:
    return _MIME_BY_EXTENSION.get(extension, "application/octet-stream")


def _validate_declared_media_type(extension: str, declared_media_type: str | None) -> None:
    declared = str(declared_media_type or "").split(";", 1)[0].strip().lower()
    if not declared or declared == "application/octet-stream":
        return
    # MIME type tokens are case-insensitive (browsers commonly vary the
    # ``macroEnabled`` casing for .xlsm), so compare normalized values.
    expected = _mime_for_extension(extension).lower()
    if declared != expected:
        raise AttachmentUnsupportedType(
            f"declared MIME type '{declared}' does not match {extension} ({expected})"
        )


def _validate_file_signature(path: str, extension: str) -> None:
    """Check cheap magic bytes before an upload becomes a durable asset."""
    import os
    with open(path, "rb") as stream:
        prefix = stream.read(16)
    if extension == ".pdf" and not prefix.startswith(b"%PDF-"):
        raise AttachmentUnsupportedType("PDF signature is invalid")
    if extension in {".docx", ".xlsx", ".xlsm"} and not prefix.startswith(b"PK"):
        raise AttachmentUnsupportedType("Office package signature is invalid")
    if extension in {".docx", ".xlsx", ".xlsm"}:
        import zipfile

        if not zipfile.is_zipfile(path):
            raise AttachmentUnsupportedType("Office package is not a valid ZIP container")
    if extension in {".edf", ".edif"}:
        with open(path, "rb") as stream:
            sample = stream.read(min(4096, os.path.getsize(path)))
        try:
            text = sample.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n")
        except Exception as exc:
            raise AttachmentUnsupportedType("EDIF signature is invalid") from exc
        if not text.lower().startswith("(edif"):
            raise AttachmentUnsupportedType("EDIF signature is invalid")


class AttachmentService:
    def __init__(self, store: AttachmentStore | None = None):
        self.store = store or AttachmentStore()

    # -- upload -------------------------------------------------------------

    def upload(
        self,
        *,
        session_id: int,
        user_id: int,
        filename: str,
        stream,
        client_request_id: str | None = None,
        usage_hint: str = USAGE_HINT_REFERENCE,
        tenant_id: str = "default",
        declared_media_type: str | None = None,
    ) -> AttachmentRecord:
        if not src.settings.CHAT_ATTACHMENTS_ENABLED:
            raise AttachmentUnsupportedType("chat attachments are disabled")
        request_id = (client_request_id or "").strip()[:128] or None
        if request_id:
            existing = self.store.get_attachment_by_client_request_id(
                session_id=session_id,
                user_id=user_id,
                client_request_id=request_id,
                include_deleted=True,
            )
            if existing is not None:
                # Idempotency is resolved before quota checks and before the
                # stream is consumed.  A retried request therefore cannot
                # create a second asset or be rejected merely because the
                # session is now full.
                return existing
        filename = safe_filename(filename)
        extension = split_extension(filename)
        if extension in REJECTED_EXTENSIONS or not extension:
            raise AttachmentUnsupportedType(
                f"unsupported attachment type '{extension or 'unknown'}'; "
                f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
        if extension not in SUPPORTED_EXTENSIONS:
            raise AttachmentUnsupportedType(
                f"unsupported attachment type '{extension}'; "
                f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
        _validate_declared_media_type(extension, declared_media_type)
        if usage_hint not in {USAGE_HINT_REFERENCE, USAGE_HINT_DATA}:
            usage_hint = USAGE_HINT_REFERENCE
        max_bytes = int(src.settings.CHAT_ATTACHMENT_MAX_BYTES)
        max_files = int(src.settings.CHAT_ATTACHMENT_MAX_FILES_PER_SESSION)
        if self.store.count_active_attachments(session_id=session_id, user_id=user_id) >= max_files:
            raise AttachmentQuotaExceeded(
                f"session attachment limit reached ({max_files} files)"
            )

        # Provisional id for the storage path; the asset row may dedupe away
        # but the upload bytes must land somewhere durable first.
        import uuid

        staging_id = uuid.uuid4().hex
        path, sha256, size = storage.write_source_stream(
            session_id, staging_id, extension, stream
        )
        if size == 0:
            storage.delete_storage_key(os_rel(path))
            raise AttachmentUnsupportedType("empty file")
        if size > max_bytes:
            storage.delete_storage_key(os_rel(path))
            raise AttachmentQuotaExceeded(
                f"attachment exceeds the size limit ({max_bytes} bytes)"
            )
        max_session_bytes = int(getattr(src.settings, "CHAT_ATTACHMENT_MAX_SESSION_BYTES", 0))
        max_tenant_bytes = int(getattr(src.settings, "CHAT_ATTACHMENT_MAX_TENANT_BYTES", 0))
        if max_session_bytes > 0:
            current_session_bytes = self.store.sum_active_attachment_bytes(
                session_id=session_id,
                user_id=user_id,
            )
            if current_session_bytes + size > max_session_bytes:
                storage.delete_storage_key(os_rel(path))
                raise AttachmentQuotaExceeded(
                    f"session attachment byte limit reached ({max_session_bytes} bytes)"
                )
        if max_tenant_bytes > 0:
            current_tenant_bytes = self.store.sum_active_attachment_bytes(
                tenant_id=tenant_id,
            )
            if current_tenant_bytes + size > max_tenant_bytes:
                storage.delete_storage_key(os_rel(path))
                raise AttachmentQuotaExceeded(
                    f"tenant attachment byte limit reached ({max_tenant_bytes} bytes)"
                )
        try:
            _validate_file_signature(path, extension)
        except Exception:
            storage.delete_storage_key(os_rel(path))
            raise

        media_type = _mime_for_extension(extension)

        asset, asset_created = self.store.create_asset(
            session_id=session_id,
            user_id=user_id,
            sha256=sha256,
            media_type=media_type,
            extension=extension,
            size_bytes=size,
            storage_key=os_rel(path),
            tenant_id=tenant_id,
        )
        if asset_created:
            # Keep the canonical copy under the asset id.
            canonical_path = storage.asset_source_path(session_id, asset.asset_id, extension)
            try:
                import os

                os.makedirs(os.path.dirname(canonical_path), exist_ok=True)
                os.replace(path, canonical_path)
                canonical_key = os_rel(canonical_path)
                self.store.update_asset_status(
                    asset.asset_id,
                    parse_status=asset.parse_status,
                )
                self._relink_asset_storage(asset.asset_id, canonical_key)
                storage.delete_storage_key(os_rel(path))
            except OSError:
                # Canonical move is an optimization; the staging copy stays valid.
                pass
        else:
            # Asset reuse: the staged duplicate is redundant.
            storage.delete_storage_key(os_rel(path))

        try:
            record, created = self.store.create_attachment(
                session_id=session_id,
                user_id=user_id,
                asset_id=asset.asset_id,
                client_request_id=request_id,
                filename=filename,
                media_type=media_type,
                extension=extension,
                size_bytes=size,
                sha256=sha256,
                usage_hint=usage_hint,
                tenant_id=tenant_id,
            )
        except Exception:
            if asset_created and self.store.asset_reference_count(asset_id=asset.asset_id) == 0:
                self._cleanup_asset(asset.asset_id)
            raise
        if not asset_created:
            try:
                from src.observability.metrics import record_attachment

                record_attachment("asset_reuse", status="reused")
            except Exception:
                pass
        if created:
            self.store.enqueue_parse_job(
                asset_id=asset.asset_id,
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        elif asset_created and self.store.asset_reference_count(asset_id=asset.asset_id) == 0:
            # A concurrent/repeated request can win the attachment
            # idempotency race after this upload created its provisional
            # asset.  Reclaim that now-unreferenced asset and its source copy.
            self._cleanup_asset(asset.asset_id)
        return self.get_attachment(
            attachment_id=record.attachment_id, session_id=session_id, user_id=user_id
        )  # type: ignore[return-value]

    def _relink_asset_storage(self, asset_id: str, storage_key: str) -> None:
        from contextlib import closing

        with closing(self.store._connect()) as conn:
            conn.execute(
                "UPDATE chat_attachment_assets SET storage_key = ? WHERE asset_id = ?",
                (storage_key, asset_id),
            )

    # -- reads --------------------------------------------------------------

    def get_attachment(
        self, *, attachment_id: str, session_id: int, user_id: int
    ) -> AttachmentRecord:
        record = self.store.get_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user_id
        )
        if record is None:
            # 404-equivalent for both missing and foreign rows: never reveal
            # whether another user's attachment id exists.
            raise AttachmentNotFound("attachment not found")
        return record

    def list_attachments(self, *, session_id: int, user_id: int) -> list[AttachmentRecord]:
        return self.store.list_attachments(session_id=session_id, user_id=user_id)

    def build_refs(self, records: list[AttachmentRecord]) -> list[AttachmentRef]:
        refs: list[AttachmentRef] = []
        for record in records:
            asset = self.store.get_asset(record.asset_id)
            manifest = dict(getattr(record, "manifest", {}) or {})
            degraded_reasons = manifest.get("degraded_reasons") or []
            if isinstance(degraded_reasons, str):
                degraded_reasons = [degraded_reasons]
            elif not isinstance(degraded_reasons, list):
                degraded_reasons = [str(degraded_reasons)]
            degraded_reason = record.error_message
            if not degraded_reason and degraded_reasons:
                degraded_reason = "; ".join(str(reason) for reason in degraded_reasons[:3])
            refs.append(
                AttachmentRef(
                    attachment_id=record.attachment_id,
                    asset_id=record.asset_id,
                    session_id=record.session_id,
                    filename=record.filename,
                    media_type=record.media_type,
                    extension=record.extension,
                    size_bytes=record.size_bytes,
                    sha256=record.sha256,
                    usage_hint=record.usage_hint,
                    parse_status=record.parse_status,
                    parser_version=asset.parser_version if asset is not None else "",
                    degraded_reason=degraded_reason,
                )
            )
        return refs

    def resolve_refs_for_turn(
        self, *, user_id: int, session_id: int, attachment_ids: list[str]
    ) -> tuple[list[AttachmentRef], list[str]]:
        """ACL-verify requested attachments for a turn.

        Returns (refs, blocked_ids). Unknown, foreign-session, deleted, or
        failed-parse attachments never silently widen scope: blocked ids are
        reported so the API layer can fail the request.
        """
        refs: list[AttachmentRef] = []
        blocked: list[str] = []
        seen: set[str] = set()
        for attachment_id in attachment_ids:
            attachment_id = str(attachment_id or "").strip()
            if not attachment_id or attachment_id in seen:
                continue
            seen.add(attachment_id)
            try:
                record = self.get_attachment(
                    attachment_id=attachment_id, session_id=session_id, user_id=user_id
                )
            except AttachmentNotFound:
                blocked.append(attachment_id)
                continue
            if record.parse_status == PARSE_STATUS_FAILED:
                blocked.append(attachment_id)
                continue
            refs.extend(self.build_refs([record]))
        return refs, blocked

    # -- writes -------------------------------------------------------------

    def delete_attachment(self, *, attachment_id: str, session_id: int, user_id: int) -> bool:
        record = self.get_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user_id
        )
        deleted = self.store.soft_delete_attachment(
            attachment_id=record.attachment_id, session_id=session_id, user_id=user_id
        )
        if deleted and self.store.asset_reference_count(asset_id=record.asset_id) == 0:
            self._cleanup_asset(record.asset_id)
        if deleted:
            # Deletion can remove the last asset row, so no parse worker event
            # is guaranteed to arrive for a turn that is currently waiting on
            # this attachment.  Resolve that waiter synchronously; the
            # coordinator is imported lazily to keep the service boundary
            # independent from ConversationService at import time.
            try:
                from src.attachments.coordinator import AttachmentTurnCoordinator

                AttachmentTurnCoordinator(store=self.store).notify_attachment_deleted(
                    record.attachment_id
                )
            except Exception:
                # Attachment deletion itself is already durable.  A later
                # reconciliation pass can still resolve the waiting turn.
                pass
        return deleted

    def retry_parse(self, *, attachment_id: str, session_id: int, user_id: int) -> AttachmentRecord:
        record = self.get_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user_id
        )
        # Enqueue first so a queue failure leaves the old failure state
        # visible and retryable.  Once a live job exists, expose the asset as
        # queued and clear stale diagnostics for both the UI and turn ACL.
        self.store.enqueue_parse_job(
            asset_id=record.asset_id,
            session_id=session_id,
            user_id=user_id,
            tenant_id=record.tenant_id,
        )
        self.store.update_asset_status(
            record.asset_id,
            parse_status=PARSE_STATUS_QUEUED,
            manifest={},
            error_code="",
            error_message="",
        )
        return self.get_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user_id
        )  # type: ignore[return-value]

    def expire_due_attachments(self, *, limit: int = 200) -> int:
        """Expire due rows immediately and reclaim assets with no live refs."""
        expired = self.store.expire_due_attachments(limit=limit)
        for _attachment_id, asset_id in expired:
            if self.store.asset_reference_count(asset_id=asset_id) == 0:
                self._cleanup_asset(asset_id)
            try:
                from src.observability.metrics import record_attachment

                record_attachment("cleanup", status="expired")
            except Exception:
                pass
        return len(expired)

    # -- template bridge support -------------------------------------------

    def read_source_bytes(self, *, attachment_id: str, session_id: int, user_id: int) -> tuple[AttachmentRecord, bytes]:
        record = self.get_attachment(
            attachment_id=attachment_id, session_id=session_id, user_id=user_id
        )
        asset = self.store.get_asset(record.asset_id)
        if asset is None:
            raise AttachmentNotFound("attachment asset not found")
        return record, storage.read_source_bytes(asset.storage_key)

    # -- cleanup ------------------------------------------------------------

    def _cleanup_asset(self, asset_id: str) -> None:
        asset = self.store.get_asset(asset_id)
        if asset is not None:
            storage.delete_storage_key(asset.storage_key)
        try:
            from src.attachments.index import AttachmentIndex

            AttachmentIndex().delete_asset(asset_id)
        except Exception:
            pass
        self.store.delete_asset(asset_id)

    def cleanup_session_storage(self, session_id: int) -> bool:
        """Durable resource removal for a deleted session (design §18.2)."""
        storage.delete_session_storage(session_id)
        return True


def os_rel(path: str) -> str:
    """Convert an absolute storage path to its root-relative storage key."""
    import os

    root = src.settings.CHAT_ATTACHMENT_STORAGE_DIR
    absolute = os.path.abspath(path)
    root_abs = os.path.abspath(root)
    if absolute.startswith(root_abs + os.sep):
        return absolute[len(root_abs) + 1:]
    # Staging paths live under the root already; fall back to basename-safe key.
    return os.path.basename(absolute)
