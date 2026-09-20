"""Redis cancel fast path (v3 §8/§12).

The durable cancel flag (PostgreSQL or SQLite) stays authoritative; the Redis
TTL key only shortens the time until the owning worker notices a cancel.  All
helpers are fail-soft: Redis being unavailable must never break cancellation
or execution, and the durable flag always remains the fallback.
"""

from __future__ import annotations

from .redis_client import get_redis_client, redis_url, reset_client

__all__ = [
    "clear_cancel",
    "fast_path_enabled",
    "get_redis_client",
    "is_cancel_requested",
    "publish_cancel",
    "reset_client",
]


def fast_path_enabled() -> bool:
    import src.settings as settings

    return bool(
        getattr(settings, "CANCEL_FAST_PATH_ENABLED", True)
    ) and bool(redis_url())


def _key(turn_id: str) -> str:
    return f"hdb:turn:cancel:{turn_id}"


def publish_cancel(turn_id: str, *, ttl_seconds: int | None = None) -> bool:
    """Best-effort fast-path signal written after the durable cancel commit."""
    if not fast_path_enabled():
        return False
    client = get_redis_client()
    if client is None:
        return False
    import src.settings as settings

    ttl = int(
        ttl_seconds
        if ttl_seconds is not None
        else getattr(settings, "CANCEL_KEY_TTL_SECONDS", 7200)
    )
    try:
        client.set(_key(turn_id), "1", ex=max(1, ttl))
        return True
    except Exception:  # noqa: BLE001 - fail-soft boundary
        return False


def is_cancel_requested(turn_id: str) -> bool:
    if not fast_path_enabled():
        return False
    client = get_redis_client()
    if client is None:
        return False
    try:
        return bool(client.exists(_key(turn_id)))
    except Exception:  # noqa: BLE001 - fail-soft boundary
        return False


def clear_cancel(turn_id: str) -> bool:
    if not fast_path_enabled():
        return False
    client = get_redis_client()
    if client is None:
        return False
    try:
        client.delete(_key(turn_id))
        return True
    except Exception:  # noqa: BLE001 - fail-soft boundary
        return False
