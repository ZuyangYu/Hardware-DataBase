from __future__ import annotations

import signal
import time

import config.settings
from src.api.context import build_context_for_user
from src.api.routes.query import GENERAL_CHAT_KB_NAME, _run_turn
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService
from src.core.conversation import ConversationService
from src.core.logger import error, log


class HardwareWorker:
    """Poll durable chat/parse records outside the HTTP server process.

    SQLite remains supported for local single-host deployments. The public
    claim/process boundary is intentionally database-only so the same worker
    can later be backed by PostgreSQL + Redis without changing API routes.
    """

    def __init__(self):
        self.running = True
        self.auth = AuthService()
        self.conversations = ConversationService()
        self.pipeline = AppPipeline()
        self.runtime = getattr(getattr(self.pipeline, "backend", None), "runtime", None)

    def stop(self, *_args) -> None:
        self.running = False

    def run_once(self) -> bool:
        did_work = False
        self.conversations.requeue_stale_turns()
        for turn, user_id in self.conversations.list_pending_turn_work(limit=8):
            user = self.auth.get_user_by_id(user_id)
            if user is None or not user.is_active:
                continue
            try:
                ctx = build_context_for_user(user, turn.kb_name, auth=self.auth)
                pipeline = None if turn.kb_name in ("", GENERAL_CHAT_KB_NAME) else self.pipeline
                _run_turn(turn_id=turn.id, user=user, ctx=ctx, pipeline=pipeline)
                did_work = True
            except Exception as exc:
                error(f"Chat worker failed for turn {turn.id}: {exc}")
        if self.runtime is not None:
            batch = max(1, config.settings.WORKER_PARSE_BATCH_SIZE)
            for _ in range(batch):
                if not self.runtime.run_once():
                    break
                did_work = True
        return did_work

    def run_forever(self) -> None:
        log("Hardware DataBase worker started")
        while self.running:
            if not self.run_once():
                time.sleep(max(0.1, config.settings.WORKER_POLL_INTERVAL_SECONDS))
        log("Hardware DataBase worker stopped")


def main() -> None:
    worker = HardwareWorker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
