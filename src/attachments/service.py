"""Attachment lifecycle operations with session-scope ACL (design §5/§14).

Every public method re-verifies ``user_id + session_id (+ attachment_id)``
against the stored row. Upload enforces extension/size/quota limits, streams
to private storage, dedupes assets per (tenant, user, session, sha256), and
enqueues a parse job. Deletion is soft; asset cleanup follows the last
reference. The service never widens scope and never exposes local paths.
"""

from __future__ import annotations

from typing import Any

import src.settings
from src.attachments import storage
from src.attachments.models import (
    AttachmentEditProposal,
    AttachmentNotFound,
    AttachmentQuotaExceeded,
    AttachmentRecord,
    AttachmentRef,
    AttachmentRevision,
    AttachmentUnsupportedType,
    PARSER_VERSION,
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
    ".drawio": "application/vnd.jgraph.mxfile",
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


_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _validate_file_signature(path: str, extension: str) -> None:
    """Check cheap magic bytes before an upload becomes a durable asset."""

    import os

    with open(path, "rb") as stream:
        prefix = stream.read(16)
    if extension == ".pdf" and not prefix.startswith(b"%PDF-"):
        raise AttachmentUnsupportedType("PDF signature is invalid")
    if extension in {".docx", ".xlsx", ".xlsm"}:
        import zipfile

        if zipfile.is_zipfile(path):
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        from src.core.logger import warn

        warn(
            "attachment signature check failed: "
            f"ext={extension} size={size} prefix={prefix[:8].hex()}"
        )
        if prefix.startswith(_OLE2_MAGIC):
            # OLE2 covers legacy .doc/.xls renamed to .docx/.xlsx and
            # password-protected Office files.
            raise AttachmentUnsupportedType(
                "该文件是 OLE2 复合文档（旧版 .doc/.xls，或加密/受保护的 Office 文件），"
                "不是 OOXML 包；请在 Word/Excel 中另存为 .docx/.xlsx（或先解除密码保护）后上传。"
            )
        raise AttachmentUnsupportedType(
            "Office 文件不是有效的 ZIP/OOXML 包：文件可能损坏、上传被中途截断，"
            "或扩展名与实际格式不符；请重新导出/下载该文件后再试（可用 unzip -t 校验）。"
        )
    if extension in {".edf", ".edif"}:
        with open(path, "rb") as stream:
            sample = stream.read(min(4096, os.path.getsize(path)))
        try:
            text = sample.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n")
        except Exception as exc:
            raise AttachmentUnsupportedType("EDIF signature is invalid") from exc
        if not text.lower().startswith("(edif"):
            raise AttachmentUnsupportedType("EDIF signature is invalid")


def _brief(value: Any, limit: int = 40) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def revision_change_summary(revision) -> str:
    """Human-readable diff summary for one attachment revision.

    Derived only from the stored audit fields (operation + before/after +
    structured removal/insertion counts), so the same text is shown on the
    candidate card, in revision history and to the release reviewer.
    """

    details = dict(getattr(revision, "details", {}) or {})
    operation = str(getattr(revision, "operation", "") or "")
    if operation == "replace_section":
        heading = ""
        heading_path = details.get("heading_path")
        if isinstance(heading_path, list) and heading_path:
            heading = str(heading_path[-1] or "")
        parts = [f"替换章节「{heading}」"] if heading else ["替换整节正文"]
        removed: list[str] = []
        for key, label in (
            ("removed_paragraphs", "段"),
            ("removed_tables", "个表格"),
            ("removed_drawings", "个嵌入对象"),
        ):
            try:
                count = int(details.get(key) or 0)
            except (TypeError, ValueError):
                count = 0
            if count:
                removed.append(f"{count} {label}")
        try:
            inserted = int(details.get("inserted_paragraphs") or 0)
        except (TypeError, ValueError):
            inserted = 0
        if removed:
            parts.append("移除 " + "、".join(removed))
        preserved: list[str] = []
        for key, label in (
            ("preserved_tables", "个表格"),
            ("preserved_drawings", "个嵌入对象"),
        ):
            try:
                count = int(details.get(key) or 0)
            except (TypeError, ValueError):
                count = 0
            if count:
                preserved.append(f"{count} {label}")
        if preserved:
            parts.append("保留 " + "、".join(preserved) + "（移至节末）")
        if inserted:
            parts.append(f"新增 {inserted} 段")
        return "；".join(parts)
    if operation == "append_paragraphs":
        heading = ""
        heading_path = details.get("heading_path")
        if isinstance(heading_path, list) and heading_path:
            heading = str(heading_path[-1] or "")
        try:
            inserted = int(details.get("inserted_paragraphs") or 0)
        except (TypeError, ValueError):
            inserted = 0
        where = f"章节「{heading}」末尾" if heading else "文档末尾"
        return f"在{where}追加 {inserted} 段"
    if operation == "annotate_text":
        page = details.get("page")
        return (
            f"在 PDF 第 {page or '?'} 页批注："
            f"将「{_brief(getattr(revision, 'before', ''))}」"
            f"替换为「{_brief(getattr(revision, 'after', ''))}」（原件内容未修改）"
        )
    if operation == "reflow_to_docx":
        return "将 PDF 重排为可编辑 DOCX 副本（版式可能变化，需人工复核）"
    if operation == "replace_cell_text":
        target = f"{details.get('sheet') or '?'}!{details.get('cell') or '?'}"
        return (
            f"在 {target} 将「{_brief(getattr(revision, 'before', ''))}」"
            f"替换为「{_brief(getattr(revision, 'after', ''))}」"
        )
    if operation == "set_cell":
        target = f"{details.get('sheet') or '?'}!{details.get('cell') or '?'}"
        return f"设置 {target} 为「{_brief(getattr(revision, 'after', ''))}」"
    if operation == "insert_rows":
        filled = int(details.get("filled_rows") or 0)
        suffix = f"（写入 {filled} 行数据）" if filled else ""
        return (
            f"在 {details.get('sheet') or '?'} 第 {details.get('after_row')} 行后"
            f"插入 {details.get('count') or 1} 行{suffix}"
        )
    if operation == "delete_rows":
        first = int(details.get("row") or 0)
        count = int(details.get("count") or 1)
        return f"删除 {details.get('sheet') or '?'} 第 {first}–{first + count - 1} 行"
    if operation == "replace_text":
        return (
            f"将「{_brief(getattr(revision, 'before', ''))}」"
            f"替换为「{_brief(getattr(revision, 'after', ''))}」"
        )
    return f"执行 {operation or '未知操作'}"


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
        if filename.startswith("~$"):
            # Word/Excel owner (lock) files share the document name and are
            # tiny binary blobs, not OOXML packages; picking one in a file
            # dialog is a common mistake.
            raise AttachmentUnsupportedType(
                "该文件是 Office 临时锁文件（~$ 前缀，仅文档打开时存在），不是真实文档；"
                "请上传不带 ~$ 的原始文件。"
            )
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

    # -- parser backfill ----------------------------------------------------

    def list_stale_assets(
        self,
        *,
        extensions: list[str] | None = None,
        limit: int = 200,
    ):
        """Assets parsed with an older parser generation than the current one."""

        return self.store.list_stale_assets(
            current_version_prefix=PARSER_VERSION,
            extensions=extensions,
            limit=limit,
        )

    def reparse_asset(self, asset) -> bool:
        """Enqueue one stale asset for re-parse; idempotent while a job is live."""

        _job, created = self.store.enqueue_parse_job(
            asset_id=asset.asset_id,
            session_id=asset.session_id,
            user_id=asset.user_id,
            tenant_id=asset.tenant_id,
            parser_version=f"{PARSER_VERSION}:{asset.extension.lstrip('.')}",
        )
        if created:
            self.store.update_asset_status(
                asset.asset_id,
                parse_status=PARSE_STATUS_QUEUED,
                manifest={},
                error_code="",
                error_message="",
            )
        return created

    def reparse_stale_assets(
        self,
        *,
        extensions: list[str] | None = None,
        limit: int = 200,
    ) -> dict:
        """Backfill derived parse data (for example DOCX heading paths)."""

        assets = self.list_stale_assets(extensions=extensions, limit=limit)
        queued_ids: list[str] = []
        for asset in assets:
            if self.reparse_asset(asset):
                queued_ids.append(asset.asset_id)
        return {
            "scanned": len(assets),
            "queued": len(queued_ids),
            "asset_ids": [asset.asset_id for asset in assets],
            "queued_asset_ids": queued_ids,
        }

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

    def create_attachment_revision(
        self,
        *,
        parent_attachment_id: str,
        session_id: int,
        user_id: int,
        before: str = "",
        after: str = "",
        client_request_id: str | None = None,
        operation: str = "replace_text",
        heading_path: list[str] | None = None,
        sheet: str = "",
        cell: str = "",
        after_row: int = 0,
        row: int = 0,
        count: int = 1,
        values: list[list[str]] | None = None,
        preserve_embedded: bool = True,
    ):
        """Create a candidate child from one exact attachment edit.

        Two operations always produce an immutable child while the source
        attachment stays untouched:

        - ``replace_text`` (default): one unique ``before`` occurrence is
          replaced by ``after``.  Supported for UTF-8 TXT/MD and DOCX
          (paragraphs and table cells, run formatting preserved).
        - ``replace_section``: the body under the ``heading_path`` heading is
          replaced by ``after`` (one paragraph per line).  DOCX only; the
          heading itself and every other section are preserved.  New
          paragraphs inherit the original body style and run formatting;
          tables and embedded objects are kept (moved to the end of the new
          body, or placed where an ``[[embed]]``/``[[embed:N]]`` line asks)
          unless ``preserve_embedded=False``.
        - ``append_paragraphs``: ``after`` paragraphs are appended at the end
          of the ``heading_path`` section, or of the document when no heading
          is given.  DOCX only; nothing else changes.

        Missing or ambiguous targets fail closed so no candidate is created
        from a guess.  A caller asking for any other Office/PDF edit receives
        an explicit unsupported error.
        """
        if not src.settings.CHAT_ATTACHMENTS_ENABLED:
            raise AttachmentUnsupportedType("chat attachments are disabled")
        normalized_operation = str(operation or "replace_text").strip().lower()
        if normalized_operation not in {
            "replace_text",
            "replace_section",
            "append_paragraphs",
            "set_cell",
            "replace_cell_text",
            "annotate_text",
            "reflow_to_docx",
            "insert_rows",
            "delete_rows",
        }:
            raise ValueError(
                f"unsupported attachment revision operation: {operation}"
            )
        before_text = str(before or "").strip()
        after_text = str(after or "").strip()
        heading_ref = [
            str(item).strip() for item in (heading_path or []) if str(item).strip()
        ]
        sheet_ref = str(sheet or "").strip()
        cell_ref = str(cell or "").strip().upper()
        if not after_text and normalized_operation not in {
            "insert_rows",
            "delete_rows",
            "reflow_to_docx",
        }:
            raise ValueError("attachment revision requires non-empty after content")
        if normalized_operation in {"replace_text", "replace_cell_text", "annotate_text"}:
            if not before_text:
                raise ValueError(
                    "text replacement requires non-empty before and after values"
                )
            if before_text == after_text:
                raise ValueError("text replacement must change the value")
        elif normalized_operation == "replace_section":
            if not heading_ref:
                raise ValueError("a section replacement requires a heading reference")
        elif normalized_operation == "set_cell" and (not sheet_ref or not cell_ref):
            raise ValueError("a cell assignment requires both sheet and cell")
        elif normalized_operation == "insert_rows":
            if not sheet_ref:
                raise ValueError("a row insertion requires a sheet")
            if int(after_row) < 0:
                raise ValueError("after_row must be >= 0")
        elif normalized_operation == "delete_rows":
            if not sheet_ref:
                raise ValueError("a row deletion requires a sheet")
            if int(row) < 1:
                raise ValueError("row must be >= 1")

        parent = self.get_attachment(
            attachment_id=str(parent_attachment_id or "").strip(),
            session_id=int(session_id),
            user_id=int(user_id),
        )
        if parent.extension not in {
            ".txt",
            ".md",
            ".docx",
            ".xlsx",
            ".xlsm",
            ".pdf",
            ".edf",
            ".edif",
        }:
            raise AttachmentUnsupportedType(
                "direct attachment revision supports .txt/.md/.docx text or "
                "section edits, .xlsx/.xlsm cell edits, .pdf annotations and "
                ".edf/.edif validated text edits; use a governed document "
                "revision for other formats"
            )
        if normalized_operation in {"replace_section", "append_paragraphs"} and parent.extension != ".docx":
            raise AttachmentUnsupportedType(
                "section replacement and paragraph append are available for "
                ".docx attachments only"
            )
        if normalized_operation in {
            "set_cell",
            "replace_cell_text",
            "insert_rows",
            "delete_rows",
        } and parent.extension not in {
            ".xlsx",
            ".xlsm",
        }:
            raise AttachmentUnsupportedType(
                "cell/row edits are available for .xlsx/.xlsm attachments only"
            )
        if normalized_operation in {"annotate_text", "reflow_to_docx"} and parent.extension != ".pdf":
            raise AttachmentUnsupportedType(
                f"{normalized_operation} is available for .pdf attachments only"
            )
        if normalized_operation == "replace_text" and parent.extension == ".pdf":
            raise AttachmentUnsupportedType(
                "PDF 不支持原位文本替换；请使用 annotate_text 生成批注候选"
            )

        import hashlib
        import json

        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "parent_attachment_id": parent.attachment_id,
                    "parent_content_hash": parent.sha256,
                    "operation": normalized_operation,
                    "before": before_text,
                    "after": after_text,
                    "heading_path": heading_ref,
                    "sheet": sheet_ref,
                    "cell": cell_ref,
                    "after_row": int(after_row or 0),
                    "row": int(row or 0),
                    "count": int(count or 1),
                    "values": [
                        [str(value) for value in item] for item in (values or [])
                    ],
                    "client_request_id": str(client_request_id or "").strip(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        idempotency_key = f"attachment-revision:{request_fingerprint}"
        existing = self.store.get_attachment_revision_by_idempotency(
            idempotency_key,
            session_id=int(session_id),
            user_id=int(user_id),
        )
        if existing is not None:
            return existing

        _record, source_bytes = self.read_source_bytes(
            attachment_id=parent.attachment_id,
            session_id=int(session_id),
            user_id=int(user_id),
        )
        edit_details: dict = {}
        if parent.extension in {".txt", ".md", ".edf", ".edif"}:
            try:
                source_text = source_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("attachment is not valid UTF-8 text") from exc
            occurrences = source_text.count(before_text)
            if occurrences != 1:
                raise ValueError(
                    f"replacement target must occur exactly once (found {occurrences})"
                )
            child_bytes = source_text.replace(before_text, after_text, 1).encode("utf-8")
            if parent.extension in {".edf", ".edif"}:
                from src.attachments.edf_revision import (
                    EdfRevisionError,
                    validate_edf_replacement,
                )

                try:
                    validate_edf_replacement(source_bytes, child_bytes)
                except EdfRevisionError as exc:
                    raise ValueError(str(exc)) from exc
            edit_details = {
                "operation": "replace_text",
                "format": parent.extension.lstrip("."),
                "occurrences": 1,
            }
        elif parent.extension == ".pdf":
            if normalized_operation == "reflow_to_docx":
                from src.attachments.pdf_reflow import (
                    PdfReflowUnavailable,
                    reflow_to_docx,
                )

                try:
                    child_bytes, payload = reflow_to_docx(source_bytes)
                except PdfReflowUnavailable as exc:
                    raise ValueError(str(exc)) from exc
                edit_details = {"format": "pdf", **payload}
            else:
                from src.attachments.pdf_revision import PdfRevisionError, annotate_text

                try:
                    child_bytes, payload = annotate_text(
                        source_bytes, before=before_text, after=after_text
                    )
                except PdfRevisionError as exc:
                    raise ValueError(str(exc)) from exc
                edit_details = {"format": "pdf", **payload}
        elif parent.extension in {".xlsx", ".xlsm"}:
            from src.attachments.xlsx_revision import (
                XlsxRevisionError,
                delete_rows,
                insert_rows,
                replace_cell_text,
                set_cell_value,
            )

            try:
                if normalized_operation == "set_cell":
                    child_bytes, payload = set_cell_value(
                        source_bytes, sheet=sheet_ref, cell=cell_ref, value=after_text
                    )
                elif normalized_operation == "insert_rows":
                    child_bytes, payload = insert_rows(
                        source_bytes,
                        sheet=sheet_ref,
                        after_row=int(after_row or 0),
                        count=int(count or 1),
                        values=values,
                    )
                elif normalized_operation == "delete_rows":
                    child_bytes, payload = delete_rows(
                        source_bytes,
                        sheet=sheet_ref,
                        row=int(row or 0),
                        count=int(count or 1),
                    )
                else:
                    child_bytes, payload = replace_cell_text(
                        source_bytes,
                        sheet=sheet_ref,
                        before=before_text,
                        after=after_text,
                    )
            except XlsxRevisionError as exc:
                raise ValueError(str(exc)) from exc
            edit_details = {"format": parent.extension.lstrip("."), **payload}
        else:
            from src.attachments.docx_revision import (
                DocxRevisionError,
                append_paragraphs_in_docx,
                replace_section_in_docx,
                replace_text_in_docx,
            )

            try:
                if normalized_operation == "replace_section":
                    child_bytes, payload = replace_section_in_docx(
                        source_bytes,
                        heading_ref,
                        after_text,
                        preserve_embedded=bool(preserve_embedded),
                    )
                    removed_text = str(payload.pop("removed_text", "") or "")
                    if not before_text:
                        before_text = removed_text[:8000]
                elif normalized_operation == "append_paragraphs":
                    child_bytes, payload = append_paragraphs_in_docx(
                        source_bytes,
                        heading_ref,
                        after_text,
                    )
                else:
                    child_bytes, payload = replace_text_in_docx(
                        source_bytes, before_text, after_text
                    )
            except DocxRevisionError as exc:
                raise ValueError(str(exc)) from exc
            edit_details = {"format": "docx", **payload}
        if child_bytes == source_bytes:
            raise ValueError("attachment revision did not change the file")

        revision_id = f"attachment-revision-{request_fingerprint[:24]}"
        stem = parent.filename.rsplit(".", 1)[0] or "attachment"
        child_extension = str(edit_details.get("child_extension") or parent.extension)
        child_filename = safe_filename(
            f"{stem}.revision-{request_fingerprint[:10]}{child_extension}"
        )
        child = self.upload(
            session_id=int(session_id),
            user_id=int(user_id),
            filename=child_filename,
            stream=__import__("io").BytesIO(child_bytes),
            client_request_id=f"attachment-revision-child:{request_fingerprint[:80]}",
            usage_hint=parent.usage_hint,
            tenant_id=parent.tenant_id,
        )
        revision, _created = self.store.create_attachment_revision(
            revision_id=revision_id,
            parent_attachment_id=parent.attachment_id,
            child_attachment_id=child.attachment_id,
            session_id=int(session_id),
            user_id=int(user_id),
            tenant_id=parent.tenant_id,
            operation=normalized_operation,
            before=before_text,
            after=after_text,
            parent_content_hash=parent.sha256,
            child_content_hash=child.sha256,
            idempotency_key=idempotency_key,
            details=edit_details,
        )
        try:
            from src.observability.metrics import record_attachment

            record_attachment("revision", status="ok")
        except Exception:
            pass
        return revision

    # -- release governance -------------------------------------------------

    def get_attachment_revision(
        self,
        *,
        revision_id: str,
        session_id: int,
        user_id: int | None = None,
    ) -> AttachmentRevision | None:
        """Read one revision; ``user_id=None`` is reserved for reviewers."""

        return self.store.get_attachment_revision(
            str(revision_id or "").strip(),
            session_id=int(session_id),
            user_id=None if user_id is None else int(user_id),
        )

    def get_attachment_revision_by_child(
        self,
        *,
        child_attachment_id: str,
        session_id: int,
        user_id: int,
    ) -> AttachmentRevision | None:
        return self.store.get_attachment_revision_by_child(
            str(child_attachment_id or "").strip(),
            session_id=int(session_id),
            user_id=int(user_id),
        )

    def list_pending_attachment_releases(
        self, *, limit: int = 200
    ) -> list[AttachmentRevision]:
        """Cross-session pending queue for authorized reviewers."""

        return self.store.list_pending_attachment_revisions(limit=limit)

    def request_attachment_release(
        self,
        *,
        revision_id: str,
        session_id: int,
        user_id: int,
        comment: str = "",
    ) -> AttachmentRevision:
        """Record explicit release-review intent for one candidate.

        This never releases anything by itself: the candidate stays immutable
        and ``release_state`` only moves to ``pending`` until an authorized
        reviewer approves or rejects it.  A repeated request is idempotent.
        """

        revision = self.store.get_attachment_revision(
            str(revision_id or "").strip(),
            session_id=int(session_id),
            user_id=int(user_id),
        )
        if revision is None:
            raise AttachmentNotFound("attachment revision not found")
        if revision.status != "review_candidate":
            raise ValueError("only a review candidate may request release")
        if revision.release_state == "approved":
            raise ValueError("the attachment candidate was already released")
        if revision.release_state == "pending":
            return revision
        updated = self.store.request_attachment_revision_release(
            revision_id=revision.revision_id,
            session_id=int(session_id),
            user_id=int(user_id),
            comment=comment,
        )
        if updated is None:
            raise ValueError("the attachment release request could not be recorded")
        try:
            from src.observability.metrics import record_attachment

            record_attachment("release", status="requested")
        except Exception:
            pass
        return updated

    def review_attachment_release(
        self,
        *,
        revision_id: str,
        session_id: int,
        reviewer_id: int,
        approve: bool,
        comment: str = "",
    ) -> AttachmentRevision:
        """Approve or reject a pending release request.

        Approval creates a separate immutable ``.released`` copy owned by the
        original uploader; the candidate itself is never promoted in place, so
        "candidate" and "released" remain strictly separated.  Rejection keeps
        the candidate downloadable and allows a later re-request.
        """

        revision = self.store.get_attachment_revision(
            str(revision_id or "").strip(),
            session_id=int(session_id),
        )
        if revision is None:
            raise AttachmentNotFound("attachment revision not found")
        if revision.release_state != "pending":
            raise ValueError("the attachment release request is not pending")

        released_attachment_id: str | None = None
        if approve:
            child, content = self.read_source_bytes(
                attachment_id=revision.child_attachment_id,
                session_id=int(session_id),
                user_id=int(revision.user_id),
            )
            stem = child.filename.rsplit(".", 1)[0] or "attachment"
            released = self.upload(
                session_id=int(session_id),
                user_id=int(revision.user_id),
                filename=safe_filename(f"{stem}.released{child.extension}"),
                stream=__import__("io").BytesIO(content),
                client_request_id=f"attachment-release:{revision.revision_id}",
                usage_hint=child.usage_hint,
                tenant_id=revision.tenant_id,
            )
            released_attachment_id = released.attachment_id

        updated = self.store.review_attachment_revision_release(
            revision_id=revision.revision_id,
            session_id=int(session_id),
            approve=bool(approve),
            reviewer_id=int(reviewer_id),
            comment=comment,
            released_attachment_id=released_attachment_id,
        )
        if updated is None:
            raise ValueError("the attachment release request was already reviewed")
        try:
            from src.observability.metrics import record_attachment

            record_attachment(
                "release", status="approved" if approve else "rejected"
            )
        except Exception:
            pass
        try:
            from src.core.notifications import NotificationService

            recipient = int(
                revision.release_requested_by or revision.user_id or 0
            )
            NotificationService().create(
                user_id=recipient,
                kind=(
                    "attachment_release_approved"
                    if approve
                    else "attachment_release_rejected"
                ),
                target_type="attachment_revision",
                target_id=revision.revision_id,
                title=(
                    "附件候选已通过正式发布"
                    if approve
                    else "附件候选发布申请被驳回"
                ),
                body=(
                    "审核通过，已生成独立的正式文件，可从会话附件中下载。"
                    if approve
                    else (
                        "驳回原因："
                        + (str(updated.review_comment or "").strip() or "未填写")
                        + "；候选仍可下载修改后重新申请。"
                    )
                ),
                payload={
                    "session_id": revision.session_id,
                    "revision_id": revision.revision_id,
                    "child_attachment_id": revision.child_attachment_id,
                    "released_attachment_id": updated.released_attachment_id,
                    "review_comment": updated.review_comment,
                },
            )
        except Exception:
            pass
        return updated

    # -- governed edit proposals -------------------------------------------

    def create_attachment_edit_proposal(
        self,
        *,
        session_id: int,
        user_id: int,
        parent_attachment_id: str,
        operation: str = "replace_section",
        heading_path: list[str] | None = None,
        after: str = "",
        before: str = "",
        evidence_refs: list[str] | None = None,
        note: str = "",
    ) -> AttachmentEditProposal:
        """Store a user-confirmable edit proposal; never writes the file."""

        normalized_operation = str(operation or "replace_section").strip().lower()
        if normalized_operation != "replace_section":
            raise ValueError(
                "governed attachment proposals currently support replace_section only"
            )
        heading_ref = [
            str(item).strip() for item in (heading_path or []) if str(item).strip()
        ]
        after_text = str(after or "").strip()
        if not heading_ref:
            raise ValueError("a section proposal requires a heading reference")
        if not after_text:
            raise ValueError("a section proposal requires non-empty content")
        if len(after_text) > 200_000:
            raise ValueError("section proposal content exceeds the 200k character limit")

        parent = self.get_attachment(
            attachment_id=str(parent_attachment_id or "").strip(),
            session_id=int(session_id),
            user_id=int(user_id),
        )
        if parent.extension != ".docx":
            raise AttachmentUnsupportedType(
                "governed section proposals are available for .docx attachments only"
            )

        pending = self.store.get_pending_attachment_edit_proposal(
            session_id=int(session_id), user_id=int(user_id)
        )
        if (
            pending is not None
            and pending.parent_attachment_id == parent.attachment_id
            and pending.heading_path == heading_ref
            and pending.after_text == after_text
        ):
            return pending

        import uuid

        proposal = self.store.create_attachment_edit_proposal(
            proposal_id=f"attachment-proposal-{uuid.uuid4().hex}",
            session_id=int(session_id),
            user_id=int(user_id),
            tenant_id=parent.tenant_id,
            parent_attachment_id=parent.attachment_id,
            operation=normalized_operation,
            heading_path=heading_ref,
            after_text=after_text,
            before_text=str(before or ""),
            evidence_refs=list(evidence_refs or []),
            note=note,
        )
        try:
            from src.observability.metrics import record_attachment

            record_attachment("proposal", status="created")
        except Exception:
            pass
        return proposal

    def get_pending_attachment_proposal(
        self, *, session_id: int, user_id: int
    ) -> AttachmentEditProposal | None:
        return self.store.get_pending_attachment_edit_proposal(
            session_id=int(session_id), user_id=int(user_id)
        )

    def confirm_attachment_proposal(
        self, *, proposal_id: str, session_id: int, user_id: int
    ) -> AttachmentRevision:
        """Execute a pending proposal as an immutable candidate."""

        proposal = self.store.get_attachment_edit_proposal(
            proposal_id,
            session_id=int(session_id),
            user_id=int(user_id),
        )
        if proposal is None:
            raise AttachmentNotFound("attachment edit proposal not found")
        if proposal.status != "pending":
            raise ValueError("the attachment edit proposal is not pending")
        revision = self.create_attachment_revision(
            parent_attachment_id=proposal.parent_attachment_id,
            session_id=proposal.session_id,
            user_id=proposal.user_id,
            operation=proposal.operation,
            heading_path=proposal.heading_path,
            after=proposal.after_text,
            client_request_id=f"attachment-proposal:{proposal.proposal_id}",
        )
        self.store.decide_attachment_edit_proposal(
            proposal_id=proposal.proposal_id,
            session_id=proposal.session_id,
            user_id=proposal.user_id,
            status="confirmed",
            confirmed_revision_id=revision.revision_id,
        )
        return revision

    def reject_attachment_proposal(
        self, *, proposal_id: str, session_id: int, user_id: int, note: str = ""
    ) -> AttachmentEditProposal:
        proposal = self.store.decide_attachment_edit_proposal(
            proposal_id=proposal_id,
            session_id=int(session_id),
            user_id=int(user_id),
            status="rejected",
            note=note,
        )
        if proposal is None:
            raise ValueError("the attachment edit proposal is not pending")
        return proposal

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
