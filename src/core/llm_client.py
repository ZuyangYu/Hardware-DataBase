from __future__ import annotations

from dataclasses import dataclass, field
import contextvars
import json
import random
import time
from collections.abc import Iterable
from typing import Any, Callable, Generator

import requests

import config.settings as settings
from src.observability import observe
from src.observability.metrics import record_llm


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
    # Vendors differ in tool-calling support. "auto" is the most universal and is
    # ON by default; the retry ladder falls back to no-tools (structured JSON in
    # content) only when a vendor rejects tools entirely. Streaming is always on.
    tool_choice: str = "auto"
    tool_streaming: bool = True


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
class ChatToolResult:
    """Result of a tool-calling chat completion.

    ``tool_calls`` is a list of ``{"id","name","arguments"}`` dicts (arguments
    parsed to a dict when possible) when the model returned native tool calls.
    It is ``None`` when the provider rejected ``tools=`` and we fell back to a
    plain completion -- in that case the caller should parse structured output
    out of ``content`` itself.
    """

    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_supported: bool = True


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


# LLMClient is a process-wide singleton (owned by the singleton
# AppPipeline/MultiSourceAgentRunner), so per-instance usage records would be
# shared across concurrent Streamlit sessions: one user's chat() could append
# to / reset / read another user's records. Scoping the list to an execution
# context (ContextVar) gives each thread/session its own list with no API
# change to reset_usage / get_usage_summary / _record_usage.
_USAGE_RECORDS: contextvars.ContextVar[list[LLMUsageRecord] | None] = contextvars.ContextVar(
    "llm_client_usage_records", default=None
)


class LLMClient:
    """Project-owned chat client for agent answer generation.

    This intentionally stays framework-neutral and does not depend on the
    document retrieval backend implementation.
    """

    def __init__(self, config: LLMClientConfig | None = None):
        self.config = config
        # Reset this context's usage log so a freshly constructed client
        # (incl. one built per test) never inherits stale records left in the
        # current thread's context. Goes through the property setter below.
        self._usage_records = []

    @property
    def _usage_records(self) -> list[LLMUsageRecord]:
        records = _USAGE_RECORDS.get()
        if records is None:
            records = []
            _USAGE_RECORDS.set(records)
        return records

    @_usage_records.setter
    def _usage_records(self, value: list[LLMUsageRecord]) -> None:
        _USAGE_RECORDS.set(list(value))

    def reset_usage(self) -> None:
        self._usage_records = []

    def get_usage_records(self) -> tuple[LLMUsageRecord, ...]:
        return tuple(self._usage_records)

    def get_usage_summary(self) -> LLMUsageSummary:
        return _summarize_usage(self._usage_records)

    def chat(self, messages: list[ChatPayload], **kwargs: Any) -> str:
        config = self.config or self._from_runtime_settings()
        self._validate_messages(messages)
        provider = _provider_name(config)
        started = time.monotonic()
        before_count = len(self._usage_records)
        status = "success"
        with observe.llm(
            "hdb.llm.chat",
            provider=provider,
            model=config.model,
            stage=kwargs.get("usage_stage", "chat"),
            streaming=False,
        ) as observation:
            _set_llm_input(observation, messages)
            try:
                if config.provider == settings.Provider.OLLAMA:
                    result = self._chat_ollama(config, messages, **kwargs)
                elif config.provider == settings.Provider.CUSTOM:
                    result = self._chat_openai_compatible(config, messages, **kwargs)
                else:
                    raise ValueError(f"Unsupported provider: {config.provider}")
                _set_usage_attributes(observation, self._usage_records[before_count:])
                _set_llm_output(observation, result)
                return result
            except Exception:
                status = "error"
                raise
            finally:
                record_llm(
                    provider=provider,
                    status=status,
                    duration_s=time.monotonic() - started,
                    streaming=False,
                )

    def stream_chat(self, messages: list[ChatPayload], **kwargs: Any) -> Generator[str, None, str]:
        """Stream chat deltas and return the assembled response when exhausted."""
        config = self.config or self._from_runtime_settings()
        self._validate_messages(messages)
        if config.provider == settings.Provider.OLLAMA:
            stream = self._stream_chat_ollama(config, messages, **kwargs)
        elif config.provider == settings.Provider.CUSTOM:
            stream = self._stream_chat_openai_compatible(config, messages, **kwargs)
        else:
            raise ValueError(f"Unsupported provider: {config.provider}")
        return self._observe_stream(stream, config, kwargs, operation="hdb.llm.chat", messages=messages)

    def chat_with_tools(
        self,
        messages: list[ChatPayload],
        *,
        tools: list[dict[str, Any]],
        tool_choice: Any = None,
        usage_stage: Any = None,
        **kwargs: Any,
    ) -> ChatToolResult:
        """Non-streaming chat completion that asks the model to call a function tool.

        Used by the agent's structured-decision nodes (planner / sufficiency
        judge / next-round planner) so they get native structured output instead
        of parsing free-text JSON. If the provider rejects ``tools=``, we retry
        once without tools and return ``tool_calls=None`` so the caller can fall
        back to JSON-text parsing of ``content`` -- the agent never hard-breaks.
        """
        config = self.config or self._from_runtime_settings()
        self._validate_messages(messages)
        provider = _provider_name(config)
        started = time.monotonic()
        before_count = len(self._usage_records)
        status = "success"
        with observe.llm(
            "hdb.llm.chat_with_tools",
            provider=provider,
            model=config.model,
            stage=usage_stage or "tool_call",
            streaming=False,
        ) as observation:
            _set_llm_input(observation, messages, tools=tools)
            try:
                if config.provider == settings.Provider.OLLAMA:
                    result = self._chat_tools_ollama(config, messages, tools, tool_choice, usage_stage, **kwargs)
                elif config.provider == settings.Provider.CUSTOM:
                    result = self._chat_tools_openai_compatible(
                        config, messages, tools, tool_choice, usage_stage, **kwargs
                    )
                else:
                    raise ValueError(f"Unsupported provider: {config.provider}")
                _set_usage_attributes(observation, self._usage_records[before_count:])
                _set_llm_output(observation, result)
                return result
            except Exception:
                status = "error"
                raise
            finally:
                record_llm(
                    provider=provider,
                    status=status,
                    duration_s=time.monotonic() - started,
                    streaming=False,
                )

    def stream_chat_with_tools(
        self,
        messages: list[ChatPayload],
        *,
        tools: list[dict[str, Any]],
        tool_choice: Any = None,
        usage_stage: Any = None,
        on_delta: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> ChatToolResult:
        """Streaming variant of chat_with_tools for the agent's reasoning nodes.

        Content deltas are forwarded to ``on_delta`` in real time (so the agent
        can emit a live ``thought`` stream) while tool_call argument fragments
        are accumulated across chunks. Returns a ChatToolResult just like
        chat_with_tools. If the provider rejects streaming+tools, falls back to
        the non-streaming chat_with_tools (no thought stream, agent still works).
        """
        config = self.config or self._from_runtime_settings()
        self._validate_messages(messages)
        provider = _provider_name(config)
        started = time.monotonic()
        before_count = len(self._usage_records)
        status = "success"
        first_delta_at: float | None = None

        def observed_delta(delta: str) -> None:
            nonlocal first_delta_at
            if first_delta_at is None:
                first_delta_at = time.monotonic()
            if on_delta is not None:
                on_delta(delta)

        with observe.llm(
            "hdb.llm.chat_with_tools",
            provider=provider,
            model=config.model,
            stage=usage_stage or "tool_call",
            streaming=True,
        ) as observation:
            _set_llm_input(observation, messages, tools=tools)
            try:
                if config.provider == settings.Provider.OLLAMA:
                    result = self._stream_chat_tools_ollama(
                        config, messages, tools, tool_choice, usage_stage, observed_delta, **kwargs
                    )
                elif config.provider == settings.Provider.CUSTOM:
                    result = self._stream_chat_tools_openai_compatible(
                        config, messages, tools, tool_choice, usage_stage, observed_delta, **kwargs
                    )
                else:
                    raise ValueError(f"Unsupported provider: {config.provider}")
                _set_usage_attributes(observation, self._usage_records[before_count:])
                _set_llm_output(observation, result)
                return result
            except Exception:
                status = "error"
                raise
            finally:
                record_llm(
                    provider=provider,
                    status=status,
                    duration_s=time.monotonic() - started,
                    streaming=True,
                    ttft_s=(first_delta_at - started if first_delta_at is not None else None),
                )

    def _observe_stream(
        self,
        stream: Generator[str, None, Any] | Any,
        config: LLMClientConfig,
        kwargs: dict[str, Any],
        *,
        operation: str,
        messages: list[ChatPayload] | None = None,
        result_stream: bool = True,
    ) -> Generator[str, None, Any]:
        """Keep a streaming span open until the provider generator is exhausted."""

        def observed() -> Generator[str, None, Any]:
            provider = _provider_name(config)
            started = time.monotonic()
            first_token_at: float | None = None
            before_count = len(self._usage_records)
            status = "success"
            with observe.llm(
                operation,
                provider=provider,
                model=config.model,
                stage=kwargs.get("usage_stage", "chat"),
                streaming=True,
            ) as observation:
                _set_llm_input(observation, messages or [])
                try:
                    if result_stream:
                        result: Any = None
                        while True:
                            try:
                                delta = next(stream)
                            except StopIteration as stop:
                                result = stop.value
                                break
                            if first_token_at is None:
                                first_token_at = time.monotonic()
                            yield delta
                    else:
                        # Tool streaming uses callbacks internally and returns a
                        # ChatToolResult rather than yielding content deltas.
                        result = stream
                    _set_usage_attributes(observation, self._usage_records[before_count:])
                    _set_llm_output(observation, result)
                    return result
                except Exception:
                    status = "error"
                    raise
                finally:
                    record_llm(
                        provider=provider,
                        status=status,
                        duration_s=time.monotonic() - started,
                        streaming=True,
                        ttft_s=(
                            first_token_at - started
                            if first_token_at is not None
                            else None
                        ),
                    )

        return observed()

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
        # requests defaults to 512-byte chunks here, which delays short model
        # deltas until the buffer fills and makes a streaming UI look frozen.
        for line in response.iter_lines(chunk_size=1, decode_unicode=True):
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
            rate_limit_max_retries=kwargs.get("rate_limit_max_retries"),
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
        # Keep upstream SSE token latency intact; the default 512-byte buffer
        # turns short answers into a single end-of-stream burst.
        for event_data in _iter_sse_data_events(response.iter_lines(chunk_size=1, decode_unicode=True)):
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

    def _chat_tools_openai_compatible(
        self,
        config: LLMClientConfig,
        messages: list[ChatPayload],
        tools: list[dict[str, Any]],
        tool_choice: Any,
        usage_stage: Any,
        **kwargs: Any,
    ) -> ChatToolResult:
        if not config.base_url:
            raise ValueError("AGENT_CUSTOM_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_CUSTOM_MODEL is required")
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        base_payload = {
            "messages": messages,
            "temperature": kwargs.get("temperature", config.temperature),
            "max_tokens": kwargs.get("max_tokens", config.max_tokens),
        }

        def _post(payload: dict[str, Any]) -> Any:
            return requests.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=kwargs.get("timeout", config.timeout),
            )

        def request_choice(choice: Any) -> Callable[[str], Any]:
            def _req(model: str) -> Any:
                payload = {"model": model, **base_payload, "tools": tools}
                if choice is not None:
                    payload["tool_choice"] = choice
                return _post(payload)

            return _req

        def request_no_tools(model: str) -> Any:
            return _post({"model": model, **base_payload})

        # Vendor-agnostic tool_choice ladder: try the preferred choice, then "auto"
        # (Ark/others reject required/object form), then drop tools entirely so the
        # caller parses structured JSON out of content. No vendor hardcoding.
        preferred = tool_choice if tool_choice is not None else config.tool_choice
        attempts = [request_choice(preferred)]
        if preferred != "auto":
            attempts.append(request_choice("auto"))
        attempts.append(request_no_tools)

        response = None
        used_no_tools = False
        for index, attempt in enumerate(attempts):
            try:
                response = self._request_openai_compatible_with_rate_limit_fallback(config, attempt)
                used_no_tools = index == len(attempts) - 1
                break
            except requests.HTTPError as exc:
                if not _is_tools_rejected(getattr(exc, "response", None)):
                    raise
        if response is None:
            raise RuntimeError("all tool-calling attempts rejected by provider")

        data = response.json()
        self._record_usage(config, usage_stage, _extract_openai_usage(data.get("usage")))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Chat API returned no choices: {data}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        raw_tool_calls = message.get("tool_calls") or []
        tool_calls = [_parse_tool_call(tc) for tc in raw_tool_calls] if raw_tool_calls else None
        return ChatToolResult(content=content, tool_calls=tool_calls, tool_call_supported=not used_no_tools)

    def _chat_tools_ollama(
        self,
        config: LLMClientConfig,
        messages: list[ChatPayload],
        tools: list[dict[str, Any]],
        tool_choice: Any,
        usage_stage: Any,
        **kwargs: Any,
    ) -> ChatToolResult:
        if not config.base_url:
            raise ValueError("AGENT_OLLAMA_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_OLLAMA_MODEL is required")
        payload = {
            "model": config.model,
            "messages": messages,
            "stream": False,
            "tools": tools,
            "options": {
                "temperature": kwargs.get("temperature", config.temperature),
                "num_predict": kwargs.get("max_tokens", config.max_tokens),
            },
        }
        try:
            response = requests.post(
                f"{config.base_url.rstrip('/')}/api/chat",
                json=payload,
                timeout=kwargs.get("timeout", config.timeout),
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            if _is_tools_rejected(getattr(exc, "response", None)):
                payload.pop("tools", None)
                response = requests.post(
                    f"{config.base_url.rstrip('/')}/api/chat",
                    json=payload,
                    timeout=kwargs.get("timeout", config.timeout),
                )
                response.raise_for_status()
                data = response.json()
                self._record_usage(config, usage_stage, _extract_ollama_usage(data))
                content = ((data.get("message") or {}).get("content") or "").strip()
                return ChatToolResult(content=content, tool_calls=None, tool_call_supported=False)
            raise
        data = response.json()
        self._record_usage(config, usage_stage, _extract_ollama_usage(data))
        message = data.get("message") or {}
        content = (message.get("content") or "").strip()
        raw_tool_calls = message.get("tool_calls") or []
        tool_calls = [_parse_tool_call(tc) for tc in raw_tool_calls] if raw_tool_calls else None
        return ChatToolResult(content=content, tool_calls=tool_calls)

    def _stream_chat_tools_openai_compatible(
        self,
        config: LLMClientConfig,
        messages: list[ChatPayload],
        tools: list[dict[str, Any]],
        tool_choice: Any,
        usage_stage: Any,
        on_delta: Callable[[str], None] | None,
        **kwargs: Any,
    ) -> ChatToolResult:
        if not config.base_url:
            raise ValueError("AGENT_CUSTOM_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_CUSTOM_MODEL is required")
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        base_payload = {
            "messages": messages,
            "temperature": kwargs.get("temperature", config.temperature),
            "max_tokens": kwargs.get("max_tokens", config.max_tokens),
        }

        def _post_stream(payload: dict[str, Any]) -> Any:
            response = requests.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=kwargs.get("timeout", config.timeout),
                stream=True,
            )
            if not _should_retry_without_stream_options(response):
                return response
            payload.pop("stream_options", None)
            return requests.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=kwargs.get("timeout", config.timeout),
                stream=True,
            )

        def request_choice(choice: Any) -> Callable[[str], Any]:
            def _req(model: str) -> Any:
                payload = {
                    "model": model,
                    **base_payload,
                    "tools": tools,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if choice is not None:
                    payload["tool_choice"] = choice
                return _post_stream(payload)

            return _req

        def request_no_tools(model: str) -> Any:
            return _post_stream(
                {"model": model, **base_payload, "stream": True, "stream_options": {"include_usage": True}}
            )

        # Same vendor-agnostic ladder as the non-streaming path.
        preferred = tool_choice if tool_choice is not None else config.tool_choice
        attempts = [request_choice(preferred)]
        if preferred != "auto":
            attempts.append(request_choice("auto"))
        attempts.append(request_no_tools)

        response = None
        used_no_tools = False
        for index, attempt in enumerate(attempts):
            try:
                response = self._request_openai_compatible_with_rate_limit_fallback(config, attempt)
                used_no_tools = index == len(attempts) - 1
                break
            except requests.HTTPError as exc:
                if not _is_tools_rejected(getattr(exc, "response", None)):
                    raise
        if response is None:
            # Streaming+tools rejected entirely: fall back to non-streaming so the
            # agent still gets a structured decision (JSON-in-content path).
            return self._chat_tools_openai_compatible(
                config, messages, tools, tool_choice, usage_stage, **kwargs
            )

        response.encoding = "utf-8"
        parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        usage: dict[str, int] | None = None
        for event_data in _iter_sse_data_events(response.iter_lines(chunk_size=1, decode_unicode=True)):
            if event_data == "[DONE]":
                break
            data = json.loads(event_data)
            if data.get("usage"):
                usage = _extract_openai_usage(data.get("usage"))
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content") or ""
            if content:
                parts.append(content)
                if on_delta is not None:
                    on_delta(content)
            for tc in delta.get("tool_calls") or []:
                index = int(tc.get("index", 0) or 0)
                acc = tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    acc["id"] = str(tc["id"])
                function = tc.get("function") or {}
                if function.get("name"):
                    acc["name"] = str(function["name"])
                if function.get("arguments"):
                    acc["arguments"] += str(function["arguments"])
        self._record_usage(config, usage_stage, usage)
        content = "".join(parts).strip()
        tool_calls = (
            [
                _parse_tool_call(
                    {"id": acc["id"], "function": {"name": acc["name"], "arguments": acc["arguments"]}}
                )
                for _, acc in sorted(tool_acc.items())
            ]
            if tool_acc
            else None
        )
        return ChatToolResult(content=content, tool_calls=tool_calls, tool_call_supported=not used_no_tools)

    def _stream_chat_tools_ollama(
        self,
        config: LLMClientConfig,
        messages: list[ChatPayload],
        tools: list[dict[str, Any]],
        tool_choice: Any,
        usage_stage: Any,
        on_delta: Callable[[str], None] | None,
        **kwargs: Any,
    ) -> ChatToolResult:
        if not config.base_url:
            raise ValueError("AGENT_OLLAMA_BASE_URL is required")
        if not config.model:
            raise ValueError("AGENT_OLLAMA_MODEL is required")
        payload = {
            "model": config.model,
            "messages": messages,
            "stream": True,
            "tools": tools,
            "options": {
                "temperature": kwargs.get("temperature", config.temperature),
                "num_predict": kwargs.get("max_tokens", config.max_tokens),
            },
        }
        try:
            response = requests.post(
                f"{config.base_url.rstrip('/')}/api/chat",
                json=payload,
                timeout=kwargs.get("timeout", config.timeout),
                stream=True,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            if _is_tools_rejected(getattr(exc, "response", None)):
                return self._chat_tools_ollama(config, messages, tools, tool_choice, usage_stage, **kwargs)
            raise
        response.encoding = "utf-8"
        parts: list[str] = []
        tool_calls: list[dict[str, Any]] | None = None
        usage: dict[str, int] | None = None
        for line in response.iter_lines(chunk_size=1, decode_unicode=True):
            if not line:
                continue
            data = json.loads(line)
            message = data.get("message") or {}
            content = message.get("content") or ""
            if content:
                parts.append(content)
                if on_delta is not None:
                    on_delta(content)
            if message.get("tool_calls"):
                tool_calls = [_parse_tool_call(tc) for tc in message["tool_calls"]]
            if data.get("done"):
                usage = _extract_ollama_usage(data)
                break
        self._record_usage(config, usage_stage, usage)
        content = "".join(parts).strip()
        return ChatToolResult(content=content, tool_calls=tool_calls, tool_call_supported=True)

    def _request_openai_compatible_with_rate_limit_fallback(
        self,
        config: LLMClientConfig,
        request_for_model: Callable[[str], Any],
        *,
        rate_limit_max_retries: int | None = None,
    ) -> Any:
        last_rate_limit_error: requests.HTTPError | None = None
        last_connection_error: requests.RequestException | None = None
        retries = max(
            0,
            int(config.rate_limit_max_retries if rate_limit_max_retries is None else rate_limit_max_retries),
        )
        for model in _model_attempt_order(config):
            for attempt in range(retries + 1):
                try:
                    response = request_for_model(model)
                except requests.RequestException as exc:
                    last_connection_error = exc
                    if attempt < retries:
                        time.sleep(_rate_limit_delay_seconds(config, attempt, None))
                        continue
                    break
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
        if last_connection_error is not None:
            raise last_connection_error
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


def _provider_name(config: LLMClientConfig) -> str:
    return str(config.provider.value if hasattr(config.provider, "value") else config.provider)


def _set_usage_attributes(observation: Any, records: Iterable[LLMUsageRecord]) -> None:
    records = tuple(records)
    if not records:
        return
    observation.tokens(
        input_tokens=sum(record.prompt_tokens for record in records),
        output_tokens=sum(record.completion_tokens for record in records),
    )


def _set_llm_input(observation: Any, messages: list[ChatPayload], tools: list[dict[str, Any]] | None = None) -> None:
    payload: dict[str, Any] = {"messages": messages}
    if tools:
        payload["tools"] = tools
    observation.set_input(payload, content_kind="llm")


def _set_llm_output(observation: Any, result: Any) -> None:
    if isinstance(result, ChatToolResult):
        result = {
            "content": result.content,
            "tool_calls": result.tool_calls or [],
            "tool_call_supported": result.tool_call_supported,
        }
    observation.set_output(result, content_kind="llm")


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


def _is_tools_rejected(response: Any) -> bool:
    """True when a 400/422 likely stems from the tools/tool_choice/stream_options params.

    Providers such as Volcengine Ark return a generic ``InvalidParameter`` 400 without
    naming "tools", so the tool-choice retry ladder treats any 400/422 on a tools request
    as rejection and advances to the next attempt (choice -> auto -> no-tools). Genuine
    errors (bad model/key) 400 without tools too, so the no-tools retry still fails and raises.
    """
    if response is None:
        return False
    status_code = int(getattr(response, "status_code", 0) or 0)
    return status_code in {400, 422}


def _parse_tool_call(tool_call: Any) -> dict[str, Any]:
    """Normalize an OpenAI/Ollama tool_call into {"id","name","arguments"}."""
    if not isinstance(tool_call, dict):
        return {"id": "", "name": "", "arguments": {}}
    function = tool_call.get("function") or {}
    raw_args = function.get("arguments")
    if isinstance(raw_args, str):
        try:
            arguments = json.loads(raw_args)
        except json.JSONDecodeError:
            arguments = {"_raw": raw_args}
    elif isinstance(raw_args, dict):
        arguments = raw_args
    else:
        arguments = {}
    return {
        "id": str(tool_call.get("id") or ""),
        "name": str(function.get("name") or ""),
        "arguments": arguments,
    }


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
    # Only the user-configured model is ever used — no cross-model fallback.
    return (str(config.model or "").strip(),)


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
