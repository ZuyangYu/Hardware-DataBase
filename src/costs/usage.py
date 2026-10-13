"""Explicit token buckets; missing is distinct from a provider-reported zero."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


def mapping(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {}


def token(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_status: str = "missing"

    def to_dict(self):
        return asdict(self)


def normalize(response: Any) -> Usage:
    raw = mapping(response)
    if raw.get("raw") is not None:
        return normalize(raw["raw"])
    generations = getattr(response, "generations", None)
    if generations:
        # ChatResult has one leaf response. LLMResult may wrap that in a list.
        generation = generations[0]
        if isinstance(generation, list):
            generation = generation[0] if generation else None
        return normalize(getattr(generation, "message", None))
    message = getattr(response, "message", None)
    if message is not None:
        return normalize(message)
    usage = mapping(getattr(response, "usage_metadata", None)) or mapping(raw.get("usage_metadata"))
    meta = mapping(getattr(response, "response_metadata", None)) or mapping(raw.get("response_metadata"))
    if not usage:
        usage = mapping(raw.get("usage")) or mapping(raw.get("token_usage")) or mapping(meta.get("token_usage")) or mapping(meta.get("usage"))
    if not usage:
        # Ollama raw responses.
        usage = {"input_tokens": raw.get("prompt_eval_count"), "output_tokens": raw.get("eval_count")}
    i = token(usage.get("input_tokens", usage.get("prompt_tokens")))
    o = token(usage.get("output_tokens", usage.get("completion_tokens")))
    inp = mapping(usage.get("input_token_details")) or mapping(usage.get("prompt_tokens_details"))
    out = mapping(usage.get("output_token_details")) or mapping(usage.get("completion_tokens_details"))
    cached = token(inp.get("cache_read", inp.get("cached_tokens")))
    written = token(inp.get("cache_creation", inp.get("cache_write")))
    reasoning = token(out.get("reasoning", out.get("reasoning_tokens")))
    invalid_detail = any(value is not None and token(value) is None for value in (
        inp.get("cache_read", inp.get("cached_tokens")), inp.get("cache_creation", inp.get("cache_write")),
        out.get("reasoning", out.get("reasoning_tokens")),
    ))
    # OpenAI cache/reasoning details are optional and zero when absent. Other
    # provider-specific cache semantics require a separate pricing adapter.
    if i is not None and cached is None and not inp:
        cached = 0
    if o is not None and reasoning is None and not out:
        reasoning = 0
    invalid_subset = (cached is not None and i is not None and cached > i) or (reasoning is not None and o is not None and reasoning > o)
    status = "reported" if i is not None and o is not None and not invalid_subset and not invalid_detail else "partial" if any(v is not None for v in (i, o, cached, written, reasoning)) else "missing"
    return Usage(i, o, cached, written, reasoning, status)


def response_identity(response: Any) -> dict:
    raw = mapping(response)
    if raw.get("raw") is not None:
        return response_identity(raw["raw"])
    generations = getattr(response, "generations", None)
    if generations:
        generation = generations[0]
        if isinstance(generation, list):
            generation = generation[0] if generation else None
        return response_identity(getattr(generation, "message", None))
    message = getattr(response, "message", None)
    if message is not None:
        return response_identity(message)
    meta = mapping(getattr(response, "response_metadata", None)) or mapping(raw.get("response_metadata"))
    return {"response_model": str(raw.get("model") or meta.get("model_name") or meta.get("model") or ""),
            "provider_request_id": str(raw.get("id") or meta.get("request_id") or "")}
