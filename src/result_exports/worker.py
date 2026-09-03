"""Standalone export worker; rendering never depends on a browser session."""

from __future__ import annotations

from dataclasses import replace
import re
import signal
import threading
import time
import uuid
from datetime import datetime, timezone

import src.settings
from src.observability import metrics as observability_metrics
from src.result_exports.renderers import render_result
from src.result_exports.store import ResultExportStore


def _filename(title: str, snapshot_id: str, extension: str) -> str:
    name = re.sub(r"[^\w\-\u4e00-\u9fff ]+", "-", str(title or "导出结果"), flags=re.UNICODE).strip(" -")
    name = name[:80] or "导出结果"
    return f"{name}-{snapshot_id[-8:]}.{extension}"


def _render_envelope(snapshot, job):
    """Apply per-job presentation options without changing the snapshot."""
    envelope = snapshot.envelope
    title = job.options.get("render_title")
    if title is not None:
        normalized_title = str(title).strip()[:160] or "导出结果"
        envelope = replace(envelope, title=normalized_title)
    if job.options.get("include_citations") is False:
        envelope = replace(envelope, citations=[])
    return envelope


def _age_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _safe_metric(callback, **kwargs) -> None:
    """Keep telemetry fail-open even when a test/exporter replaces a hook."""

    try:
        callback(**kwargs)
    except Exception:
        pass


class ResultExportWorker:
    def __init__(self, store: ResultExportStore | None = None, worker_id: str | None = None):
        self.store = store or ResultExportStore()
        self.worker_id = worker_id or f"export-worker-{uuid.uuid4().hex}"
        self.running = True
        self._next_retention_cleanup = 0.0

    def _cleanup_expired_artifacts(self) -> None:
        now = time.monotonic()
        if now < self._next_retention_cleanup:
            return
        self._next_retention_cleanup = now + 60.0
        try:
            removed = self.store.cleanup_expired(limit=100)
            if removed:
                from src.core.app_logs import AppLogService

                audit = AppLogService(self.store.db_path)
                for artifact in removed:
                    audit.record_audit(
                        action="cleanup_export_artifact",
                        target_type="export_artifact",
                        target_id=artifact.artifact_id,
                        metadata={
                            "format": artifact.format,
                            "size": artifact.size,
                            "sha256": artifact.sha256,
                        },
                    )
        except Exception:
            # A retention sweep must never prevent queued jobs from running.
            pass

    def _record_queue_state(self) -> None:
        try:
            depth, oldest_age_s = self.store.queue_state()
            _safe_metric(
                observability_metrics.set_export_queue_state,
                queue="result-export",
                depth=depth,
                oldest_age_s=oldest_age_s,
            )
        except Exception:
            pass

    def stop(self, *_args) -> None:
        self.running = False

    def _start_lease_heartbeat(self, job, lease_seconds: int):
        """Renew a claimed job while a renderer is doing expensive work."""

        stop = threading.Event()
        interval = max(1.0, min(30.0, float(lease_seconds) / 3.0))

        def loop() -> None:
            while not stop.wait(interval):
                try:
                    self.store.heartbeat(
                        job.export_job_id,
                        self.worker_id,
                        job.lease_token,
                        lease_seconds=lease_seconds,
                    )
                except Exception:
                    # publish_artifact performs the authoritative lease check;
                    # a lost lease will therefore fail closed.
                    return

        # Renew once before rendering so even a renderer that runs just longer
        # than the claim boundary has a fresh lease. The thread handles longer
        # renders without making the renderers aware of queue mechanics.
        self.store.heartbeat(
            job.export_job_id,
            self.worker_id,
            job.lease_token,
            lease_seconds=lease_seconds,
        )
        thread = threading.Thread(
            target=loop,
            name=f"export-lease-{job.export_job_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return stop, thread

    def run_once(self, limit: int = 8) -> bool:
        self._cleanup_expired_artifacts()
        self._record_queue_state()
        did_work = False
        for candidate in self.store.list_pending(limit=limit):
            job = self.store.claim(
                candidate.export_job_id,
                self.worker_id,
                lease_seconds=max(15, int(getattr(src.settings, "RESULT_EXPORT_JOB_LEASE_SECONDS", 300))),
            )
            if job is None:
                continue
            did_work = True
            started_at = time.monotonic()
            queue_s = _age_seconds(candidate.created_at)
            lease_seconds = max(15, int(getattr(src.settings, "RESULT_EXPORT_JOB_LEASE_SECONDS", 300)))
            heartbeat_stop = None
            heartbeat_thread = None
            render_started_at = None
            render_recorded = False
            try:
                heartbeat_stop, heartbeat_thread = self._start_lease_heartbeat(job, lease_seconds)
                snapshot = self.store.get_snapshot(job.owner_user_id, job.snapshot_id)
                if snapshot is None:
                    raise KeyError("result snapshot not found")
                render_envelope = _render_envelope(snapshot, job)
                render_started_at = time.monotonic()
                rendered = render_result(
                    render_envelope,
                    job.format,
                    content_shape=job.content_shape,
                    render_options=job.options,
                )
                _safe_metric(
                    observability_metrics.record_export_render,
                    format=job.format,
                    status="succeeded",
                    duration_s=time.monotonic() - render_started_at,
                )
                render_recorded = True
                self.store.publish_artifact(
                    job_id=job.export_job_id,
                    worker_id=self.worker_id,
                    lease_token=job.lease_token,
                    content=rendered.content,
                    filename=_filename(render_envelope.title, snapshot.snapshot_id, rendered.extension),
                    mime_type=rendered.mime_type,
                    preview=rendered.preview,
                )
                _safe_metric(
                    observability_metrics.record_export_bytes,
                    format=job.format,
                    byte_count=len(rendered.content),
                )
                _safe_metric(
                    observability_metrics.record_export_job,
                    format=job.format,
                    status="succeeded",
                    duration_s=time.monotonic() - started_at,
                    queue_s=queue_s,
                    job_id=job.export_job_id,
                )
            except Exception as exc:
                if render_started_at is not None and not render_recorded:
                    _safe_metric(
                        observability_metrics.record_export_render,
                        format=job.format,
                        status="failed",
                        duration_s=time.monotonic() - render_started_at,
                    )
                failed_status = "failed"
                try:
                    failed_job = self.store.fail(
                        job.export_job_id,
                        self.worker_id,
                        job.lease_token,
                        str(exc)[:1000] or type(exc).__name__,
                        retryable=not isinstance(exc, (ValueError, KeyError, PermissionError)),
                    )
                    failed_status = failed_job.status
                except Exception:
                    pass
                _safe_metric(
                    observability_metrics.record_export_job,
                    format=job.format,
                    status=failed_status,
                    duration_s=time.monotonic() - started_at,
                    queue_s=queue_s,
                    job_id=job.export_job_id,
                )
            finally:
                if heartbeat_stop is not None:
                    heartbeat_stop.set()
                if heartbeat_thread is not None:
                    heartbeat_thread.join(timeout=1)
        self._record_queue_state()
        return did_work

    def run_forever(self) -> None:
        while self.running:
            if not self.run_once(limit=max(1, int(getattr(src.settings, "RESULT_EXPORT_JOB_BATCH_SIZE", 8)))):
                time.sleep(max(0.1, float(getattr(src.settings, "WORKER_POLL_INTERVAL_SECONDS", 0.5))))


def main() -> None:
    worker = ResultExportWorker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
