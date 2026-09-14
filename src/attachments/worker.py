"""Attachment worker loop (lease-claimed parse jobs, design §12.2).

Runs in the shared HardwareWorker process (and standalone via ``main``).
Chat turn workers never poll attachment state: the coordinator resolves
``waiting_for_attachments`` turns when assets settle.
"""

from __future__ import annotations

import inspect
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import Event, Thread

import src.settings
from src.attachments.coordinator import AttachmentTurnCoordinator
from src.attachments.jobs import AttachmentParseExecutor
from src.attachments.store import AttachmentStore
from src.core.logger import error, log
from src.observability.metrics import record_attachment


class AttachmentWorker:
    def __init__(
        self,
        store: AttachmentStore | None = None,
        executor: AttachmentParseExecutor | None = None,
        coordinator: AttachmentTurnCoordinator | None = None,
        worker_id: str = "attachment-worker",
        parse_timeout_seconds: float | None = None,
        lease_seconds: int | None = None,
    ):
        self.running = True
        self.worker_id = worker_id
        self.store = store or AttachmentStore()
        self.executor = executor or AttachmentParseExecutor(store=self.store)
        self.coordinator = coordinator or AttachmentTurnCoordinator(store=self.store)
        self.parse_timeout_seconds = parse_timeout_seconds
        self.lease_seconds = lease_seconds

    def stop(self, *_args) -> None:
        self.running = False

    def run_once(self) -> bool:
        did_work = False
        # Retention is checked before claiming parse work.  Reads also enforce
        # the deadline synchronously, while this sweep reclaims the physical
        # asset, index rows and caches once the last live reference expires.
        try:
            from src.attachments.service import AttachmentService

            if AttachmentService(store=self.store).expire_due_attachments() > 0:
                did_work = True
        except Exception as exc:  # noqa: BLE001 - retention must not stop parsing
            error(f"Attachment retention sweep failed: {exc}")
        # Session cleanups first: reclaim resources before accepting new work.
        try:
            from src.core.session_cleanup import SessionCleanupCoordinator

            cleanup = SessionCleanupCoordinator(store=self.store, worker_id=self.worker_id)
            if cleanup.run_once(limit=2):
                did_work = True
        except Exception as exc:  # noqa: BLE001
            error(f"Attachment session cleanup failed: {exc}")
        try:
            recovered = self.coordinator.reconcile_waiting_turns()
            if recovered:
                did_work = True
        except Exception as exc:  # noqa: BLE001 - recovery must not stop parsing
            error(f"Attachment waiting-turn reconciliation failed: {exc}")
        pending = self.store.list_pending_jobs(limit=4)
        for job in pending:
            claim_kwargs = {}
            if self.lease_seconds is not None:
                claim_kwargs["lease_seconds"] = self.lease_seconds
            claimed = self.store.claim_job(job.job_id, self.worker_id, **claim_kwargs)
            if claimed is None:
                continue
            did_work = True
            started = time.monotonic()
            try:
                result = self._process_with_watchdog(claimed)
                if result.get("ok"):
                    self.store.complete_job(claimed.job_id, self.worker_id, result)
                    manifest = result.get("manifest") or {}
                    ocr_pages = manifest.get("ocr_attempted_pages")
                    if isinstance(ocr_pages, list) and ocr_pages:
                        failed_pages = manifest.get("ocr_failed_pages") or []
                        record_attachment(
                            "ocr",
                            status="degraded" if failed_pages else "ok",
                            duration_s=time.monotonic() - started,
                        )
                    record_attachment(
                        "parse",
                        status=str(result.get("parse_status") or "ready"),
                        duration_s=time.monotonic() - started,
                    )
                else:
                    self.store.fail_job(
                        claimed.job_id,
                        self.worker_id,
                        str(result.get("error_code") or "parse_failed"),
                        str(result.get("error_message") or "parse failed"),
                    )
                    record_attachment("parse", status="failed", duration_s=time.monotonic() - started)
                self.coordinator.notify_asset_settled(claimed.asset_id)
            except Exception as exc:  # noqa: BLE001 - worker must survive per-job faults
                self.store.fail_job(
                    claimed.job_id, self.worker_id, "worker_error", str(exc)[:1000]
                )
                record_attachment("parse", status="failed", duration_s=time.monotonic() - started)
                error(f"Attachment parse job {claimed.job_id} failed: {exc}")
        return did_work

    def _process_with_watchdog(self, claimed) -> dict:
        """Run a parser with a deadline while keeping its job lease alive."""
        timeout = max(
            0.01,
            float(
                self.parse_timeout_seconds
                if self.parse_timeout_seconds is not None
                else src.settings.CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS
            ),
        )
        lease_seconds = max(
            15,
            int(
                self.lease_seconds
                if self.lease_seconds is not None
                else src.settings.CHAT_ATTACHMENT_JOB_LEASE_SECONDS
            ),
        )
        deadline = time.monotonic() + timeout
        # Refresh immediately, then periodically. The first refresh also
        # makes the lease contract observable for very short test parses.
        try:
            self.store.heartbeat_job(claimed.job_id, self.worker_id, lease_seconds)
        except Exception as exc:  # noqa: BLE001 - parsing can continue safely
            error(f"Attachment job {claimed.job_id} heartbeat failed: {exc}")

        stop_heartbeat = Event()
        interval = max(0.05, min(float(lease_seconds) / 3.0, timeout / 3.0))

        def heartbeat_loop() -> None:
            while not stop_heartbeat.wait(interval):
                try:
                    self.store.heartbeat_job(claimed.job_id, self.worker_id, lease_seconds)
                except Exception as exc:  # noqa: BLE001 - best effort lease refresh
                    error(f"Attachment job {claimed.job_id} heartbeat failed: {exc}")

        heartbeat_thread = Thread(
            target=heartbeat_loop,
            name=f"attachment-heartbeat-{claimed.job_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attachment-parse")
        future: Future = pool.submit(self._call_executor, claimed, deadline)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            return {
                "ok": False,
                "parse_status": "failed",
                "error_code": "parse_timeout",
                "error_message": f"attachment parse timed out after {timeout:g} seconds",
            }
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=min(0.2, interval + 0.05))
            # A timed-out parser may still be unwinding. It receives the
            # deadline and must not persist results after expiry; do not block
            # the worker on its cleanup. Normal parses are joined completely.
            pool.shutdown(wait=False, cancel_futures=True)

    def _call_executor(self, claimed, deadline: float) -> dict:
        process_asset = self.executor.process_asset
        kwargs = {"parser_version": claimed.parser_version or None}
        try:
            parameters = inspect.signature(process_asset).parameters.values()
            supports_deadline = any(
                parameter.name == "deadline"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_deadline = False
        if supports_deadline:
            kwargs["deadline"] = deadline
        return process_asset(claimed.asset_id, **kwargs)

    def run_forever(self) -> None:
        log("Attachment worker started")
        while self.running:
            if not self.run_once():
                time.sleep(max(0.1, float(src.settings.WORKER_POLL_INTERVAL_SECONDS)))
        log("Attachment worker stopped")


def main() -> None:
    worker = AttachmentWorker()
    import signal

    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
