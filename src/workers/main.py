from __future__ import annotations

import argparse
import signal
import os
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

import src.settings
from src.api.context import build_context_for_user
from src.api.routes.query import _run_turn
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService
from src.core.conversation import ConversationService
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.core.logger import error, log
from src.observability import init_observability, shutdown_observability
from src.observability.metrics import (
    record_job_dead_letter,
    record_job_phase,
    record_worker,
    set_queue_state,
)
from src.observability.worker_registry import heartbeat, register, unregister


VALID_WORKER_ROLES = ("all", "chat", "authoring", "parse")


def _settle_action_receipts(
    job,
    work_order_id: str = "",
    *,
    status: str,
    result_refs: list[str] | None = None,
    error_code: str = "",
    message: str = "",
) -> None:
    """P2-001: wake action-plan receipts waiting on this document job.

    Best-effort by design: a receipt-ledger fault must never fail the job.
    """
    try:
        from src.agents.orchestration.receipts import settle_action_receipts_for_job

        references = []
        if getattr(job, "task_id", None):
            references.append(f"task:{job.task_id}")
        for candidate in (
            work_order_id,
            str(getattr(job, "work_order_id", "") or ""),
            str(getattr(job, "job_id", "") or ""),
        ):
            if candidate:
                references.append(f"work_order:{candidate}")
        settle_action_receipts_for_job(
            references,
            status=status,
            result_refs=result_refs,
            error_code=error_code,
            message=message,
        )
    except Exception:
        pass


def resolve_worker_role(value: str | None = None) -> str:
    """Normalize a role argument/config; unknown values fall back to ``all``."""
    role = str(value or getattr(src.settings, "WORKER_ROLE", "all") or "all").strip().lower()
    return role if role in VALID_WORKER_ROLES else "all"


class HardwareWorker:
    """Poll durable chat/parse records outside the HTTP server process.

    SQLite remains supported for local single-host deployments. The public
    claim/process boundary is intentionally database-only so the same worker
    can later be backed by PostgreSQL + Redis without changing API routes.

    Roles:
    - ``all``: legacy combined loop (local development only)
    - ``chat``: chat turn claim/lease/recovery
    - ``authoring``: document plan submissions and authoring jobs
    - ``parse``: RAGFlow/large-file parsing and attachment parse/coordination
    """

    def __init__(self, role: str = "all"):
        self.role = resolve_worker_role(role)
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

    def _role_enabled(self, *roles: str) -> bool:
        current = resolve_worker_role(getattr(self, "role", "all"))
        return current == "all" or current in roles

    @staticmethod
    def _acquire_heavy_permit():
        """Acquire one HEAVY_JOB slot; ``False`` means saturated, ``None`` disabled."""
        if not bool(getattr(src.settings, "RESOURCE_GOVERNOR_ENABLED", True)):
            return None
        try:
            from src.core.concurrency import GovernorRejected, get_governor

            return get_governor().acquire("heavy_job", interactive=False, timeout=1.0)
        except GovernorRejected:
            return False

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
            from src.core.model_factory import invalidate_chat_model_cache

            invalidate_chat_model_cache()
            self._env_mtime_ns = current_mtime_ns
            # v3 §11.3: record the reloaded content revision so config drift
            # across API/worker processes is traceable in logs.
            log(
                "Worker reloaded runtime settings "
                f"(config_revision={getattr(src.settings, 'CONFIG_REVISION', '')})"
            )
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
            heavy_permit = self._acquire_heavy_permit()
            if heavy_permit is False:
                # Saturated: leave the job pending and yield this loop.
                break
            try:
                job = self.document_jobs.claim(
                    candidate.job_id,
                    self.worker_id,
                    lease_seconds=max(15, int(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_LEASE_SECONDS", 300))),
                )
            except Exception:
                if heavy_permit is not None:
                    heavy_permit.release()
                raise
            if job is None:
                if heavy_permit is not None:
                    heavy_permit.release()
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
                    _settle_action_receipts(
                        job,
                        work_order_id,
                        status="completed",
                        result_refs=[f"artifact:{converted.artifact_id}"],
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
                _settle_action_receipts(
                    job,
                    work_order_id,
                    status="completed",
                    result_refs=(
                        [f"artifact:{artifact_id}"] if artifact_id else None
                    ),
                )
                record_worker(status="success", duration_s=time.monotonic() - started)
                record_job_phase(kind="authoring", phase="execution", seconds=time.monotonic() - started)
            except Exception as exc:
                # Validation, authorization and frozen-input errors are
                # deterministic and must not spin forever.  Provider/backend
                # failures are retryable and eventually become dead-letter.
                retryable = not isinstance(exc, (PermissionError, KeyError, ValueError))
                record_job_phase(kind="authoring", phase="execution", seconds=time.monotonic() - started)
                if not retryable:
                    record_job_dead_letter(kind="authoring")
                    _settle_action_receipts(
                        job,
                        str(
                            (getattr(job, "payload", {}) or {}).get("work_order_id")
                            or getattr(job, "work_order_id", "")
                            or ""
                        ),
                        status="failed",
                        error_code="authoring_job_failed",
                        message=str(exc)[:500],
                    )
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
                if heavy_permit is not None:
                    heavy_permit.release()
        return did_work

    def _plan_submission_store(self):
        planning = getattr(getattr(self.pipeline, "document_generation", None), "planning", None)
        return getattr(planning, "store", None) and planning.store.submissions

    def _restore_document_task_lineage(self, ctx, submission):
        """Restore durable Chat lineage before materializing a plan submission.

        Plan submissions intentionally carry identifiers only.  The worker
        must rebuild the request context from live authorization and recover
        the Chat association from the owner-scoped GenerationSession; using a
        synthetic worker context would otherwise make the existing Chat task
        look like an API task during idempotency checks.
        """
        generation_store = getattr(
            getattr(getattr(self.pipeline, "document_generation", None), "store", None),
            "generation_sessions",
            None,
        )
        if generation_store is None:
            return ctx
        session = generation_store.get_session(
            submission.session_id,
            tenant_id=submission.tenant_id,
            user_id=submission.user_id,
        )
        conversation_id = str(getattr(session, "conversation_id", None) or "").strip()
        initiating_turn_id = str(getattr(session, "initiating_turn_id", None) or "").strip()
        if not conversation_id and not initiating_turn_id:
            return ctx
        if not conversation_id or not initiating_turn_id:
            raise ValueError("document generation session has incomplete conversation lineage")
        ctx.metadata.update({
            "document_task_origin": "chat",
            "conversation_id": conversation_id,
            "initiating_turn_id": initiating_turn_id,
        })
        return ctx

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
            heavy_permit = self._acquire_heavy_permit()
            if heavy_permit is False:
                break
            claimed = submissions.claim(
                candidate.submission_id,
                self.worker_id,
                lease_seconds=max(15, int(getattr(src.settings, "DOCUMENT_AUTHORING_JOB_LEASE_SECONDS", 300))),
            )
            if claimed is None:
                if heavy_permit is not None:
                    heavy_permit.release()
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
                ctx = self._restore_document_task_lineage(ctx, claimed)
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
            finally:
                if heavy_permit is not None:
                    heavy_permit.release()
        return did_work

    def stop(self, *_args) -> None:
        self.running = False
        loop = getattr(self, "_coordinator_loop", None)
        if loop is not None:
            try:
                loop.stop()
            except Exception:
                pass
        pool = getattr(self, "_chat_pool", None)
        if pool is not None:
            # Bounded drain (v3 §26): queued work is cancelled, running turns
            # finish on their own lease; the reaper converges anything lost.
            pool.shutdown(wait=False, cancel_futures=True)

    def _ensure_chat_pool(self, concurrency: int) -> ThreadPoolExecutor:
        """Process-level chat executor, recreated only on a concurrency change."""
        pool = getattr(self, "_chat_pool", None)
        if pool is not None and getattr(self, "_chat_pool_workers", 0) == concurrency:
            return pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="hdb-chat")
        self._chat_pool = pool
        self._chat_pool_workers = concurrency
        return pool

    def _process_chat_claims(self) -> bool:
        """Execute chat turns with slot-based refill (v3 §6.1).

        A finished turn frees its slot immediately instead of waiting for the
        rest of a batch; the executor lives for the whole process.  `handled`
        stops a requeued turn from being re-submitted within the same pass.
        """
        concurrency = max(1, int(getattr(src.settings, "WORKER_TURN_CONCURRENCY", 1) or 1))
        batch_size = max(1, int(getattr(src.settings, "WORKER_CLAIM_BATCH_SIZE", 8)))
        if concurrency <= 1:
            did_work = False
            for turn, user_id in self.conversations.list_pending_turn_work(limit=batch_size):
                self._process_turn(turn, user_id)
                did_work = True
            return did_work
        pool = self._ensure_chat_pool(concurrency)
        in_flight: dict[Future, str] = {}
        handled: set[str] = set()
        did_work = False
        while self.running:
            free = concurrency - len(in_flight)
            if free > 0:
                candidates = list(
                    self.conversations.list_pending_turn_work(limit=min(free, batch_size))
                )
                for turn, user_id in candidates:
                    if len(in_flight) >= concurrency:
                        break
                    if turn.id in handled:
                        continue
                    handled.add(turn.id)
                    in_flight[pool.submit(self._process_turn, turn, user_id)] = turn.id
                    did_work = True
            if not in_flight:
                break
            done, _pending = wait(
                list(in_flight), timeout=1.0, return_when=FIRST_COMPLETED
            )
            for future in done:
                in_flight.pop(future, None)
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - 单轮失败不影响其他轮
                    error(f"Chat worker turn future failed: {exc}")
        return did_work

    def start_coordinator_control_loop(self) -> None:
        """Start the parse role's dedicated waiting-turn control loop (P0-004).

        The loop only performs conditional DB transitions; it never parses,
        cleans files or performs network calls, so a blocked parse can never
        delay the 600 s wait sweep.  Leadership is a shared lease row, so the
        standalone ``python -m src.attachments.worker`` entry stays compatible
        without two scanners running at once.
        """
        if getattr(self, "_coordinator_loop", None) is not None:
            return
        if not self._role_enabled("parse") or not getattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", False):
            return
        from src.attachments.coordinator_lease import AttachmentCoordinatorLoop

        loop = AttachmentCoordinatorLoop(worker_id=f"{self.worker_id}-coordinator")
        self._coordinator_loop = loop
        loop.start()

    def _process_turn(self, turn, user_id) -> None:
        user = self.auth.get_user_by_id(user_id)
        if user is None or not user.is_active:
            return
        started = time.monotonic()
        try:
            heartbeat(self.worker_id, task_kind="chat", task_id=str(turn.id))
            ctx = build_context_for_user(user, turn.kb_name, auth=self.auth)
            _run_turn(turn_id=turn.id, user=user, ctx=ctx, pipeline=self.pipeline)
            record_worker(status="success", duration_s=time.monotonic() - started)
        except Exception as exc:
            record_worker(status="failed", duration_s=time.monotonic() - started)
            error(f"Chat worker failed for turn {turn.id}: {exc}")
        finally:
            heartbeat(self.worker_id)

    def _process_mcp_write_proposals(self, limit: int = 1) -> bool:
        """Execute user-confirmed MCP write proposals (candidate-artifact mode)."""

        if not bool(getattr(src.settings, "MCP_WRITE_ENABLED", False)):
            return False
        try:
            from src.agents.mcp.executor import MCPToolExecutor
            from src.agents.mcp.write_store import build_write_proposal_store

            store = getattr(self, "_mcp_write_store", None)
            if store is None:
                store = build_write_proposal_store()
                self._mcp_write_store = store
            store.expire_overdue()
            claimed = store.claim(self.worker_id, limit=max(1, int(limit)))
            if not claimed:
                return False
            executor = MCPToolExecutor(write_store=store)
            for proposal in claimed:
                try:
                    outcome = executor.execute_proposal(proposal, worker_id=self.worker_id)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the loop
                    error(f"MCP write proposal {proposal.proposal_id} crashed: {exc}")
                    outcome = executor.finish_proposal(
                        proposal, error_code="worker_error", retryable=True
                    )
                log(
                    "MCP write proposal "
                    f"{proposal.proposal_id} -> {outcome.get('status')} "
                    f"({outcome.get('error_code') or 'ok'})"
                )
            return True
        except Exception as exc:  # noqa: BLE001 - fail-soft like the other workers
            error(f"MCP write worker failed: {exc}")
            return False

    def run_once(self) -> bool:
        self._reload_runtime_settings_if_changed()
        did_work = False
        # Chat attachment parse jobs (design §12.2): owned by the parse role.
        # Fail-soft: an attachment fault never blocks chat/document work.
        if self._role_enabled("parse") and getattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", False):
            try:
                from src.attachments.worker import AttachmentWorker

                if not hasattr(self, "_attachment_worker"):
                    self._attachment_worker = AttachmentWorker(worker_id=self.worker_id)
                if self._attachment_worker.run_once():
                    did_work = True
            except Exception as exc:
                error(f"Attachment worker failed: {exc}")
        if self._role_enabled("authoring"):
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
        if self._role_enabled("chat"):
            self.conversations.requeue_stale_turns()
            self.conversations.expire_queued_turns()
            depth, oldest_age_s = self.conversations.pending_turn_queue_state()
            set_queue_state("chat", depth=depth, oldest_age_s=oldest_age_s)
            if self._process_chat_claims():
                did_work = True
            if self._process_mcp_write_proposals(
                limit=max(1, int(getattr(src.settings, "MCP_WRITE_MAX_ATTEMPTS", 3))),
            ):
                did_work = True
            depth, oldest_age_s = self.conversations.pending_turn_queue_state()
            set_queue_state("chat", depth=depth, oldest_age_s=oldest_age_s)
        if self._role_enabled("parse") and self.runtime is not None:
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
        # RAGFlow 解析是远端异步: 周期性把仍在 parsing 的本地台账与远端对齐,
        # 否则完成/失败状态永远不落库(上传时读不到实时进度的部署)。
        refresh = getattr(getattr(self.pipeline, "backend", None), "refresh_parse_statuses", None)
        if callable(refresh):
            try:
                if refresh(limit=max(2, int(getattr(src.settings, "WORKER_PARSE_BATCH_SIZE", 4)) * 2)):
                    did_work = True
            except Exception as exc:  # noqa: BLE001 - 状态同步失败不影响主循环
                error(f"Parse status refresh failed: {exc}")
        return did_work

    def run_forever(self) -> None:
        log("Hardware DataBase worker started")
        self.start_coordinator_control_loop()
        try:
            from src.core.concurrency import start_governor_maintenance

            start_governor_maintenance()
        except Exception:
            pass
        while self.running:
            # H6: 即使队列为空也要刷新进程注册表心跳，
            # 否则上游无法区分「空闲」与「已死」。
            heartbeat(self.worker_id)
            if not self.run_once():
                time.sleep(max(0.1, src.settings.WORKER_POLL_INTERVAL_SECONDS))
        log("Hardware DataBase worker stopped")


def main() -> None:
    from src.core.redis_client import assert_redis_ready

    assert_redis_ready(purpose="worker 启动")
    parser = argparse.ArgumentParser(description="Hardware DataBase durable worker")
    parser.add_argument(
        "--role",
        choices=VALID_WORKER_ROLES,
        default=None,
        help="consume only this work class (default: WORKER_ROLE or all)",
    )
    args = parser.parse_args()
    init_observability(
        "hardware-database-worker",
        service_version=src.settings.OBS_SERVICE_VERSION,
        environment=src.settings.OBS_ENVIRONMENT,
    )
    worker = HardwareWorker(role=resolve_worker_role(args.role))
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    try:
        worker.run_forever()
    finally:
        try:
            from src.agents.tools.retrieval_executor import reset_retrieval_executor

            reset_retrieval_executor(
                deadline_seconds=float(
                    getattr(src.settings, "RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS", 10)
                )
            )
        except Exception:
            pass
        try:
            from src.agents.mcp.client import reset_mcp_runtime

            reset_mcp_runtime()
        except Exception:
            pass
        unregister(worker.worker_id)
        shutdown_observability()


if __name__ == "__main__":
    main()
