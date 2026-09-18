"""Provider-level RPM quota (v3 §9.3).

Entry-request admission cannot stand in for provider quota: one request may
produce several model calls (agent loop, retries).  Every underlying request
passes through the LLM governor, which consumes a shared per-provider/model
bucket here when ``PROVIDER_RPM_LIMIT`` is set.  A disabled limit returns
``True`` without touching Redis; an unavailable limiter returns ``None`` so
the governor can fail closed.
"""

from __future__ import annotations


def allow_provider_call(provider: str, model: str) -> bool | None:
    import src.settings as settings

    limit = int(getattr(settings, "PROVIDER_RPM_LIMIT", 0) or 0)
    if limit <= 0:
        return True
    from .distributed_limiter import allow

    key = f"hdb:quota:provider:{provider or 'default'}:{model or 'default'}"
    return allow(
        key,
        rate_per_minute=limit,
        burst=max(1.0, min(float(limit), 10.0)),
        ttl_seconds=120,
    )


def allow_provider_tokens(provider: str, model: str, tokens: int) -> bool | None:
    """Consume a provider/model TPM budget at the real model-call boundary."""
    import src.settings as settings

    limit = int(getattr(settings, "PROVIDER_TPM_LIMIT", 0) or 0)
    requested = max(0, int(tokens or 0))
    if limit <= 0 or requested == 0:
        return True
    from .distributed_limiter import allow_tokens

    key = f"hdb:quota:provider-tokens:{provider or 'default'}:{model or 'default'}"
    return allow_tokens(
        key,
        rate_per_minute=limit,
        cost=requested,
        burst=max(float(requested), min(float(limit), 100_000.0)),
        ttl_seconds=120,
    )


__all__ = ["allow_provider_call", "allow_provider_tokens"]
