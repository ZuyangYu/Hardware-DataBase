"""Token-bucket rate limiting for RPM/TPM-style quotas (P0b).

Concurrency limits (the resource governor) and rate limits are managed
separately: a call must fit both.  Buckets are keyed by an actual quota scope
(``provider``/``account``/``model``), never by request or user id.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_second: float, *, burst: float | None = None) -> None:
        self.rate = max(0.0001, float(rate_per_second))
        self.capacity = max(self.rate, float(burst if burst is not None else self.rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    def tokens_available(self) -> float:
        with self._lock:
            self._refill(time.monotonic())
            return self._tokens

    def take(self, tokens: float = 1.0) -> bool:
        """Non-blocking token reservation."""
        tokens = max(0.0, float(tokens))
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self._tokens + 1e-9 < tokens:
                return False
            self._tokens -= tokens
            return True

    def wait(self, tokens: float = 1.0, timeout: float = 0.0) -> bool:
        """Block up to ``timeout`` seconds for enough tokens."""
        tokens = max(0.0, float(tokens))
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._tokens + 1e-9 >= tokens:
                    self._tokens -= tokens
                    return True
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(sleep_for, remaining, 0.25))


class RateLimiter:
    """Registry of named token buckets with a no-config = unlimited default."""

    def __init__(self, configs: dict[str, tuple[float, float]] | None = None) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        for key, (rate, burst) in (configs or {}).items():
            self.configure(key, rate, burst)

    def configure(self, key: str, rate_per_second: float, burst: float | None = None) -> None:
        with self._lock:
            self._buckets[str(key)] = TokenBucket(rate_per_second, burst=burst)

    def is_configured(self, key: str) -> bool:
        with self._lock:
            return str(key) in self._buckets

    def allow(self, key: str, tokens: float = 1.0) -> bool:
        with self._lock:
            bucket = self._buckets.get(str(key))
        return True if bucket is None else bucket.take(tokens)

    def wait(self, key: str, tokens: float = 1.0, timeout: float = 0.0) -> bool:
        with self._lock:
            bucket = self._buckets.get(str(key))
        return True if bucket is None else bucket.wait(tokens, timeout=timeout)
