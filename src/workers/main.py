from __future__ import annotations

import signal
import time
import uuid

import src.settings
from src.api.context import build_context_for_user
from src.api.routes.query import _run_turn
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService
from src.core.conversation import ConversationService
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
        self.runtime = getattr(getattr(self.pipeline, "backend", None), "runtime", None)

    def stop(self, *_args) -> None:
        self.running = False

    def run_once(self) -> bool:
        did_work = False
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
