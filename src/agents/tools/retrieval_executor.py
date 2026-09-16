"""Process-level bounded retrieval executor (P0-007).

One shared thread pool and one bounded wait queue per process replace the
per-call ``ThreadPoolExecutor`` in ``document_search_batch``.  Per-request
fan-out is limited by the caller; the global RAG budget is enforced separately
by the resource governor at the RAGFlow client boundary.

Cancellation semantics: unstarted futures are cancelled, running calls are
left to finish (a Python thread cannot be interrupted), and their late results
are discarded here; the tool layer additionally re-checks cancellation before
registering evidence.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from src.observability import submit_with_current_context


class RetrievalSaturated(RuntimeError):
    """Raised/recorded when the bounded retrieval queue cannot admit work."""


@dataclass
class RetrievalCallResult:
    index: int
    value: Any = None
    error: Exception | None = None
    status: str = "ok"  # ok | failed | cancelled | saturated


class RetrievalExecutor:
    def __init__(
        self,
        *,
        max_workers: int = 8,
        queue_max_depth: int = 256,
        per_request_fanout: int = 4,
        queue_max_wait_seconds: float = 30.0,
    ) -> None:
        self.max_workers = max(1, int(max_workers))
        self.queue_max_depth = max(1, int(queue_max_depth))
        self.per_request_fanout = max(1, int(per_request_fanout))
        self.queue_max_wait_seconds = max(0.0, float(queue_max_wait_seconds))
        self._executor: ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self._condition = threading.Condition()
        self._pending = 0
        self._closed = False
        self._high_water = 0
        # Futures admitted into the pool, tracked so shutdown can cancel queued
        # items and bound the wait for already-running calls (plan §6.5.1).
        self._futures: set[Future] = set()

    # -- lifecycle ----------------------------------------------------------

    def _ensure_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._closed:
                raise RetrievalSaturated("retrieval executor is closed")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix="hdb-retrieval",
                )
            return self._executor

    def close(self, *, deadline_seconds: float = 0.0) -> int:
        """Shut down the shared pool; returns unconfirmed (still running) calls.

        Queued-but-unstarted futures are cancelled.  Already-running calls are
        given ``deadline_seconds`` to finish; calls still unconfirmed at the
        deadline keep their downstream permits leased (they become ``uncertain``
        after lease expiry), so a process exit never clears quotas.
        """
        with self._executor_lock:
            self._closed = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if deadline_seconds <= 0:
            return len(self._futures)
        done, not_done = wait(
            list(self._futures), timeout=max(0.0, float(deadline_seconds))
        )
        unconfirmed = len(not_done)
        for future in done:
            self._discard_future(future)
        if unconfirmed:
            self._record_unconfirmed(unconfirmed)
        return unconfirmed

    def _discard_future(self, future: Future) -> None:
        with self._condition:
            self._futures.discard(future)

    @staticmethod
    def _record_unconfirmed(count: int) -> None:
        try:
            from src.observability.metrics import counter

            counter(
                "hdb.retrieval.executor.unconfirmed",
                value=int(count),
                description="Running retrieval calls still unconfirmed at shutdown deadline",
            )
        except Exception:
            pass

    # -- bounded submission -------------------------------------------------

    def _release(self) -> None:
        with self._condition:
            self._pending = max(0, self._pending - 1)
            self._condition.notify_all()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """Submit one call with ContextVar/OTel propagation."""
        executor = self._ensure_executor()
        with self._condition:
            self._pending += 1
            self._high_water = max(self._high_water, self._pending)
        try:
            future = submit_with_current_context(executor, fn, *args, **kwargs)
        except Exception:
            self._release()
            raise
        with self._condition:
            self._futures.add(future)
        future.add_done_callback(self._on_future_done)
        return future

    def _on_future_done(self, future: Future) -> None:
        self._discard_future(future)
        self._release()

    def _admit(self, deadline: float) -> bool:
        """Wait for a queue slot; returns False when the deadline passed."""
        with self._condition:
            while self._pending >= self.queue_max_depth:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, 0.2))
            return True

    def map_bounded(
        self,
        fn: Callable[[Any], Any],
        items: Sequence[Any],
        *,
        fanout: int | None = None,
        deadline: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[RetrievalCallResult]:
        """Run ``fn`` over ``items`` with bounded fan-out, queue and deadline.

        Results keep the input order.  Cancelled, failed and saturated calls are
        reported per item instead of failing the whole batch.
        """
        fanout = max(1, int(fanout or self.per_request_fanout))
        wait_budget = self.queue_max_wait_seconds if deadline is None else max(0.0, float(deadline))
        deadline_at = time.monotonic() + wait_budget
        items = list(items)
        results: list[RetrievalCallResult] = [
            RetrievalCallResult(index=index, status="cancelled") for index in range(len(items))
        ]
        futures: dict[Future, int] = {}
        next_index = 0

        def cancelled() -> bool:
            return bool(cancel_check is not None and cancel_check())

        while next_index < len(items) or futures:
            if cancelled():
                for future, index in futures.items():
                    if future.cancel():
                        results[index].status = "cancelled"
                futures.clear()
                break
            while next_index < len(items) and len(futures) < fanout:
                admit_started = time.monotonic()
                if not self._admit(deadline_at):
                    self._record_admission("saturated", time.monotonic() - admit_started)
                    for index in range(next_index, len(items)):
                        results[index].status = "saturated"
                        results[index].error = RetrievalSaturated("retrieval queue is full")
                    next_index = len(items)
                    break
                self._record_admission("admitted", time.monotonic() - admit_started)
                future = self.submit(fn, items[next_index])
                futures[future] = next_index
                next_index += 1
            if not futures:
                break
            done, _pending = wait(list(futures), timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                index = futures.pop(future)
                result = results[index]
                if future.cancelled():
                    result.status = "cancelled"
                    continue
                error = future.exception()
                if error is not None:
                    if isinstance(error, RetrievalSaturated):
                        result.status = "saturated"
                    else:
                        result.status = "failed"
                    result.error = error
                    continue
                result.status = "ok"
                result.value = future.result()
        return results

    @staticmethod
    def _record_admission(status: str, wait_s: float) -> None:
        try:
            from src.observability.metrics import record_retrieval_executor

            record_retrieval_executor(status=status, wait_s=wait_s)
        except Exception:
            pass

    # -- diagnostics --------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._condition:
            pending = self._pending
        with self._executor_lock:
            alive = self._executor is not None
        return {
            "pending": pending,
            "high_water": self._high_water,
            "max_workers": self.max_workers,
            "queue_max_depth": self.queue_max_depth,
            "alive": int(alive),
        }


_EXECUTOR: RetrievalExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def get_retrieval_executor() -> RetrievalExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            import src.settings as settings

            _EXECUTOR = RetrievalExecutor(
                max_workers=int(getattr(settings, "RETRIEVAL_EXECUTOR_MAX_WORKERS", 8)),
                queue_max_depth=int(getattr(settings, "RETRIEVAL_QUEUE_MAX_DEPTH", 256)),
                per_request_fanout=int(getattr(settings, "RETRIEVAL_PER_REQUEST_FANOUT", 4)),
                queue_max_wait_seconds=float(
                    getattr(settings, "RETRIEVAL_QUEUE_MAX_WAIT_SECONDS", 30.0)
                ),
            )
            import atexit

            atexit.register(reset_retrieval_executor)
        return _EXECUTOR


def reset_retrieval_executor(*, deadline_seconds: float = 0.0) -> int:
    """Close the singleton; returns unconfirmed calls at the deadline."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        unconfirmed = 0
        if _EXECUTOR is not None:
            unconfirmed = _EXECUTOR.close(deadline_seconds=deadline_seconds)
        _EXECUTOR = None
        return unconfirmed
