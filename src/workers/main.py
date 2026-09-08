from __future__ import annotations

import signal
import os
import time
import uuid

import src.settings
from src.api.context import build_context_for_user
from src.api.routes.query import _run_turn
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService
from src.core.conversation import ConversationService
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.core.logger import error, log
from src.observability import init_observability, shutdown_observability
from src.observability.metrics import record_worker, set_queue_state
from src.observability.worker_registry import heartbeat, register, unregister


class HardwareWorker:
    """Poll durable chat/parse records outside the HTTP server process.

    SQLite remains supported for local single-host deployments. The public
    claim/process boundary is intentionally database-only so the same worker
    can later be backed by PostgreSQL + Redis without changing API routes.
    """

    def __init__(self):
        self.running = True
        self.worker_id = f"worker-{uuid.uuid4().hex}"
        register(self.worker_id)
        self.auth = AuthService()
        self.conversations = ConversationService()
        self.pipeline = AppPipeline()
        self.document_jobs = getattr(self.pipeline, "document_job_store", None)
        if self.document_jobs is None:
            self.document_jobs = DocumentAuthoringJobStore()
        self.runtime = getattr(getattr(self.pipeline, "backend", None), "runtime", None)
        self._env_mtime_ns = self._settings_mtime_ns()

    @staticmethod
    def _settings_mtime_ns() -> int:
        try:
            return os.stat(src.settings.ENV_FILE_PATH).st_mtime_ns
        except OSError:
            return -1

    def _reload_runtime_settings_if_changed(self) -> None:
        """Reload live config in the standalone worker.

        The API and chat worker are separate processes. The API config route
        can clear its own model cache, but cannot clear the worker's cache;
        watching the shared .env file keeps queued turns on the new model
        without requiring a worker restart.
        """
        current_mtime_ns = self._settings_mtime_ns()
        if current_mtime_ns == self._env_mtime_ns:
            return
        try:
            src.settings.reload_settings()
            from src.core.model_factory import create_chat_model

            create_chat_model.cache_clear()
            self._env_mtime_ns = current_mtime_ns
            log("Worker reloaded runtime settings")
        except Exception as exc:
            error(f"Worker failed to reload runtime settings: {exc}")

    def _process_document_authoring_jobs(
        self,
        limit: int = 4,
        time_budget_seconds: float | None = None,
    ) -> bool:
        """Claim and execute only persisted document-authoring operations.

        The worker never receives a closure from the HTTP process.  A job is
        resolved from its allow-listed operation/payload, re-authorized from
        the current auth database, and then handed to the normal frozen-scope
        AppPipeline path.  Lease-expired rows are returned by ``list_pending``
        and can therefore be adopted after a worker restart.
        """
        pending = self.document_jobs.list_pending(limit=limit)
        try:
            budget = float(
                time_budget_seconds
                if time_budget_seconds is not None
                else getattr(src.settings, "DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS", 10)
            )
        except (TypeError, ValueError):
            budget = 10.0
        deadline = time.monotonic() + max(0.0, budget)
        try:
            queue_depth, oldest_age_s = self.document_jobs.queue_state()
        except AttributeError:  # compatibility with a small injected test store
            queue_depth, oldest_age_s = len(pending), 0.0
        set_queue_state("document_authoring", depth=queue_depth, oldest_age_s=oldest_age_s)
        did_work = False
        for candidate in pending:
            # The time budget is a boundary between jobs.  A running job is
            # allowed to finish so its lease/idempotency state remains valid;
            # the next iteration yields the worker to the chat queue.
            if did_work and time.monotonic() >= deadline:
                break
            job = self.document_jobs.claim(
                candidate.job_id,
                self.worker_id,
                lease_seconds=max(15, int(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_LEASE_SECONDS", 300))),
            )
            if job is None:
                continue
            did_work = True
            started = time.monotonic()
            try:
                if job.operation not in {"generate_work_order", "resume_work_order", "convert_artifact"}:
                    # This should be unreachable because Store validates the
                    # operation, but keep the worker fail-closed if a future
                    # operation is added without a dispatch branch.
                    self.document_jobs.fail(
                        job.job_id, self.worker_id, job.lease_token,
                        "unsupported document authoring job operation",
                        retryable=False,
                    )
                    record_worker(status="failed", duration_s=time.monotonic() - started)
                    continue

                user = self.auth.get_user_by_username(job.user_id)
                if user is None:
                    try:
                        user = self.auth.get_user_by_id(int(job.user_id))
                    except (TypeError, ValueError):
                        user = None
                if user is None or not user.is_active:
                    self.document_jobs.fail(
                        job.job_id, self.worker_id, job.lease_token,
                        "document authoring job owner is unavailable",
                        retryable=False,
                    )
                    record_worker(status="failed", duration_s=time.monotonic() - started)
                    continue

                payload = dict(job.payload or {})
                work_order_id = str(payload.get("work_order_id") or job.work_order_id or "").strip()
                kb_name = str(payload.get("knowledge_base_name") or "").strip()
                if not work_order_id or not kb_name:
                    self.document_jobs.fail(
                        job.job_id, self.worker_id, job.lease_token,
                        "document authoring job payload is incomplete",
                        retryable=False,
                    )
                    record_worker(status="failed", duration_s=time.monotonic() - started)
                    continue

                # Rebuild scope from the live user record.  The persisted
                # payload carries references only; it cannot override tenant,
                # department or permissions.
                ctx = build_context_for_user(user, kb_name, auth=self.auth)
                def document_job_cancelled() -> bool:
                    current_job = self.document_jobs.get(job.job_id)
                    return current_job is not None and current_job.status == "cancelled"

                ctx.metadata["document_job_cancelled"] = document_job_cancelled
                self.document_jobs.heartbeat(
                    job.job_id,
                    self.worker_id,
                    job.lease_token,
                    lease_seconds=max(15, int(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_LEASE_SECONDS", 300))),
                )
                if job.operation == "convert_artifact":
                    source_artifact_id = str(payload.get("source_artifact_id") or "").strip()
                    target_format = str(payload.get("target_format") or "").strip().lower()
                    if not source_artifact_id or not target_format:
                        raise ValueError("document artifact conversion payload is incomplete")
                    converted = self.pipeline.document_generation.convert_document_artifact(
                        ctx,
                        source_artifact_id,
                        target_format=target_format,
                    )
                    self.document_jobs.complete(
                        job.job_id,
                        self.worker_id,
                        job.lease_token,
                        result={
                            "work_order_id": work_order_id,
                            "source_artifact_id": source_artifact_id,
                            "artifact_id": converted.artifact_id,
                            "output_format": converted.output_format,
                            "stage": converted.stage,
                            "status": "completed",
                        },
                    )
                    record_worker(status="success", duration_s=time.monotonic() - started)
                    continue
                if job.operation == "generate_work_order":
                    result = self.pipeline.continue_knowledge_base_document_generation(
                        ctx, work_order_id, should_cancel=document_job_cancelled,
                    )
                else:
                    harness_run_id = str(payload.get("harness_run_id") or "").strip()
                    if not harness_run_id:
                        raise ValueError("resume document authoring job payload is incomplete")
                    result = self.pipeline.resume_knowledge_base_document_generation_run(
                        ctx,
                        work_order_id,
                        harness_run_id,
                        should_cancel=document_job_cancelled,
                    )
                artifact_id = getattr(result, "artifact_id", None)
                conversion_job_id = None
                requested_output_format = str(payload.get("requested_output_format") or "native").strip().lower()
                if artifact_id and requested_output_format not in {"", "native"}:
                    artifact = self.pipeline.document_generation.store.get_artifact(str(artifact_id))
                    native_format = str(getattr(artifact, "output_format", "") or "").strip().lower()
                    if native_format and requested_output_format != native_format:
                        conversion_job = self.pipeline.submit_document_artifact_conversion(
                            ctx,
                            str(artifact_id),
                            target_format=requested_output_format,
                            session_id=getattr(job, "session_id", None),
                        )
                        conversion_job_id = conversion_job.job_id
                self.document_jobs.complete(
                    job.job_id,
                    self.worker_id,
                    job.lease_token,
                    result={
                        "work_order_id": work_order_id,
                        "artifact_id": artifact_id,
                        "status": getattr(result, "stage", "completed"),
                        "requested_output_format": requested_output_format,
                        "conversion_job_id": conversion_job_id,
                    },
                )
                record_worker(status="success", duration_s=time.monotonic() - started)
            except Exception as exc:
                # Validation, authorization and frozen-input errors are
                # deterministic and must not spin forever.  Provider/backend
                # failures are retryable and eventually become dead-letter.
                retryable = not isinstance(exc, (PermissionError, KeyError, ValueError))
                try:
                    self.document_jobs.fail(
                        job.job_id,
                        self.worker_id,
                        job.lease_token,
                        str(exc)[:1000] or type(exc).__name__,
                        retryable=retryable,
                    )
                except Exception as fail_exc:
                    error(f"Document authoring job failure state update failed for {job.job_id}: {fail_exc}")
                record_worker(status="failed", duration_s=time.monotonic() - started)
                error(f"Document authoring worker failed for job {job.job_id}: {exc}")
            finally:
                heartbeat(self.worker_id)
        return did_work

    def _plan_submission_store(self):
        planning = getattr(getattr(self.pipeline, "document_generation", None), "planning", None)
        return getattr(planning, "store", None) and planning.store.submissions

    def _process_document_plan_submissions(self, limit: int = 2) -> bool:
        """Drain confirmed plan submissions before normal document jobs.

        Each submission is claimed under a lease, re-authorized from the live
        auth database, and materialized into exactly one template-backed
        Work Order/job by the pipeline dispatcher.  Deterministic failures
        (permission/hash errors) fail non-retryably; provider failures retry
        with backoff until ``max_attempts``.
        """
        submissions = self._plan_submission_store()
        if submissions is None:
            return False
        pending = submissions.list_pending(limit=limit)
        did_work = False
        for candidate in pending:
            claimed = submissions.claim(
                candidate.submission_id,
                self.worker_id,
                lease_seconds=max(15, int(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_LEASE_SECONDS", 300))),
            )
            if claimed is None:
                continue
            did_work = True
            try:
                user = self.auth.get_user_by_username(claimed.user_id)
                if user is None:
                    try:
                        user = self.auth.get_user_by_id(int(claimed.user_id))
                    except (TypeError, ValueError):
                        user = None
                if user is None or not user.is_active:
                    submissions.fail(
                        claimed.submission_id, self.worker_id, claimed.lease_token,
                        "plan submission owner is unavailable", retryable=False,
                    )
                    continue
                ctx = build_context_for_user(
                    user, claimed.knowledge_base_name, auth=self.auth,
                )
                submissions.heartbeat(
                    claimed.submission_id,
                    self.worker_id,
                    claimed.lease_token,
                    lease_seconds=max(15, int(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_LEASE_SECONDS", 300))),
                )
                result = self.pipeline.dispatch_confirmed_document_plan(ctx, claimed)
                status = str(result.get("status") or "")
                if status == "dispatched":
                    submissions.mark_dispatched(
                        claimed.submission_id, self.worker_id, claimed.lease_token,
                        work_order_id=str(result["work_order_id"]),
                        job_id=str(result["job_id"]),
                    )
                elif status == "waiting_human":
                    submissions.mark_waiting_human(
                        claimed.submission_id, self.worker_id, claimed.lease_token,
                        work_order_id=str(result["work_order_id"]),
                    )
                else:
                    submissions.fail(
                        claimed.submission_id, self.worker_id, claimed.lease_token,
                        f"unexpected dispatch status: {status or 'missing'}",
                        retryable=True,
                    )
            except Exception as exc:
                retryable = not isinstance(exc, (PermissionError, KeyError, ValueError))
                try:
                    submissions.fail(
                        claimed.submission_id,
                        self.worker_id,
                        claimed.lease_token,
                        str(exc)[:1000] or type(exc).__name__,
                        retryable=retryable,
                    )
                except Exception as fail_exc:
                    error(f"Plan submission failure state update failed for {claimed.submission_id}: {fail_exc}")
                error(f"Plan submission dispatch failed for {claimed.submission_id}: {exc}")
        return did_work

    def stop(self, *_args) -> None:
        self.running = False

    def run_once(self) -> bool:
        self._reload_runtime_settings_if_changed()
        did_work = False
        # Chat attachment parse jobs (design §12.2): processed in the shared
        # worker so a single deployment command covers the whole pipeline.
        # Fail-soft: an attachment fault never blocks chat/document work.
        if getattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", False):
            try:
                from src.attachments.worker import AttachmentWorker

                if not hasattr(self, "_attachment_worker"):
                    self._attachment_worker = AttachmentWorker(worker_id=self.worker_id)
                if self._attachment_worker.run_once():
                    did_work = True
            except Exception as exc:
                error(f"Attachment worker failed: {exc}")
        if self._process_document_plan_submissions(
            limit=max(1, int(getattr(src.settings, "DOCUMENT_PLAN_SUBMISSION_BATCH_SIZE", 2))),
        ):
            did_work = True
        if self._process_document_authoring_jobs(
            limit=max(1, int(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_BATCH_SIZE", 4))),
            time_budget_seconds=max(
                0.0,
                float(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS", 10)),
            ),
        ):
            did_work = True
        self.conversations.requeue_stale_turns()
        depth, oldest_age_s = self.conversations.pending_turn_queue_state()
        set_queue_state("chat", depth=depth, oldest_age_s=oldest_age_s)
        for turn, user_id in self.conversations.list_pending_turn_work(limit=8):
            user = self.auth.get_user_by_id(user_id)
            if user is None or not user.is_active:
                continue
            try:
                started = time.monotonic()
                heartbeat(self.worker_id, task_kind="chat", task_id=str(turn.id))
                ctx = build_context_for_user(user, turn.kb_name, auth=self.auth)
                _run_turn(turn_id=turn.id, user=user, ctx=ctx, pipeline=self.pipeline)
                did_work = True
                record_worker(status="success", duration_s=time.monotonic() - started)
            except Exception as exc:
                record_worker(status="failed", duration_s=time.monotonic() - started)
                error(f"Chat worker failed for turn {turn.id}: {exc}")
            finally:
                heartbeat(self.worker_id)
        depth, oldest_age_s = self.conversations.pending_turn_queue_state()
        set_queue_state("chat", depth=depth, oldest_age_s=oldest_age_s)
        if self.runtime is not None:
            batch = max(1, src.settings.WORKER_PARSE_BATCH_SIZE)
            for _ in range(batch):
                started = time.monotonic()
                heartbeat(self.worker_id, task_kind="parse")
                try:
                    if not self.runtime.run_once():
                        break
                    did_work = True
                    record_worker(status="success", duration_s=time.monotonic() - started)
                except Exception as exc:
                    record_worker(status="failed", duration_s=time.monotonic() - started)
                    error(f"Parse worker failed: {exc}")
                finally:
                    heartbeat(self.worker_id)
        return did_work

    def run_forever(self) -> None:
        log("Hardware DataBase worker started")
        while self.running:
            # H6: 即使队列为空也要刷新进程注册表心跳，
            # 否则上游无法区分「空闲」与「已死」。
            heartbeat(self.worker_id)
            if not self.run_once():
                time.sleep(max(0.1, src.settings.WORKER_POLL_INTERVAL_SECONDS))
        log("Hardware DataBase worker stopped")


def main() -> None:
    init_observability(
        "hardware-database-worker",
        service_version=src.settings.OBS_SERVICE_VERSION,
        environment=src.settings.OBS_ENVIRONMENT,
    )
    worker = HardwareWorker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    try:
        worker.run_forever()
    finally:
        unregister(worker.worker_id)
        shutdown_observability()


if __name__ == "__main__":
    main()
