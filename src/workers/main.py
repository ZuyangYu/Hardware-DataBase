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
from src.core.logger import error, log, warn
from src.observability import init_observability, shutdown_observability
from src.observability.metrics import (
    record_worker,
    set_queue_state,
)
from src.observability.worker_registry import WorkerHeartbeat, heartbeat


VALID_WORKER_ROLES = ("all", "chat", "parse")



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
    - ``parse``: RAGFlow/large-file parsing and attachment parse/coordination
    """

    def __init__(self, role: str = "all"):
        self.role = resolve_worker_role(role)
        self.running = True
        self.worker_id = f"worker-{uuid.uuid4().hex}"
        self.auth = AuthService()
        self.conversations = ConversationService()
        self.pipeline = AppPipeline()
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
            from src.agents.tools.retrieval_executor import initialize_retrieval_executor

            initialize_retrieval_executor()
            self._env_mtime_ns = current_mtime_ns
            # v3 §11.3: record the reloaded content revision so config drift
            # across API/worker processes is traceable in logs.
            log(
                "Worker reloaded runtime settings "
                f"(config_revision={getattr(src.settings, 'CONFIG_REVISION', '')})"
            )
        except Exception as exc:
            error(f"Worker failed to reload runtime settings: {exc}")

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
        if self._role_enabled("chat"):
            try:
                self.conversations.requeue_stale_turns()
                self.conversations.expire_queued_turns()
            except Exception as exc:  # noqa: BLE001 - maintenance must not kill the worker
                error(f"Chat turn maintenance failed: {exc}")
            try:
                depth, oldest_age_s = self.conversations.pending_turn_queue_state()
                set_queue_state("chat", depth=depth, oldest_age_s=oldest_age_s)
            except Exception as exc:  # noqa: BLE001 - metrics are fail-soft
                error(f"Chat queue state failed: {exc}")
            try:
                if self._process_chat_claims():
                    did_work = True
            except Exception as exc:  # noqa: BLE001 - keep polling after a DB lock
                error(f"Chat claim loop failed: {exc}")
            try:
                if self._process_mcp_write_proposals(
                    limit=max(1, int(getattr(src.settings, "MCP_WRITE_MAX_ATTEMPTS", 3))),
                ):
                    did_work = True
            except Exception as exc:  # noqa: BLE001 - fail-soft auxiliary path
                error(f"MCP write proposal loop failed: {exc}")
            try:
                depth, oldest_age_s = self.conversations.pending_turn_queue_state()
                set_queue_state("chat", depth=depth, oldest_age_s=oldest_age_s)
            except Exception as exc:  # noqa: BLE001 - metrics are fail-soft
                error(f"Chat queue state failed: {exc}")
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
    obsolete = src.settings.obsolete_env_keys()
    if obsolete:
        warn("配置中包含已废弃/内置项，可删除: %s", ", ".join(obsolete))
    from src.agents.checkpoints.postgres_provider import verify_configured_checkpoint_schemas

    verify_configured_checkpoint_schemas()
    try:
        from src.agents.tools.retrieval_executor import initialize_retrieval_executor

        try:
            initialize_retrieval_executor()
        except Exception as exc:
            error(f"Worker retrieval executor unavailable: {type(exc).__name__}")
        worker = HardwareWorker(role=resolve_worker_role(args.role))
        signal.signal(signal.SIGTERM, worker.stop)
        signal.signal(signal.SIGINT, worker.stop)
        with WorkerHeartbeat(worker.worker_id, worker.role):
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
        shutdown_observability()


if __name__ == "__main__":
    main()
