"""Redis Lua token-bucket limiter for distributed admission (v3 §14).

Multiple API processes on one host must share one quota: the local bucket in
``admission.py`` is only a cheap pre-check, while this limiter is the
authoritative counter.  ``allow`` returns ``True``/``False`` for a decision
and ``None`` when Redis is unavailable so callers can fail closed explicitly.
"""

from __future__ import annotations

_RATE_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = burst
  ts = now
end
local delta = now - ts
if delta < 0 then delta = 0 end
tokens = math.min(burst, tokens + delta * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, ttl_ms)
return allowed
"""

_TOKEN_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local now = tonumber(ARGV[5])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = burst
  ts = now
end
local delta = now - ts
if delta < 0 then delta = 0 end
tokens = math.min(burst, tokens + delta * rate)
local allowed = 0
if cost <= burst and tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, ttl_ms)
return allowed
"""


def allow(
    key: str,
    *,
    rate_per_minute: float,
    burst: float,
    ttl_seconds: int = 120,
) -> bool | None:
    """Consume one token; ``None`` means the limiter is unavailable."""
    from src.core.redis_client import get_redis_client

    client = get_redis_client()
    if client is None:
        return None
    rate = max(0.001, float(rate_per_minute) / 60.0)
    tokens_burst = max(1.0, min(float(burst), 1000.0))
    ttl_ms = max(1000, int(ttl_seconds) * 1000)
    try:
        seconds, micros = client.time()
        now = float(seconds) + float(micros) / 1_000_000
        result = client.eval(
            _RATE_LUA, 1, str(key), rate, tokens_burst, ttl_ms, now
        )
        return bool(int(result))
    except Exception:  # noqa: BLE001 - fail-soft boundary, caller decides
        return None


def allow_tokens(
    key: str,
    *,
    rate_per_minute: float,
    cost: int,
    burst: float | None = None,
    ttl_seconds: int = 120,
) -> bool | None:
    """Consume ``cost`` tokens from an atomic Redis bucket.

    ``None`` means Redis is unavailable.  The bucket uses Redis server time,
    so API/worker clock skew cannot create extra provider capacity.
    """
    from src.core.redis_client import get_redis_client

    requested = max(0, int(cost or 0))
    if requested == 0:
        return True
    client = get_redis_client()
    if client is None:
        return None
    rate = max(0.001, float(rate_per_minute) / 60.0)
    capacity = max(float(requested), float(burst if burst is not None else rate_per_minute))
    capacity = min(capacity, 10_000_000.0)
    ttl_ms = max(1000, int(ttl_seconds) * 1000)
    try:
        seconds, micros = client.time()
        now = float(seconds) + float(micros) / 1_000_000
        result = client.eval(
            _TOKEN_LUA,
            1,
            str(key),
            rate,
            capacity,
            requested,
            ttl_ms,
            now,
        )
        return bool(int(result))
    except Exception:  # noqa: BLE001 - fail-soft boundary, caller decides
        return None


def reset_client() -> None:
    from src.core.redis_client import reset_client as _reset

    _reset()


__all__ = ["allow", "reset_client"]
