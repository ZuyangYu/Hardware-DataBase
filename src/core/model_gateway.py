"""Model access seam: one place for model construction, admission and usage.

Layering (agent/wiki/memory/authoring depend on this module, not on a provider
SDK directly):

- :class:`UsageLedger` -- process-wide token/call accounting per channel and
  model (the factory is the only construction entry)
  (authoring/external/ingestion).
- :class:`_GovernedChatOpenAI` / :class:`_GovernedChatOllama` -- real adapter
  classes that hold one governor slot for the duration of each generate/stream
  call, replacing the old post-construction class swap.
- :func:`build_chat_model` -- construction entry used by ``model_factory``.
  (memory worker's independent model settings).

Fail-open: any accounting/metadata failure must never break a model call.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from contextvars import copy_context
from collections import defaultdict
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

import src.settings as settings
from src.costs.capture import leaf_call
from src.core.cancellation import current_cancel_check
from src.core.llm_governor import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    get_llm_governor,
    CURRENT_LLM_PRIORITY,
)


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
        persist_usage_record(
            channel=str(channel or "unknown"),
            model=str(model or "unknown"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._total,
                "by_channel": {k: dict(v) for k, v in sorted(self._by_channel.items())},
                "by_model": {k: dict(v) for k, v in sorted(self._by_model.items())},
            }


_usage_ledger = UsageLedger()


_USAGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage_ledger (
    channel TEXT NOT NULL,
    model TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(channel, model)
)
"""


def _usage_db_path() -> str:
    return str(getattr(settings, "LLM_USAGE_LEDGER_DB", "") or settings.AUTH_DB_PATH)


def persist_usage_record(
    *, channel: str, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int
) -> None:
    """把一笔调用累加进 auth.db 的 llm_usage_ledger(fail-soft, 跨进程共享)。"""
    try:
        import os
        import sqlite3

        path = _usage_db_path()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute(_USAGE_TABLE_SQL)
            conn.execute(
                """
                INSERT INTO llm_usage_ledger
                    (channel, model, calls, prompt_tokens, completion_tokens, total_tokens, updated_at)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(channel, model) DO UPDATE SET
                    calls = calls + 1,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    total_tokens = total_tokens + excluded.total_tokens,
                    updated_at = excluded.updated_at
                """,
                (channel, model, prompt_tokens, completion_tokens, total_tokens, _utc_now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 账本落库绝不阻塞调用
        pass


def get_persisted_usage() -> dict[str, Any]:
    """读取跨进程累计用量(重启不清零); 不可用时返回空。"""
    try:
        import sqlite3

        conn = sqlite3.connect(_usage_db_path(), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT channel, model, calls, prompt_tokens, completion_tokens, total_tokens "
                "FROM llm_usage_ledger ORDER BY channel, model"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {}
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    by_channel: dict[str, dict[str, int]] = {}
    for row in rows:
        data = dict(row)
        channel = str(data.pop("channel"))
        data.pop("model", None)
        bucket = by_channel.setdefault(channel, dict(total))
        for key in total:
            bucket[key] += int(data.get(key) or 0)
            total[key] += int(data.get(key) or 0)
    return {"total": total, "by_channel": by_channel}


def _iter_with_idle_timeout(
    iterator: Any,
    timeout_seconds: float,
    *,
    should_cancel: Any = None,
) -> Any:
    """迭代 LLM 流并施加空闲超时: 超过 N 秒没有新块则中断, 防止挂死吃满槽位。

    ``should_cancel`` is polled while waiting for the next provider chunk.  A
    Stop request therefore releases the model governor slot immediately, even
    when the provider is in a long reasoning gap and has not produced a chunk
    that the agent loop could check.
    """
    timeout_seconds = float(timeout_seconds or 0)
    if timeout_seconds <= 0 and should_cancel is None:
        yield from iterator
        return
    import queue as _queue
    import threading as _threading
    import time as _time

    box: "_queue.Queue[Any]" = _queue.Queue(maxsize=16)
    sentinel = object()
    stopped = _threading.Event()

    def _close_iterator() -> None:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - teardown must stay fail-soft
                pass

    def pump() -> None:
        try:
            for item in iterator:
                if stopped.is_set():
                    return
                while not stopped.is_set():
                    try:
                        box.put(item, timeout=0.5)
                        break
                    except _queue.Full:
                        continue
        except Exception as exc:  # noqa: BLE001
            if not stopped.is_set():
                try:
                    box.put(exc, timeout=0.5)
                except _queue.Full:
                    pass
        finally:
            if stopped.is_set():
                _close_iterator()
                return
            try:
                box.put(sentinel, timeout=0.5)
            except _queue.Full:
                pass

    from contextvars import copy_context

    context = copy_context()
    _threading.Thread(target=lambda: context.run(pump), daemon=True, name="llm-stream-pump").start()
    deadline = _time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    try:
        while True:
            if should_cancel is not None and should_cancel():
                return
            wait_seconds = 0.25
            if deadline is not None:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    stopped.set()
                    _close_iterator()
                    raise TimeoutError(
                        f"LLM stream idle for more than {timeout_seconds:.0f}s"
                    ) from None
                wait_seconds = min(wait_seconds, remaining)
            try:
                item = box.get(timeout=max(0.01, wait_seconds))
            except _queue.Empty:
                continue
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


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


def _estimated_call_tokens(model: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
    """Bound one provider request for TPM admission before it is sent.

    Provider SDKs expose usage only after completion.  The preflight estimate
    therefore uses the serialized prompt size plus the configured output cap;
    it is deliberately conservative and only affects deployments that enable
    ``PROVIDER_TPM_LIMIT``.
    """
    try:
        messages = args[0] if args else kwargs.get("messages", "")
        prompt_chars = len(repr(messages))
        output_cap = int(getattr(model, "max_tokens", 0) or 0)
        return max(1, (prompt_chars + 3) // 4 + max(0, output_cap))
    except Exception:  # noqa: BLE001 - quota estimation must not break setup
        return 0


@asynccontextmanager
async def _async_slot(cm, priority):
    """Enter/exit the sync governor in the same Context, off the event loop.

    Cancellation during acquisition still drains the in-flight acquisition and
    releases its permit. Two to_thread calls otherwise get distinct contexts,
    making ContextVar.reset fail and leaking the governance scope.
    """
    context = copy_context()
    acquisition = asyncio.create_task(asyncio.to_thread(context.run, cm.__enter__))
    try:
        await asyncio.shield(acquisition)
    except BaseException:
        try:
            await asyncio.shield(acquisition)
        except Exception:
            pass
        else:
            await asyncio.shield(asyncio.to_thread(context.run, cm.__exit__, None, None, None))
        raise
    token = CURRENT_LLM_PRIORITY.set(priority)
    try:
        yield
    finally:
        CURRENT_LLM_PRIORITY.reset(token)
        await asyncio.shield(asyncio.to_thread(context.run, cm.__exit__, None, None, None))


class _GovernedChatOpenAI(ChatOpenAI):
    hdb_priority: str = PRIORITY_INTERACTIVE
    hdb_provider: str = "openai-compatible"
    hdb_model: str = ""

    def _generate(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(
            self.hdb_priority,
            provider=self.hdb_provider,
            model=self.hdb_model,
            estimated_tokens=_estimated_call_tokens(self, args, kwargs),
        ):
            with leaf_call(self) as call:
                result = super()._generate(*args, **kwargs)
                if call:
                    call.response = result
                return result

    def _stream(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(
            self.hdb_priority,
            provider=self.hdb_provider,
            model=self.hdb_model,
            estimated_tokens=_estimated_call_tokens(self, args, kwargs),
        ):
            timeout = float(getattr(settings, "LLM_STREAM_IDLE_TIMEOUT_SECONDS", 0) or 0)
            with leaf_call(self) as call:
                for chunk in _iter_with_idle_timeout(
                    super()._stream(*args, **kwargs),
                    timeout,
                    should_cancel=current_cancel_check(),
                ):
                    if call:
                        call.chunk(chunk)
                    yield chunk

    async def _agenerate(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        cm = get_llm_governor().slot(
            self.hdb_priority,
            provider=self.hdb_provider,
            model=self.hdb_model,
            estimated_tokens=_estimated_call_tokens(self, args, kwargs),
        )
        async with _async_slot(cm, self.hdb_priority):
            with leaf_call(self) as call:
                result = await super()._agenerate(*args, **kwargs)
                if call:
                    call.response = result
                return result

    async def _astream(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        cm = get_llm_governor().slot(
            self.hdb_priority,
            provider=self.hdb_provider,
            model=self.hdb_model,
            estimated_tokens=_estimated_call_tokens(self, args, kwargs),
        )
        async with _async_slot(cm, self.hdb_priority):
            with leaf_call(self) as call:
                async for chunk in super()._astream(*args, **kwargs):
                    if call:
                        call.chunk(chunk)
                    yield chunk


class _GovernedChatOllama(ChatOllama):
    hdb_priority: str = PRIORITY_INTERACTIVE
    hdb_provider: str = "ollama"
    hdb_model: str = ""

    def _generate(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(
            self.hdb_priority,
            provider=self.hdb_provider,
            model=self.hdb_model,
            estimated_tokens=_estimated_call_tokens(self, args, kwargs),
        ):
            with leaf_call(self) as call:
                result = super()._generate(*args, **kwargs)
                if call:
                    call.response = result
                return result

    def _stream(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        with get_llm_governor().slot(
            self.hdb_priority,
            provider=self.hdb_provider,
            model=self.hdb_model,
            estimated_tokens=_estimated_call_tokens(self, args, kwargs),
        ):
            timeout = float(getattr(settings, "LLM_STREAM_IDLE_TIMEOUT_SECONDS", 0) or 0)
            with leaf_call(self) as call:
                for chunk in _iter_with_idle_timeout(
                    super()._stream(*args, **kwargs),
                    timeout,
                    should_cancel=current_cancel_check(),
                ):
                    if call:
                        call.chunk(chunk)
                    yield chunk

    async def _agenerate(self, *args: Any, **kwargs: Any):
        cm = get_llm_governor().slot(self.hdb_priority, provider=self.hdb_provider, model=self.hdb_model,
                                     estimated_tokens=_estimated_call_tokens(self, args, kwargs))
        async with _async_slot(cm, self.hdb_priority):
            with leaf_call(self) as call:
                result = await super()._agenerate(*args, **kwargs)
                if call:
                    call.response = result
                return result

    async def _astream(self, *args: Any, **kwargs: Any):
        cm = get_llm_governor().slot(self.hdb_priority, provider=self.hdb_provider, model=self.hdb_model,
                                     estimated_tokens=_estimated_call_tokens(self, args, kwargs))
        async with _async_slot(cm, self.hdb_priority):
            with leaf_call(self) as call:
                async for chunk in super()._astream(*args, **kwargs):
                    if call:
                        call.chunk(chunk)
                    yield chunk


def build_governed_model(
    *,
    provider: str,
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    reasoning_effort: str = "",
    max_retries: int = 4,
    timeout: int = 120,
    base_url: str = "",
    api_key: str = "",
    default_headers: dict[str, str] | None = None,
    client_kwargs: dict[str, Any] | None = None,
    priority: str = PRIORITY_INTERACTIVE,
) -> Any:
    """显式参数版受治理模型构造: 工厂 (create_chat_model) 的唯一构建入口。

    真实 adapter 类(非构造后换类), 闸门与用量账本都在 adapter 内生效。
    """
    callbacks = [_UsageLedgerCallback(channel=priority, model=model)]
    if provider == "ollama":
        kwargs = dict(client_kwargs or {})
        kwargs.setdefault("timeout", timeout)
        return _GovernedChatOllama(
            model=model,
            base_url=base_url or None,
            temperature=temperature,
            client_kwargs=kwargs,
            callbacks=callbacks,
            hdb_priority=priority,
            hdb_provider="ollama",
            hdb_model=model,
        )
    return _GovernedChatOpenAI(
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort or None,
        max_retries=max_retries,
        timeout=timeout,
        default_headers=default_headers or None,
        callbacks=callbacks,
        hdb_priority=priority,
        hdb_provider="openai-compatible",
        hdb_model=model,
    )


def build_chat_model(
    *,
    provider: str = "",
    model: str = "",
    priority: str = PRIORITY_INTERACTIVE,
) -> Any:
    """settings 版壳: 解析 AGENT_* 后调用 :func:`build_governed_model`。"""
    provider = (provider or str(settings.AGENT_LLM_PROVIDER)).lower()
    model_name = model or (
        str(settings.AGENT_OLLAMA_MODEL) if provider == "ollama" else str(settings.AGENT_CUSTOM_MODEL)
    )
    if provider == "ollama":
        return build_governed_model(
            provider="ollama",
            model=model_name,
            temperature=float(settings.AGENT_TEMPERATURE),
            max_tokens=int(settings.AGENT_CUSTOM_MAX_TOKENS),
            timeout=int(settings.AGENT_TIMEOUT_SECONDS),
            base_url=str(settings.AGENT_OLLAMA_BASE_URL),
            priority=priority,
        )
    return build_governed_model(
        provider="custom",
        model=model_name,
        temperature=float(settings.AGENT_TEMPERATURE),
        max_tokens=int(settings.AGENT_CUSTOM_MAX_TOKENS),
        reasoning_effort=str(settings.AGENT_REASONING_EFFORT),
        max_retries=int(settings.AGENT_RATE_LIMIT_MAX_RETRIES),
        timeout=int(settings.AGENT_TIMEOUT_SECONDS),
        base_url=str(settings.AGENT_CUSTOM_BASE_URL),
        api_key=str(settings.AGENT_CUSTOM_API_KEY),
        default_headers=_extra_headers(settings.AGENT_CUSTOM_BASE_URL) or None,
        priority=priority,
    )


def _extra_headers(base_url: object) -> dict[str, str]:
    try:
        from src.core.llm_headers import opencode_extra_headers

        return opencode_extra_headers(base_url)
    except Exception:  # noqa: BLE001
        return {}


def _record_ragas_usage(result: Any) -> None:
    """尽力从 RAGAS LLMResult 提取 token 用量; 提取不到也要记调用次数。"""
    usage: Any = None
    llm_output = getattr(result, "llm_output", None)
    if isinstance(llm_output, dict):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
    elif isinstance(llm_output, str):
        usage = None
    model = "ragas-judge"
    prompt = completion = total = 0
    if usage is not None:
        if isinstance(usage, dict):
            model = str(usage.get("model") or model)
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total = int(usage.get("total_tokens") or 0)
        else:
            model = str(getattr(usage, "model", None) or model)
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            total = int(getattr(usage, "total_tokens", 0) or 0)
    get_usage_ledger().record(
        channel=PRIORITY_BATCH, model=model,
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
        usage_returned=usage is not None,
    )


_GOVERNANCE_LOCK = threading.Lock()
_GOVERNANCE_STATUS: dict[str, Any] = {
    "assembled": False,
    "bypassed": False,
    "reason": "",
    "wrapped_methods": [],
}


def governance_status() -> dict[str, Any]:
    """Last judge-LLM governance assembly status (P0-006 acceptance surface).

    ``bypassed`` means the wrapper could not be installed; evaluation results
    produced in that state must be marked ``governance_bypassed`` and are not
    valid for production capacity/quality acceptance.
    """
    with _GOVERNANCE_LOCK:
        return dict(_GOVERNANCE_STATUS)


def reset_governance_status() -> None:
    with _GOVERNANCE_LOCK:
        _GOVERNANCE_STATUS.update(
            {"assembled": False, "bypassed": False, "reason": "", "wrapped_methods": []}
        )


def _mark_governance(assembled: bool, reason: str, wrapped: list[str]) -> None:
    with _GOVERNANCE_LOCK:
        _GOVERNANCE_STATUS.update(
            {"assembled": assembled, "bypassed": not assembled, "reason": reason, "wrapped_methods": wrapped}
        )
    if not assembled:
        try:
            from src.observability.metrics import counter

            counter(
                "hdb.eval.governance_bypassed",
                attributes={"reason": str(reason or "unknown")[:80]},
                description="Judge-LLM governance could not be assembled (results marked bypassed)",
            )
        except Exception:
            pass


def govern_ragas_llm(llm: Any) -> Any:
    """把 RAGAS judge 纳入闸门。

    不能在 client 层包代理: ragas 的 instructor 适配器要求真 ``AsyncOpenAI``
    实例(isinstance 校验)。因此在 LLM 对象的 generate/agenerate 层包裹,
    让评测(批量)与在线问答共享准入控制。

    装配成功/失败都会记录显式治理状态（``governance_status()``）；失败时
    fail-open 返回原对象，但评测结果必须标记 ``governance_bypassed``。
    """
    wrapped: list[str] = []
    try:
        for name in ("agenerate", "generate"):
            original = getattr(llm, name, None)
            if original is None or getattr(original, "_hdb_governed", False):
                if original is not None:
                    wrapped.append(name)
                continue
            if asyncio.iscoroutinefunction(original):
                async def wrapped_async(*args: Any, _orig=original, **kwargs: Any) -> Any:
                    async with _async_slot(get_llm_governor().slot(PRIORITY_BATCH), PRIORITY_BATCH):
                        result = await _orig(*args, **kwargs)
                    _record_ragas_usage(result)
                    return result

                target = wrapped_async
            else:
                def wrapped_sync(*args: Any, _orig=original, **kwargs: Any) -> Any:
                    with get_llm_governor().slot(PRIORITY_BATCH):
                        result = _orig(*args, **kwargs)
                    _record_ragas_usage(result)
                    return result

                target = wrapped_sync
            target._hdb_governed = True  # type: ignore[attr-defined]
            setattr(llm, name, target)
            wrapped.append(name)
        if not wrapped:
            _mark_governance(False, "no_generate_method", wrapped)
        else:
            _mark_governance(True, "", wrapped)
        return llm
    except Exception as exc:  # noqa: BLE001 - 治理层绝不阻塞评测
        _mark_governance(False, f"{type(exc).__name__}: {exc}", wrapped)
        return llm


__all__ = [
    "PRIORITY_BATCH",
    "PRIORITY_INTERACTIVE",
    "UsageLedger",
    "build_chat_model",
    "build_governed_model",
    "get_persisted_usage",
    "get_usage_ledger",
    "govern_ragas_llm",
    "governance_status",
    "reset_governance_status",
    "reset_usage_ledger",
]
