"""Single-instance background executor used by the P2a deterministic MVP.

This is intentionally not the P2c lease/checkpoint worker.  It keeps document
execution out of a Streamlit request thread while all material state remains
in the WorkOrder and Artifact stores for inspection after completion.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.observability import observe, submit_with_current_context
from src.observability.metrics import record_authoring


@dataclass
class BackgroundRun:
    run_id: str
    work_order_id: str
    status: str = "queued"
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class DocumentGenerationWorker:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-authoring")
        self._runs: dict[str, BackgroundRun] = {}
        self._lock = threading.Lock()

    def submit(self, work_order_id: str, operation: Callable[[], Any]) -> str:
        run = BackgroundRun(run_id=f"bg-{uuid.uuid4().hex}", work_order_id=work_order_id)
        with self._lock:
            self._runs[run.run_id] = run
        future = submit_with_current_context(self._executor, self._execute, run.run_id, operation)
        future.add_done_callback(lambda completed: self._complete(run.run_id, completed))
        return run.run_id

    def get(self, run_id: str) -> BackgroundRun | None:
        with self._lock:
            run = self._runs.get(run_id)
            return None if run is None else BackgroundRun(**run.__dict__)

    def _execute(self, run_id: str, operation: Callable[[], Any]) -> Any:
        with self._lock:
            run = self._runs[run_id]
            run.status = "running"
        started = datetime.now(timezone.utc).timestamp()
        status = "completed"
        with observe.agent(
            "hdb.authoring.run",
            run_id=run_id,
            work_order_id=run.work_order_id,
        ):
            try:
                return operation()
            except Exception:
                status = "failed"
                raise
            finally:
                record_authoring(
                    status=status,
                    duration_s=max(0.0, datetime.now(timezone.utc).timestamp() - started),
                )

    def _complete(self, run_id: str, future: Future) -> None:
        with self._lock:
            run = self._runs[run_id]
            run.completed_at = datetime.now(timezone.utc)
            try:
                future.result()
            except Exception as exc:
                run.status = "failed"
                run.error = str(exc)
            else:
                run.status = "completed"

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
