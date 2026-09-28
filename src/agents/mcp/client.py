"""Async MCP runtime, tool discovery and the sync bridge.

HDB's agent loop is a synchronous generator, while adapter tools are
async-only (``StructuredTool(coroutine=...)``); this module owns the single
background event loop that bridges the two, plus the discovery path that feeds
the process-local tool cache.  Per-turn policy, events and truncation belong
to the policy/executor layers, never here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

import src.settings as settings
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.models import McpServerRecord
from src.agents.mcp.validation import McpValidationError, validate_url


class McpDiscoveryError(RuntimeError):
    """Discovery failed; ``kind`` feeds metrics and degraded events."""

    def __init__(self, message: str, *, kind: str = "protocol") -> None:
        super().__init__(message)
        self.kind = str(kind)


class McpRuntimeUnavailable(RuntimeError):
    """The background loop is not running (shutdown or failed start)."""


def mcp_http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """httpx client that ignores environment proxies and never follows redirects."""

    kwargs: dict[str, Any] = {
        "timeout": timeout or httpx.Timeout(30.0, read=300.0),
        "follow_redirects": False,
        "trust_env": False,
    }
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


class MCPAsyncRuntime:
    """Process singleton: one daemon thread owning one asyncio loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-async-runtime", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise McpRuntimeUnavailable("MCP runtime loop did not start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._closed

    def run(self, coro, *, timeout: float | None = None):
        """Run one coroutine on the background loop and wait synchronously.

        On timeout the underlying asyncio task is cancelled before the error
        propagates, so a slow server cannot keep the loop busy forever.
        """

        if not self._thread.is_alive():
            raise McpRuntimeUnavailable("MCP runtime is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise
        except concurrent.futures.CancelledError as exc:
            raise McpRuntimeUnavailable("MCP call was cancelled") from exc

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Cancel pending tasks, stop the loop and join the thread."""

        if self._closed:
            return
        if self._thread.is_alive():
            try:
                self.run(self._cancel_pending(), timeout=timeout)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout)
        self._closed = True
        if not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:
                pass

    async def _cancel_pending(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


_RUNTIME: MCPAsyncRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_mcp_runtime() -> MCPAsyncRuntime:
    """Return the process-wide runtime, creating it on first use."""

    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None or not _RUNTIME.alive:
            _RUNTIME = MCPAsyncRuntime()
        return _RUNTIME


def reset_mcp_runtime(*, timeout: float = 5.0) -> None:
    """Shut down and drop the process-wide runtime (lifespan/tests)."""

    global _RUNTIME
    with _RUNTIME_LOCK:
        runtime, _RUNTIME = _RUNTIME, None
    if runtime is not None:
        runtime.shutdown(timeout=timeout)


@dataclass(frozen=True)
class DiscoveredServerTools:
    """One server's discovered tools plus stats for the management view."""

    slug: str
    fingerprint: str
    tools: list[Any] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def read_only_tools(self) -> list[Any]:
        return [tool for tool in self.tools if tool_is_read_only(tool)]


def tool_is_read_only(tool: Any) -> bool:
    """Read the MCP ``readOnlyHint`` preserved in adapter metadata."""

    metadata = getattr(tool, "metadata", None) or {}
    return bool(metadata.get("readOnlyHint"))


def classify_mcp_error(exc: BaseException) -> str:
    if isinstance(exc, (concurrent.futures.TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, BaseExceptionGroup):
        kinds = [classify_mcp_error(inner) for inner in exc.exceptions]
        for kind in kinds:
            if kind != "protocol":
                return kind
        return "protocol"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connect"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "timeout"
    return "protocol"


def _discovery_error_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _discovery_error_message(exc.exceptions[0])
    text = str(exc or "").strip()
    return (text or type(exc).__name__)[:300]


async def _discover_tools(slug: str, config: dict[str, Any]) -> list[Any]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connection = {**config, "httpx_client_factory": mcp_http_client_factory}
    client = MultiServerMCPClient({slug: connection})
    return await client.get_tools()


async def discover_server_tools_async(
    record: McpServerRecord, *, force_refresh: bool = False
) -> DiscoveredServerTools:
    """Discover (or return cached) tools for one record, inside the loop.

    All failures are raised as :class:`McpDiscoveryError` so concurrent callers
    can degrade one server without touching the others.
    """

    if not record.credentials_ok:
        raise McpDiscoveryError(
            f"credentials unavailable: {record.headers_error}", kind="credentials"
        )
    try:
        url = validate_url(
            record.url,
            allow_insecure_http=bool(settings.MCP_ALLOW_INSECURE_HTTP),
            allow_private_hosts=bool(settings.MCP_ALLOW_PRIVATE_HOSTS),
        )
    except McpValidationError as exc:
        raise McpDiscoveryError(str(exc), kind="policy") from exc

    cache = get_tool_cache()
    if not force_refresh:
        cached = cache.get(record.slug, record.connection_fingerprint)
        if cached is not None:
            return DiscoveredServerTools(
                slug=cached.slug,
                fingerprint=cached.fingerprint,
                tools=list(cached.tools),
                stats=dict(cached.stats),
                from_cache=True,
            )

    config = {**record.runtime_config(), "url": url}
    try:
        tools = await _discover_tools(record.slug, config)
    except Exception as exc:  # noqa: BLE001 - classified into a structured error
        kind = classify_mcp_error(exc)
        raise McpDiscoveryError(
            f"MCP discovery failed ({kind}): {_discovery_error_message(exc)}", kind=kind
        ) from exc

    max_tools = int(settings.MCP_MAX_TOOLS_PER_SERVER)
    if len(tools) > max_tools:
        raise McpDiscoveryError(
            f"工具数 {len(tools)} 超过单服务器上限 {max_tools}", kind="tool_limit"
        )
    read_only = sum(1 for tool in tools if tool_is_read_only(tool))
    stats = {
        "total": len(tools),
        "read_only": read_only,
        "writable": len(tools) - read_only,
    }
    cache.put(
        cache.build_item(
            slug=record.slug,
            fingerprint=record.connection_fingerprint,
            tools=tools,
            stats=stats,
        )
    )
    return DiscoveredServerTools(
        slug=record.slug,
        fingerprint=record.connection_fingerprint,
        tools=list(tools),
        stats=stats,
        from_cache=False,
    )


def load_server_tools(
    record: McpServerRecord, *, force_refresh: bool = False
) -> DiscoveredServerTools:
    """Synchronous discovery for one server through the background loop."""

    runtime = get_mcp_runtime()
    try:
        return runtime.run(
            discover_server_tools_async(record, force_refresh=force_refresh),
            timeout=float(settings.MCP_CONNECT_TIMEOUT_SECONDS),
        )
    except McpRuntimeUnavailable as exc:
        raise McpDiscoveryError(str(exc), kind="runtime") from exc
    except McpDiscoveryError:
        raise
    except concurrent.futures.TimeoutError as exc:
        raise McpDiscoveryError(
            f"MCP discovery timed out after {settings.MCP_CONNECT_TIMEOUT_SECONDS}s", kind="timeout"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - classified into a structured error
        kind = classify_mcp_error(exc)
        raise McpDiscoveryError(
            f"MCP discovery failed ({kind}): {_discovery_error_message(exc)}", kind=kind
        ) from exc


def load_many_server_tools(
    records: list[McpServerRecord], *, force_refresh: bool = False
) -> list[DiscoveredServerTools | McpDiscoveryError]:
    """Concurrently discover many servers; failures stay per-server values.

    One slow/broken server must not delay or fail the others, so the gather is
    bounded by the connect timeout and returns ``McpDiscoveryError`` in place
    of the failed entry.
    """

    if not records:
        return []

    async def _gather():
        return await asyncio.gather(
            *(
                discover_server_tools_async(record, force_refresh=force_refresh)
                for record in records
            ),
            return_exceptions=True,
        )

    runtime = get_mcp_runtime()
    try:
        raw = runtime.run(
            _gather(), timeout=float(settings.MCP_CONNECT_TIMEOUT_SECONDS) + 5.0
        )
    except McpRuntimeUnavailable as exc:
        raise McpDiscoveryError(str(exc), kind="runtime") from exc
    except concurrent.futures.TimeoutError as exc:
        raise McpDiscoveryError(
            f"MCP discovery timed out after {settings.MCP_CONNECT_TIMEOUT_SECONDS}s", kind="timeout"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - classified into a structured error
        kind = classify_mcp_error(exc)
        raise McpDiscoveryError(
            f"MCP discovery failed ({kind}): {_discovery_error_message(exc)}", kind=kind
        ) from exc

    results: list[DiscoveredServerTools | McpDiscoveryError] = []
    for item in raw:
        if isinstance(item, McpDiscoveryError):
            results.append(item)
        elif isinstance(item, BaseException):
            kind = classify_mcp_error(item)
            results.append(
                McpDiscoveryError(
                    f"MCP discovery failed ({kind}): {_discovery_error_message(item)}", kind=kind
                )
            )
        else:
            results.append(item)
    return results


def sync_call_tool(runtime: MCPAsyncRuntime, tool: Any, arguments: dict[str, Any], *, timeout: float):
    """Invoke one async adapter tool from sync code via the background loop."""

    return runtime.run(tool.ainvoke(dict(arguments)), timeout=timeout)


def stringify_result(result: Any) -> str:
    """Normalize an MCP tool result into the text shown to the model.

    Adapter tools use ``response_format="content_and_artifact"``, so the raw
    return value is a ``(content, artifact)`` tuple.
    """

    content = result
    if isinstance(result, tuple) and len(result) == 2:
        content = result[0]
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


__all__ = [
    "DiscoveredServerTools",
    "MCPAsyncRuntime",
    "McpDiscoveryError",
    "McpRuntimeUnavailable",
    "classify_mcp_error",
    "discover_server_tools_async",
    "get_mcp_runtime",
    "load_many_server_tools",
    "load_server_tools",
    "mcp_http_client_factory",
    "reset_mcp_runtime",
    "stringify_result",
    "sync_call_tool",
    "tool_is_read_only",
]
