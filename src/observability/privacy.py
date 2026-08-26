"""Privacy policy for telemetry attributes and structured logs."""

from __future__ import annotations

import re
from typing import Any

import src.settings as settings


_ALWAYS_REDACTED = {
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "password",
    "secret",
    "set-cookie",
    "token",
}
_QUERY_KEYS = {"query", "original_query", "rewritten_query", "question", "user_query"}
_EVIDENCE_KEYS = {
    "content",
    "evidence",
    "retrieved_text",
    "document",
    "documents",
    "datasheet",
    "spreadsheet",
    "cell",
}
_LLM_KEYS = {"prompt", "completion", "input", "output", "messages", "response"}
_INPUT_OUTPUT_KEYS = {"input.value", "output.value"}
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|password|secret|token)\s*[:=]\s*([^\s,;]+)"
)


def allow_query_content() -> bool:
    return bool(settings.OBS_CAPTURE_CONTENT or settings.OBS_CAPTURE_QUERY)


def allow_evidence_content() -> bool:
    return bool(settings.OBS_CAPTURE_CONTENT or settings.OBS_CAPTURE_EVIDENCE)


def allow_llm_content() -> bool:
    return bool(settings.OBS_CAPTURE_CONTENT or settings.OBS_CAPTURE_LLM_CONTENT)


def allow_any_content() -> bool:
    return bool(
        settings.OBS_CAPTURE_CONTENT
        or settings.OBS_CAPTURE_QUERY
        or settings.OBS_CAPTURE_EVIDENCE
        or settings.OBS_CAPTURE_LLM_CONTENT
    )


def allow_content(kind: str) -> bool:
    normalized = str(kind or "any").lower()
    if normalized == "query":
        return allow_query_content()
    if normalized == "evidence":
        return allow_evidence_content()
    if normalized in {"llm", "answer", "model"}:
        return allow_llm_content()
    return allow_any_content()


def _key_kind(key: str) -> str:
    normalized = key.rsplit(".", 1)[-1].replace("-", "_").lower()
    if normalized in _ALWAYS_REDACTED:
        return "secret"
    if normalized in _QUERY_KEYS:
        return "query"
    if normalized in _EVIDENCE_KEYS:
        return "evidence"
    if normalized in _LLM_KEYS:
        return "llm"
    return "safe"


def safe_span_attributes(attrs: dict[str, Any] | None, *, content_kind: str | None = None) -> dict[str, Any]:
    """Return OTel-compatible attributes without user content by default."""

    safe: dict[str, Any] = {}
    for raw_key, raw_value in (attrs or {}).items():
        key = str(raw_key)
        kind = _key_kind(key)
        is_content_value = key.lower() in _INPUT_OUTPUT_KEYS or content_kind is not None
        if content_kind is not None and not allow_content(content_kind):
            continue
        if key.lower() in _INPUT_OUTPUT_KEYS:
            # ``input.value``/``output.value`` are deliberately opt-in. Callers
            # that know whether the value is a query, evidence, or LLM payload
            # pass ``content_kind`` so a query-only policy cannot accidentally
            # expose an answer or a prompt.
            if content_kind is None:
                continue
            kind = "content"
        if kind == "secret":
            continue
        if kind == "query" and not allow_query_content():
            continue
        if kind == "evidence" and not allow_evidence_content():
            continue
        if kind == "llm" and not allow_llm_content():
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, (str, bool, int, float)):
            value: Any = raw_value
            if isinstance(value, str):
                if is_content_value or kind in {"query", "evidence", "llm", "content"}:
                    limit = max(1000, int(getattr(settings, "OBS_CONTENT_MAX_CHARS", 50000)))
                else:
                    limit = 2000
                value = value[:limit]
        elif isinstance(raw_value, (list, tuple)):
            value = [str(item)[:200] for item in raw_value[:50]]
        else:
            value = str(raw_value)[:2000]
        safe[key] = value
    return safe


def redact_text(value: object) -> str:
    """Redact common credential-shaped values from log messages."""

    text = str(value or "")
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", text)[:8000]
