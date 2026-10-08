"""Privacy policy for telemetry attributes and structured logs."""

from __future__ import annotations

import re
import traceback
from collections.abc import Mapping
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
    r"(?i)([\"']?(?:[\w.-]*[_-])?"
    r"(?:api[_-]?key|apikey|authorization|password|secret|token|cookie|set-cookie)"
    r"[\"']?\s*[:=]\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)"
)
_AUTH_SCHEME_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+")
_COOKIE_HEADER_PATTERN = re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+")
_URL_PASSWORD_PATTERN = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^/@\s]+(?=@)")


def is_secret_key(key: str) -> bool:
    normalized = key.rsplit(".", 1)[-1].replace("-", "_").lower()
    return normalized in {item.replace("-", "_") for item in _ALWAYS_REDACTED} or normalized.endswith(
        ("_api_key", "_apikey", "_password", "_secret", "_token")
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
    if is_secret_key(key):
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
                value = redact_text(value, max_chars=limit)
        elif isinstance(raw_value, (list, tuple)):
            value = [str(redact_value(item))[:200] for item in raw_value[:50]]
        else:
            value = str(redact_value(raw_value))[:2000]
        safe[key] = value
    return safe


def redact_text(value: object, *, max_chars: int = 8000) -> str:
    """Redact common credential-shaped values from log messages."""

    text = str(value or "")
    text = _COOKIE_HEADER_PATTERN.sub(r"\1[redacted]", text)
    text = _AUTH_SCHEME_PATTERN.sub(lambda match: f"{match.group(1)} [redacted]", text)
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _URL_PASSWORD_PATTERN.sub(r"\1[redacted]", text)
    return text[:max_chars]


def redact_value(value: Any, *, depth: int = 0) -> Any:
    """Sanitize structured log extras without exposing nested credentials."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, Mapping):
        return {
            str(key): redact_value(item, depth=depth + 1)
            for key, item in value.items()
            if not is_secret_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


def exception_attributes(exc: BaseException) -> dict[str, str]:
    """Preserve exception diagnostics while sanitizing message and traceback."""
    return {
        "exception.type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "exception.message": redact_text(str(exc)),
        "exception.stacktrace": redact_text("".join(traceback.format_exception(exc))),
    }
