from __future__ import annotations

from dataclasses import dataclass
import json
from collections.abc import Iterable
from typing import Any, Generator

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


class LLMClient:
    """Project-owned chat client for agent answer generation.

    This intentionally stays framework-neutral and does not depend on the
    document retrieval backend implementation.
    """

    def __init__(self, config: LLMClientConfig | None = None):
        self.config = config

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
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            data = json.loads(line)
            delta = ((data.get("message") or {}).get("content") or "")
            if delta:
                parts.append(delta)
                yield delta
            if data.get("done"):
                break
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
            "model": config.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", config.temperature),
            "max_tokens": kwargs.get("max_tokens", config.max_tokens),
        }
        response = requests.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
            timeout=kwargs.get("timeout", config.timeout),
        )
        response.raise_for_status()
        data = response.json()
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
            "model": config.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", config.temperature),
            "max_tokens": kwargs.get("max_tokens", config.max_tokens),
            "stream": True,
        }
        response = requests.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
            timeout=kwargs.get("timeout", config.timeout),
            stream=True,
        )
        response.raise_for_status()
        # OpenAI-compatible SSE responses are UTF-8 even when their
        # Content-Type header omits an explicit charset.
        response.encoding = "utf-8"
        parts: list[str] = []
        for event_data in _iter_sse_data_events(response.iter_lines(decode_unicode=True)):
            if event_data == "[DONE]":
                break
            data = json.loads(event_data)
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = ((choices[0].get("delta") or {}).get("content") or "")
            if delta:
                parts.append(delta)
                yield delta
        content = "".join(parts).strip()
        if not content:
            raise RuntimeError("Chat API returned an empty streamed response")
        return content
