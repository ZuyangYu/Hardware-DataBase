"""(process-wide) LLM admission control: concurrency + priority + queue budget.

Every model call should pass through one governed slot before hitting the
provider:

- ``interactive`` (live agent queries / asset autofill) may use the full
  budget;
- ``batch`` (wiki distillation, memory extraction, ingestion enrichment) is
  capped separately so it can never starve interactive traffic.

Queue waits longer than ``LLM_QUEUE_WARN_SECONDS`` are logged; waits longer
than ``LLM_QUEUE_TIMEOUT_SECONDS`` raise :class:`LLMQueueTimeoutError` so
fail-open callers degrade instead of hanging forever.

This module is admission control only; model construction and usage
accounting live in :mod:`src.core.model_gateway`.

Scope: per process. Worker processes get their own budget; scaling to multiple
API replicas needs a shared limiter (Redis) later. Wiring is fail-open -- any
governor bug must never take the model path down.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

import src.settings as settings

_log = logging.getLogger(__name__)

PRIORITY_INTERACTIVE = "interactive"
PRIORITY_BATCH = "batch"


class LLMQueueTimeoutError(TimeoutError):
    """Raised when a model call cannot be admitted within the queue budget."""


class LLMGovernor:
    """Thread-safe admission controller for model calls."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active_interactive = 0
        self._active_batch = 0
        self._waiting_interactive = 0
        self._waiting_batch = 0
        self._completed = 0
        self._timeouts = 0
        self._max_wait_ms = 0.0
        self._last_warn_monotonic = 0.0

    # -- limits -------------------------------------------------------------

    @staticmethod
    def _limits() -> tuple[int, int]:
        total = max(1, int(getattr(settings, "LLM_MAX_CONCURRENCY", 4) or 4))
        batch_cap = max(0, int(getattr(settings, "LLM_BATCH_MAX_CONCURRENCY", 1) or 0))
        return total, min(batch_cap, total)

    def _admissible_locked(self, is_batch: bool, total: int, batch_cap: int) -> bool:
        active = self._active_interactive + self._active_batch
        if is_batch:
            return self._active_batch < batch_cap and active < total
        return active < total

    # -- admission ----------------------------------------------------------

    @contextmanager
    def slot(self, priority: str = PRIORITY_INTERACTIVE):
        """Hold one governor slot for the duration of the ``with`` body.

        For streaming calls the body must span the whole stream iteration so
        the slot is released only when generation finishes.
        """
        is_batch = priority == PRIORITY_BATCH
        total, batch_cap = self._limits()
        timeout = float(getattr(settings, "LLM_QUEUE_TIMEOUT_SECONDS", 180) or 0)
        started = time.monotonic()
        deadline = started + timeout

        with self._cond:
            if is_batch:
                self._waiting_batch += 1
            else:
                self._waiting_interactive += 1
            admitted = False
            try:
                while True:
                    if self._admissible_locked(is_batch, total, batch_cap):
                        admitted = True
                        break
                    if timeout > 0:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._timeouts += 1
                            break
                        self._cond.wait(min(0.25, remaining))
                    else:
                        self._cond.wait(0.25)
                if not admitted:
                    raise LLMQueueTimeoutError(
                        f"LLM queue timeout after {timeout:.0f}s "
                        f"(priority={priority}, active={self._active_interactive + self._active_batch}/{total}, "
                        f"queued={self._waiting_interactive + self._waiting_batch})"
                    )
                if is_batch:
                    self._active_batch += 1
                else:
                    self._active_interactive += 1
            finally:
                if is_batch:
                    self._waiting_batch = max(0, self._waiting_batch - 1)
                else:
                    self._waiting_interactive = max(0, self._waiting_interactive - 1)

        wait_ms = (time.monotonic() - started) * 1000
        with self._cond:
            self._max_wait_ms = max(self._max_wait_ms, wait_ms)
        warn_seconds = float(getattr(settings, "LLM_QUEUE_WARN_SECONDS", 10) or 0)
        if warn_seconds and wait_ms >= warn_seconds * 1000:
            now = time.monotonic()
            # 最多每 30 秒告警一次, 避免拥塞时刷屏
            if now - self._last_warn_monotonic >= 30:
                self._last_warn_monotonic = now
                with self._cond:
                    active = self._active_interactive + self._active_batch
                    queued = self._waiting_interactive + self._waiting_batch
                _log.warning(
                    "LLM queue wait %.1fs (priority=%s, active=%d/%d, queued=%d)",
                    wait_ms / 1000, priority, active, total, queued,
                )
        try:
            yield
        finally:
            with self._cond:
                if is_batch:
                    self._active_batch = max(0, self._active_batch - 1)
                else:
                    self._active_interactive = max(0, self._active_interactive - 1)
                self._completed += 1
                self._cond.notify_all()

    # -- observability ------------------------------------------------------

    def snapshot(self) -> dict[str, int]:
        total, batch_cap = self._limits()
        with self._cond:
            return {
                "max_concurrency": total,
                "batch_max_concurrency": batch_cap,
                "active": self._active_interactive + self._active_batch,
                "active_interactive": self._active_interactive,
                "active_batch": self._active_batch,
                "queued": self._waiting_interactive + self._waiting_batch,
                "queued_interactive": self._waiting_interactive,
                "queued_batch": self._waiting_batch,
                "completed": self._completed,
                "timeouts": self._timeouts,
                "max_wait_ms": int(self._max_wait_ms),
            }


_governor = LLMGovernor()


def get_llm_governor() -> LLMGovernor:
    return _governor


def reset_llm_governor() -> None:
    """Test helper: drop accumulated metrics/counters."""
    global _governor
    _governor = LLMGovernor()
