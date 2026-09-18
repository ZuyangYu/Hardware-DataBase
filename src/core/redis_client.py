"""Shared lazy Redis client for the realtime plane (v3 §4).

A single fail-soft client serves cancellation keys, the turn-event stream and
(distributed admission later).  An unreachable Redis is retried after a short
window instead of being disabled forever; callers always keep a durable
fallback.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_CLIENT: Any | None = None
_CLIENT_LOCK = threading.Lock()
_FAILED_AT: float = 0.0
_RETRY_SECONDS = 5.0


def _settings():
    import src.settings as settings

    return settings


def redis_url() -> str:
    return str(getattr(_settings(), "REDIS_URL", "") or "").strip()


def get_redis_client() -> Any | None:
    """Return a shared sync client, or None while Redis is disabled/unreachable."""
    global _CLIENT, _FAILED_AT
    if _CLIENT is not None:
        return _CLIENT
    now = time.monotonic()
    if _FAILED_AT and now - _FAILED_AT < _RETRY_SECONDS:
        return None
    url = redis_url()
    if not url:
        return None
    try:
        import redis
    except ImportError:
        _FAILED_AT = now
        return None
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=1.0,
                socket_connect_timeout=1.0,
            )
            client.ping()
            _CLIENT = client
            _FAILED_AT = 0.0
        except Exception:  # noqa: BLE001 - fail-soft boundary
            _FAILED_AT = time.monotonic()
            return None
    return _CLIENT


def reset_client() -> None:
    global _CLIENT, _FAILED_AT
    with _CLIENT_LOCK:
        _CLIENT = None
        _FAILED_AT = 0.0
