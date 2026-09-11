"""Model access seam: one place for model construction, admission and usage.

Layering (agent/wiki/memory/authoring depend on this module, not on a provider
SDK directly):

- :class:`UsageLedger` -- process-wide token/call accounting per channel and
  model, shared by the LangChain path (agent/wiki/assets) and ``LLMClient``
  (authoring/external/ingestion).
- :class:`_GovernedChatOpenAI` / :class:`_GovernedChatOllama` -- real adapter
  classes that hold one governor slot for the duration of each generate/stream
  call, replacing the old post-construction class swap.
- :func:`build_chat_model` -- construction entry used by ``model_factory``.
- :func:`govern_model` -- class-swap fallback for models built elsewhere
  (memory worker's independent model settings).

Fail-open: any accounting/metadata failure must never break a model call.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

import src.settings as settings
from src.core.llm_governor import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    get_llm_governor,
)

_GOVERNED_MARKER = "_HDBGoverned"


class UsageLedger:
    """Thread-safe cumulative token/call ledger (process-local)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    @staticmethod
    def _empty() -> dict[str, int]:
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "usage_returned_calls": 0,
        }

    def reset(self) -> None:
        with self._lock:
            self._total = self._empty()
            self._by_channel: dict[str, dict[str, int]] = defaultdict(self._empty)
            self._by_model: dict[str, dict[str, int]] = defaultdict(self._empty)

    def record(
        self,
        *,
        channel: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        usage_returned: bool = True,
    ) -> None:
        prompt_tokens = max(0, int(prompt_tokens or 0))
        completion_tokens = max(0, int(completion_tokens or 0))
        total_tokens = max(0, int(total_tokens or 0))
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        entry = {
            "calls": 1,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "usage_returned_calls": 1 if usage_returned else 0,
        }
        with self._lock:
            for bucket in (self._total, self._by_channel[str(channel or "unknown")], self._by_model[str(model or "unknown")]):
                for key, value in entry.items():
                    bucket[key] += value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._total,
                "by_channel": {k: dict(v) for k, v in sorted(self._by_channel.items())},
                "by_model": {k: dict(v) for k, v in sorted(self._by_model.items())},
            }


_usage_ledger = UsageLedger()


def get_usage_ledger() -> UsageLedger:
    return _usage_ledger


def reset_usage_ledger() -> None:
    """Test helper."""
    _usage_ledger.reset()


class _UsageLedgerCallback(BaseCallbackHandler):
    """Records token usage reported by any LangChain model call (invoke/stream)."""

    def __init__(self, *, channel: str, model: str) -> None:
        self.channel = channel
        self.model = model

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ANN401
        prompt, completion, total = _usage_from_llm_result(response)
        try:
            get_usage_ledger().record(
                channel=self.channel,
                model=self.model or "unknown",
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                usage_returned=(prompt + completion + total) > 0,
            )
        except Exception:  # noqa: BLE001 - 记账绝不阻塞模型调用
            pass


def _usage_from_llm_result(response: Any) -> tuple[int, int, int]:
    """Best-effort token extraction from an LLMResult (provider shapes vary)."""
    llm_output = getattr(response, "llm_output", None) or {}
    token_usage = (
        llm_output.get("token_usage")
        or llm_output.get("usage")
        or llm_output.get("usage_metadata")
        or {}
    )
    prompt = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
    completion = int(token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0)
    total = int(token_usage.get("total_tokens") or 0)
    if prompt or completion or total:
        return prompt, completion, total
    generations = getattr(response, "generations", None) or []
    for group in generations:
        for generation in group or []:
            message = getattr(generation, "message", None)
            meta = getattr(message, "usage_metadata", None)
            if isinstance(meta, dict):
                prompt = int(meta.get("input_tokens") or 0)
                completion = int(meta.get("output_tokens") or 0)
                total = int(meta.get("total_tokens") or 0)
                if prompt or completion or total:
                    return prompt, completion, total
    return 0, 0, 0


def _model_name(model: Any) -> str:
    return str(getattr(model, "model_name", "") or getattr(model, "model", "") or "unknown")


class _GovernedChatOpenAI(ChatOpenAI):
    hdb_priority: str = PRIORITY_INTERACTIVE

    def _generate(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(self.hdb_priority):
            return super()._generate(*args, **kwargs)

    def _stream(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(self.hdb_priority):
            yield from super()._stream(*args, **kwargs)

    async def _agenerate(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        cm = get_llm_governor().slot(self.hdb_priority)
        await asyncio.to_thread(cm.__enter__)
        try:
            return await super()._agenerate(*args, **kwargs)
        finally:
            await asyncio.to_thread(cm.__exit__, None, None, None)

    async def _astream(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        cm = get_llm_governor().slot(self.hdb_priority)
        await asyncio.to_thread(cm.__enter__)
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        finally:
            await asyncio.to_thread(cm.__exit__, None, None, None)


class _GovernedChatOllama(ChatOllama):
    hdb_priority: str = PRIORITY_INTERACTIVE

    def _generate(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(self.hdb_priority):
            return super()._generate(*args, **kwargs)

    def _stream(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(self.hdb_priority):
            yield from super()._stream(*args, **kwargs)


def build_chat_model(
    *,
    provider: str = "",
    model: str = "",
    priority: str = PRIORITY_INTERACTIVE,
) -> Any:
    """Construct a governed LangChain chat model from AGENT_* settings.

    Mirrors what ``init_chat_model`` produced before, but as a real adapter
    class (no post-construction ``__class__`` swap) and with a usage-ledger
    callback attached.
    """
    provider = (provider or str(settings.AGENT_LLM_PROVIDER)).lower()
    temperature = float(settings.AGENT_TEMPERATURE)
    max_retries = int(settings.AGENT_RATE_LIMIT_MAX_RETRIES)
    timeout = int(settings.AGENT_TIMEOUT_SECONDS)
    model_name = model or (
        str(settings.AGENT_OLLAMA_MODEL) if provider == "ollama" else str(settings.AGENT_CUSTOM_MODEL)
    )

    if provider == "ollama":
        return _GovernedChatOllama(
            model=model_name,
            base_url=str(settings.AGENT_OLLAMA_BASE_URL),
            temperature=temperature,
            client_kwargs={"timeout": timeout},
            callbacks=[_UsageLedgerCallback(channel=priority, model=model_name)],
            hdb_priority=priority,
        )

    return _GovernedChatOpenAI(
        model=model_name,
        base_url=str(settings.AGENT_CUSTOM_BASE_URL) or None,
        api_key=str(settings.AGENT_CUSTOM_API_KEY),
        temperature=temperature,
        max_tokens=int(settings.AGENT_CUSTOM_MAX_TOKENS),
        max_retries=max_retries,
        timeout=timeout,
        default_headers=_extra_headers(settings.AGENT_CUSTOM_BASE_URL) or None,
        callbacks=[_UsageLedgerCallback(channel=priority, model=model_name)],
        hdb_priority=priority,
    )


def _extra_headers(base_url: object) -> dict[str, str]:
    try:
        from src.core.llm_headers import opencode_extra_headers

        return opencode_extra_headers(base_url)
    except Exception:  # noqa: BLE001
        return {}


# -- fallback wrapping for models built outside build_chat_model ------------

_GOVERNED_CLASSES: dict[tuple[type, str], type] = {}


def _governed_class(base: type, priority: str) -> type:
    key = (base, priority)
    cached = _GOVERNED_CLASSES.get(key)
    if cached is not None:
        return cached

    def _generate(self, *args, **kwargs):  # noqa: ANN001
        with get_llm_governor().slot(priority):
            return base._generate(self, *args, **kwargs)

    def _stream(self, *args, **kwargs):  # noqa: ANN001
        with get_llm_governor().slot(priority):
            yield from base._stream(self, *args, **kwargs)

    async def _agenerate(self, *args, **kwargs):  # noqa: ANN001
        cm = get_llm_governor().slot(priority)
        await asyncio.to_thread(cm.__enter__)
        try:
            return await base._agenerate(self, *args, **kwargs)
        finally:
            await asyncio.to_thread(cm.__exit__, None, None, None)

    async def _astream(self, *args, **kwargs):  # noqa: ANN001
        cm = get_llm_governor().slot(priority)
        await asyncio.to_thread(cm.__enter__)
        try:
            async for chunk in base._astream(self, *args, **kwargs):
                yield chunk
        finally:
            await asyncio.to_thread(cm.__exit__, None, None, None)

    governed = type(
        f"{_GOVERNED_MARKER}{base.__name__.lstrip('_')}_{priority}",
        (base,),
        {
            "__module__": __name__,
            "_generate": _generate,
            "_stream": _stream,
            "_agenerate": _agenerate,
            "_astream": _astream,
        },
    )
    _GOVERNED_CLASSES[key] = governed
    return governed


def govern_model(model: Any, priority: str = PRIORITY_INTERACTIVE) -> Any:
    """Route a model built elsewhere through the governor (fail-open swap)."""
    if isinstance(model, (_GovernedChatOpenAI, _GovernedChatOllama)):
        return model
    if type(model).__name__.startswith(_GOVERNED_MARKER):
        return model
    try:
        model.__class__ = _governed_class(type(model), priority)
    except Exception:  # noqa: BLE001 - 治理层绝不阻塞主流程
        return model
    return model


__all__ = [
    "PRIORITY_BATCH",
    "PRIORITY_INTERACTIVE",
    "UsageLedger",
    "build_chat_model",
    "get_usage_ledger",
    "govern_model",
    "reset_usage_ledger",
]
