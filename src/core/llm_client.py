from __future__ import annotations

from dataclasses import dataclass, field
import json
import random
import time
from collections.abc import Iterable
from typing import Any, Callable, Generator

import requests

import config.settings as settings


ChatPayload = dict[str, str]


def _iter_sse_data_events(lines: Iterable[str]) -> Generator[str, None, None]:
    """Yield complete SSE data payloads from a line iterator.

    Some OpenAI-compatible providers split one JSON object across physical
    lines. Treat blank lines as event boundaries, but keep accumulating until
    the buffered data is valid JSON or a [DONE] sentinel.
    """
    event_lines: list[str] = []

    def candidate_payloads() -> tuple[str, ...]:
        newline_payload = "\n".join(event_lines).strip()
        joined_payload = "".join(event_lines).strip()
        if joined_payload and joined_payload != newline_payload:
            return newline_payload, joined_payload
        return (newline_payload,)

    def complete_payload() -> str:
        for payload in candidate_payloads():
            if payload == "[DONE]":
                return payload
            try:
                json.loads(payload)
            except json.JSONDecodeError:
                continue
            return payload
        return ""

    def flush() -> str:
        nonlocal event_lines
        payload = complete_payload() or "".join(event_lines).strip()
        event_lines = []
        return payload

    for raw_line in lines:
        if raw_line is None:
            continue
        line = str(raw_line).rstrip("\r")
        if not line:
            if complete_payload():
                payload = flush()
                if payload:
                    yield payload
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:]
            if line.startswith(" "):
                line = line[1:]
        event_lines.append(line)
        if complete_payload():
            yield flush()

    payload = flush()
    if payload:
        yield payload


@dataclass(frozen=True)
class LLMClientConfig:
    provider: settings.Provider
    base_url: str
    model: str
    api_key: str = ""
    max_tokens: int = 4096
    timeout: int = 120
    temperature: float = 0.2
    rate_limit_max_retries: int = 4
    rate_limit_initial_delay_seconds: float = 1.0
    rate_limit_max_delay_seconds: float = 16.0
    fallback_model: str = "deepseek-ai/DeepSeek-V4-Pro"


@dataclass(frozen=True)
class LLMUsageRecord:
    stage: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_returned: bool = False


@dataclass(frozen=True)
class LLMUsageSummary:
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    usage_returned_count: int = 0
    by_stage: dict[str, "LLMUsageSummary"] = field(default_factory=dict)

    @property
    def has_usage(self) -> bool:
        return self.usage_returned_count > 0


class LLMClient:
    """Project-owned chat client for agent answer generation.

    This intentionally stays framework-neutral and does not depend on the
    document retrieval backend implementation.
    """

    def __init__(self, config: LLMClientConfig | None = None):
        self.config = config
        self._usage_records: list[LLMUsageRecord] = []

    def reset_usage(self) -> None:
        self._usage_records = []

    def get_usage_records(self) -> tuple[LLMUsageRecord, ...]:
        return tuple(self._usage_records)

    def get_usage_summary(self) -> LLMUsageSummary:
        return _summarize_usage(self._usage_records)

    def chat(self, messages: list[ChatPayload], **kwargs: Any) -> str:
        config = self.config or self._from_runtime_settings()
        self._validate_messages(messages)
        if config.provider == settings.Provider.OLLAMA:
            return self._chat_ollama(config, messages, **kwargs)
        if config.provider == settings.Provider.CUSTOM:
            return self._chat_openai_compatible(config, messages, **kwargs)
        raise ValueError(f"Unsupported provider: {config.provider}")

    def stream_chat(self, messages: list[ChatPayload], **kwargs: Any) -> Generator[str, None, str]:
        """Stream chat deltas and return the assembled response when exhausted."""
        config = self.config or self._from_runtime_settings()
        self._validate_messages(messages)
        if config.provider == settings.Provider.OLLAMA:
            return self._stream_chat_ollama(config, messages, **kwargs)
        if config.provider == settings.Provider.CUSTOM:
            return self._stream_chat_openai_compatible(config, messages, **kwargs)
        raise ValueError(f"Unsupported provider: {config.provider}")

    @staticmethod
    def _from_runtime_settings() -> LLMClientConfig:
        if settings.AGENT_LLM_PROVIDER == settings.Provider.OLLAMA:
            return LLMClientConfig(
                provider=settings.Provider.OLLAMA,
                base_url=settings.AGENT_OLLAMA_BASE_URL,
                model=settings.AGENT_OLLAMA_MODEL,
                max_tokens=settings.AGENT_CUSTOM_MAX_TOKENS,
                timeout=settings.AGENT_TIMEOUT_SECONDS,
                temperature=settings.AGENT_TEMPERATURE,
            )
        return LLMClientConfig(
            provider=settings.Provider.CUSTOM,
            base_url=settings.AGENT_CUSTOM_BASE_URL,
            model=settings.AGENT_CUSTOM_MODEL,
            api_key=settings.AGENT_CUSTOM_API_KEY,
            max_tokens=settings.AGENT_CUSTOM_MAX_TOKENS,
            timeout=settings.AGENT_TIMEOUT_SECONDS,
            temperature=settings.AGENT_TEMPERATURE,
            rate_limit_max_retries=settings.AGENT_RATE_LIMIT_MAX_RETRIES,
            rate_limit_initial_delay_seconds=settings.AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS,
            rate_limit_max_delay_seconds=settings.AGENT_RATE_LIMIT_MAX_DELAY_SECONDS,
            fallback_model=settings.AGENT_FALLBACK_MODEL,
        )

    @staticmethod
    def _validate_messages(messages: list[ChatPayload]) -> None:
        if not messages:
            raise ValueError("messages cannot be empty")
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"}:
                raise ValueError(f"Invalid chat role: {role}")
            if not isinstance(content, str):
                raise ValueError("Chat message content must be a string")

    def _chat_ollama(self, config: LLMClientConfig, messages: list[ChatPayload], **kwargs: Any) -> str:
        if not config.base_url:
            raise ValueError("AGENT_OLLAMA_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_OLLAMA_MODEL is required")
        payload = {
            "model": config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", config.temperature),
                "num_predict": kwargs.get("max_tokens", config.max_tokens),
            },
        }
        response = requests.post(
            f"{config.base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=kwargs.get("timeout", config.timeout),
        )
        response.raise_for_status()
        data = response.json()
        self._record_usage(config, kwargs.get("usage_stage"), _extract_ollama_usage(data))
        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            raise RuntimeError(f"Ollama returned an empty response: {data}")
        return content

    def _stream_chat_ollama(
        self,
        config: LLMClientConfig,
        messages: list[ChatPayload],
        **kwargs: Any,
    ) -> Generator[str, None, str]:
        if not config.base_url:
            raise ValueError("AGENT_OLLAMA_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_OLLAMA_MODEL is required")
        payload = {
            "model": config.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": kwargs.get("temperature", config.temperature),
                "num_predict": kwargs.get("max_tokens", config.max_tokens),
            },
        }
        response = requests.post(
            f"{config.base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=kwargs.get("timeout", config.timeout),
            stream=True,
        )
        response.raise_for_status()
        # `text/event-stream` without a charset defaults to ISO-8859-1 in
        # requests, corrupting UTF-8 model deltas before JSON parsing.
        response.encoding = "utf-8"
        parts: list[str] = []
        usage: dict[str, int] | None = None
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            data = json.loads(line)
            if data.get("done"):
                usage = _extract_ollama_usage(data)
            delta = ((data.get("message") or {}).get("content") or "")
            if delta:
                parts.append(delta)
                yield delta
            if data.get("done"):
                break
        self._record_usage(config, kwargs.get("usage_stage"), usage)
        content = "".join(parts).strip()
        if not content:
            raise RuntimeError("Ollama returned an empty streamed response")
        return content

    def _chat_openai_compatible(self, config: LLMClientConfig, messages: list[ChatPayload], **kwargs: Any) -> str:
        if not config.base_url:
            raise ValueError("AGENT_CUSTOM_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_CUSTOM_MODEL is required")
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        payload = {
            "messages": messages,
            "temperature": kwargs.get("temperature", config.temperature),
            "max_tokens": kwargs.get("max_tokens", config.max_tokens),
        }
        response = self._request_openai_compatible_with_rate_limit_fallback(
            config,
            lambda model: requests.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={"model": model, **payload},
                timeout=kwargs.get("timeout", config.timeout),
            ),
        )
        data = response.json()
        self._record_usage(config, kwargs.get("usage_stage"), _extract_openai_usage(data.get("usage")))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Chat API returned no choices: {data}")
        content = (((choices[0].get("message") or {}).get("content")) or "").strip()
        if not content:
            raise RuntimeError(f"Chat API returned an empty response: {data}")
        return content

    def _stream_chat_openai_compatible(
        self,
        config: LLMClientConfig,
        messages: list[ChatPayload],
        **kwargs: Any,
    ) -> Generator[str, None, str]:
        if not config.base_url:
            raise ValueError("AGENT_CUSTOM_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_CUSTOM_MODEL is required")
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        payload = {
            "messages": messages,
            "temperature": kwargs.get("temperature", config.temperature),
            "max_tokens": kwargs.get("max_tokens", config.max_tokens),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        def request_stream(model: str) -> Any:
            model_payload = {"model": model, **payload}
            response = requests.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=model_payload,
                timeout=kwargs.get("timeout", config.timeout),
                stream=True,
            )
            if not _should_retry_without_stream_options(response):
                return response
            retry_payload = dict(model_payload)
            retry_payload.pop("stream_options", None)
            return requests.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=retry_payload,
                timeout=kwargs.get("timeout", config.timeout),
                stream=True,
            )

        response = self._request_openai_compatible_with_rate_limit_fallback(config, request_stream)
        # OpenAI-compatible SSE responses are UTF-8 even when their
        # Content-Type header omits an explicit charset.
        response.encoding = "utf-8"
        parts: list[str] = []
        usage: dict[str, int] | None = None
        for event_data in _iter_sse_data_events(response.iter_lines(decode_unicode=True)):
            if event_data == "[DONE]":
                break
            data = json.loads(event_data)
            if data.get("usage"):
                usage = _extract_openai_usage(data.get("usage"))
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = ((choices[0].get("delta") or {}).get("content") or "")
            if delta:
                parts.append(delta)
                yield delta
        self._record_usage(config, kwargs.get("usage_stage"), usage)
        content = "".join(parts).strip()
        if not content:
            raise RuntimeError("Chat API returned an empty streamed response")
        return content

    def _request_openai_compatible_with_rate_limit_fallback(
        self,
        config: LLMClientConfig,
        request_for_model: Callable[[str], Any],
    ) -> Any:
        last_rate_limit_error: requests.HTTPError | None = None
        retries = max(0, int(config.rate_limit_max_retries))
        for model in _model_attempt_order(config):
            for attempt in range(retries + 1):
                response = request_for_model(model)
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    if not _is_rate_limited(response):
                        raise
                    last_rate_limit_error = exc
                    if attempt < retries:
                        time.sleep(_rate_limit_delay_seconds(config, attempt, response))
                        continue
                    break
                return response
        if last_rate_limit_error is not None:
            raise last_rate_limit_error
        raise RuntimeError("OpenAI-compatible request did not select a model")

    def _record_usage(self, config: LLMClientConfig, stage: Any, usage: dict[str, int] | None) -> None:
        prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
        completion_tokens = int((usage or {}).get("completion_tokens") or 0)
        total_tokens = int((usage or {}).get("total_tokens") or 0)
        if usage and total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        self._usage_records.append(
            LLMUsageRecord(
                stage=str(stage or "unknown"),
                provider=str(config.provider.value if hasattr(config.provider, "value") else config.provider),
                model=config.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                usage_returned=usage is not None,
            )
        )


def _extract_openai_usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    prompt_tokens = _int_usage_value(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion_tokens = _int_usage_value(usage.get("completion_tokens", usage.get("output_tokens")))
    total_tokens = _int_usage_value(usage.get("total_tokens"))
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _extract_ollama_usage(data: Any) -> dict[str, int] | None:
    if not isinstance(data, dict):
        return None
    if "prompt_eval_count" not in data and "eval_count" not in data:
        return None
    prompt_tokens = _int_usage_value(data.get("prompt_eval_count"))
    completion_tokens = _int_usage_value(data.get("eval_count"))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _int_usage_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _should_retry_without_stream_options(response: Any) -> bool:
    status_code = int(getattr(response, "status_code", 0) or 0)
    body = str(getattr(response, "text", "") or "").lower()
    return status_code in {400, 422} and "stream_options" in body


def _is_rate_limited(response: Any) -> bool:
    return int(getattr(response, "status_code", 0) or 0) == 429


def _retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _rate_limit_delay_seconds(config: LLMClientConfig, attempt: int, response: Any) -> float:
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        return retry_after
    initial_delay = max(0.0, float(config.rate_limit_initial_delay_seconds))
    max_delay = max(0.0, float(config.rate_limit_max_delay_seconds))
    delay = min(max_delay, initial_delay * (2 ** max(0, attempt)))
    return min(max_delay, delay + (random.random() * delay * 0.25))


def _model_attempt_order(config: LLMClientConfig) -> tuple[str, ...]:
    primary = str(config.model or "").strip()
    fallback = str(config.fallback_model or "").strip()
    if fallback and fallback != primary:
        return primary, fallback
    return (primary,)


def _summarize_usage(records: Iterable[LLMUsageRecord]) -> LLMUsageSummary:
    record_list = list(records)
    by_stage: dict[str, LLMUsageSummary] = {}
    for stage in sorted({record.stage for record in record_list}):
        stage_records = [record for record in record_list if record.stage == stage]
        by_stage[stage] = _summarize_usage_flat(stage_records)
    summary = _summarize_usage_flat(record_list)
    return LLMUsageSummary(
        provider=summary.provider,
        model=summary.model,
        prompt_tokens=summary.prompt_tokens,
        completion_tokens=summary.completion_tokens,
        total_tokens=summary.total_tokens,
        call_count=summary.call_count,
        usage_returned_count=summary.usage_returned_count,
        by_stage=by_stage,
    )


def _summarize_usage_flat(records: list[LLMUsageRecord]) -> LLMUsageSummary:
    providers = {record.provider for record in records if record.provider}
    models = {record.model for record in records if record.model}
    return LLMUsageSummary(
        provider=next(iter(providers)) if len(providers) == 1 else ("mixed" if providers else ""),
        model=next(iter(models)) if len(models) == 1 else ("mixed" if models else ""),
        prompt_tokens=sum(record.prompt_tokens for record in records),
        completion_tokens=sum(record.completion_tokens for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        call_count=len(records),
        usage_returned_count=sum(1 for record in records if record.usage_returned),
    )
