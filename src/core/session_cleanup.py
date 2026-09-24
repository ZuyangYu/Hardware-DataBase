"""Session-scoped attachment cleanup (design §18.2 / §21.1).

Consumes ``session_cleanup_outbox`` entries enqueued by
``ConversationService.delete_session``. Every handler is idempotent: rows are
already gone with the session (FK cascade), so cleanup only removes
attachment-domain state (records, assets, parts, index rows, storage) and is
safe to re-run after partial failure.
"""

from __future__ import annotations

import json

from src.attachments import storage
from src.attachments.store import AttachmentStore
from src.core.logger import error
from src.observability.metrics import record_attachment


class SessionCleanupCoordinator:
    def __init__(
        self,
        store: AttachmentStore | None = None,
        worker_id: str = "cleanup-worker",
        result_store=None,
        document_job_store=None,
        document_store=None,
    ):
        self.store = store or AttachmentStore()
        self.worker_id = worker_id
        self.result_store = result_store
        self.document_job_store = document_job_store
        self.document_store = document_store

    def run_once(self, limit: int = 4) -> bool:
        entries = self.store.list_pending_cleanups(limit=limit)
        did_work = False
        for entry in entries:
            claimed = self.store.claim_cleanup(entry["outbox_id"], self.worker_id)
            if claimed is None:
                continue
            did_work = True
            try:
                try:
                    targets = json.loads(claimed.get("targets_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    targets = {}
                self._cleanup_session(
                    int(claimed["session_id"]),
                    user_id=int(claimed.get("user_id") or 0),
                    require_session_absent=bool(targets.get("require_session_absent")),
                )
                self.store.complete_cleanup(claimed["outbox_id"], self.worker_id)
                record_attachment("cleanup", status="ok")
            except Exception as exc:  # noqa: BLE001 - cleanup must retry, not crash
                self.store.retry_cleanup(claimed["outbox_id"], self.worker_id, str(exc))
                record_attachment("cleanup_retry", status="retrying")
                error(f"Session cleanup {claimed['outbox_id']} failed: {exc}")
        return did_work

    def _cleanup_session(
        self,
        session_id: int,
        *,
        user_id: int | None = None,
        require_session_absent: bool = False,
    ) -> None:
        # The outbox is reserved before the conversation delete commits. If
        # that delete later rolls back, do not purge a still-live session's
        # attachments; leave the row retryable until the session disappears.
        if require_session_absent and user_id:
            from src.core.conversation import ConversationService

            if ConversationService().get_session(user_id, session_id) is not None:
                raise RuntimeError("session deletion not committed")
        from src.agents.runner import forget_thread

        if not forget_thread(str(session_id)):
            raise RuntimeError("chat agent checkpoint cleanup failed")
        # 1. Cancel any queued parse jobs (rows may already be gone).
        self.store.cancel_session_jobs(session_id=session_id)
        # 2. Collect assets belonging to the session and purge each one.
        asset_ids: list[str] = []
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT asset_id, storage_key FROM chat_attachment_assets WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            asset_ids = [row["asset_id"] for row in rows]
            for asset_id in asset_ids:
                conn.execute("DELETE FROM chat_attachment_parts WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_embeddings WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_visual_cache WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_jobs WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_assets WHERE asset_id = ?", (asset_id,))
            conn.execute(
                "DELETE FROM chat_attachments WHERE session_id = ?", (session_id,)
            )
        # 3. Remove FTS rows and physical storage.
        try:
            from src.attachments.index import AttachmentIndex

            index = AttachmentIndex()
            for asset_id in asset_ids:
                index.delete_asset(asset_id)
        except Exception:
            pass
        storage.delete_session_storage(session_id)
        result_store = self.result_store
        if result_store is None:
            from src.result_exports.store import ResultExportStore

            result_store = ResultExportStore()
            self.result_store = result_store
        result_store.cleanup_session(session_id=session_id)

        document_job_store = self.document_job_store
        if document_job_store is None:
            from src.document_authoring.job_store import DocumentAuthoringJobStore

            document_job_store = DocumentAuthoringJobStore()
            self.document_job_store = document_job_store
        cancel_session = getattr(document_job_store, "cancel_session", None)
        work_order_ids = (
            list(cancel_session(session_id=session_id, reason="chat session deleted"))
            if callable(cancel_session)
            else []
        )
        document_store = self.document_store
        if document_store is None:
            from src.document_authoring.work_order_store import DocumentAuthoringStore

            document_store = DocumentAuthoringStore()
            self.document_store = document_store
        if work_order_ids:
            document_store.cleanup_work_orders(work_order_ids)
        cleanup_jobs = getattr(document_job_store, "cleanup_session", None)
        if callable(cleanup_jobs):
            cleanup_jobs(session_id=session_id)
