"""Redis turn-event transport (v3 §10/§11).

PostgreSQL stays the replay truth: the worker publishes each committed event
to a bounded per-turn Redis Stream so an SSE subscriber in another process
learns about it in milliseconds, while the durable `seq` and database replay
cover gaps, tails and reconnects.  Publishing is best-effort and never blocks
or fails the worker.
"""

from __future__ import annotations

import json
from typing import Any

from .redis_client import get_redis_client, redis_url

_MAX_READ_COUNT = 500


def events_transport_enabled() -> bool:
    return bool(redis_url())


def stream_key(turn_id: str) -> str:
    return f"hdb:turn:events:{turn_id}"


def publish_turn_event(
    turn_id: str, seq: int, event_type: str, payload: dict
) -> bool:
    """Best-effort XADD after the durable event commit."""
    if not events_transport_enabled():
        return False
    client = get_redis_client()
    if client is None:
        return False
    import src.settings as settings

    ttl = int(getattr(settings, "TURN_EVENT_STREAM_TTL_SECONDS", 7200))
    maxlen = int(getattr(settings, "TURN_EVENT_STREAM_MAXLEN", 5000))
    envelope = json.dumps(
        {
            "schema_version": 1,
            "turn_id": str(turn_id),
            "seq": int(seq),
            "event": str(event_type),
            "payload": payload or {},
        },
        ensure_ascii=False,
        default=str,
    )
    try:
        kwargs: dict[str, Any] = {}
        if maxlen > 0:
            kwargs["maxlen"] = maxlen
            kwargs["approximate"] = True
        with client.pipeline(transaction=False) as pipeline:
            pipeline.xadd(stream_key(turn_id), {"event": envelope}, **kwargs)
            if ttl > 0:
                pipeline.expire(stream_key(turn_id), ttl)
            pipeline.execute()
        return True
    except Exception:  # noqa: BLE001 - fail-soft boundary
        return False


def read_turn_events(
    turn_id: str, *, after_id: str = "0-0", count: int = 100
) -> tuple[list[dict], str]:
    """Read new stream entries after ``after_id``.

    Returns ``(events, next_id)``.  Events carry the durable ``seq`` so the
    caller can treat the stream purely as a wake-up/notification signal and
    keep reading replay truth from PostgreSQL.
    """
    if not events_transport_enabled():
        return [], after_id
    client = get_redis_client()
    if client is None:
        return [], after_id
    start = f"({after_id}" if after_id and after_id != "0-0" else "-"
    try:
        rows = client.xrange(
            stream_key(turn_id), min=start, max="+", count=min(max(1, count), _MAX_READ_COUNT)
        )
    except Exception:  # noqa: BLE001 - fail-soft boundary
        return [], after_id
    events: list[dict] = []
    next_id = after_id
    for entry_id, fields in rows:
        next_id = str(entry_id)
        raw = (fields or {}).get("event")
        if not raw:
            continue
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(envelope, dict):
            events.append(envelope)
    return events, next_id


def clear_turn_events(turn_id: str) -> bool:
    if not events_transport_enabled():
        return False
    client = get_redis_client()
    if client is None:
        return False
    try:
        client.delete(stream_key(turn_id))
        return True
    except Exception:  # noqa: BLE001 - fail-soft boundary
        return False
