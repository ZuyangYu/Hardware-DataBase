"""T0 POC for the MCP external-plugin plan.

Gate (plan T0): before any business integration, prove in this repository that

1. ``langchain-mcp-adapters`` + ``mcp`` resolve against the locked toolchain;
2. tool discovery works over ``streamable_http``;
3. ``ToolAnnotations.readOnlyHint`` survives the adapter conversion (metadata);
4. adapter tools are async-only, so a sync bridge is required;
5. a minimal background-loop bridge can call tools, time out and cancel
   without leaking the loop thread.

If any of these fail, the plan switches to a direct MCP SDK wrapper instead of
the adapter.  The bridge below is a throwaway prototype: the production
version lands in ``src/agents/mcp/client.py`` (plan T3).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import unittest

from tests._mcp_stub import McpStubServer


class _PocAsyncRuntime:
    """Minimal background-loop sync bridge prototype."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mcp-poc-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("poc runtime loop did not start")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro, *, timeout: float = 10.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    @property
    def thread_alive(self) -> bool:
        return self._thread.is_alive()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
        if not self._loop.is_closed():
            self._loop.close()


class McpPocTests(unittest.TestCase):
    def _load_tools(self, runtime: _PocAsyncRuntime, url: str):
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(
            {"stub": {"url": url, "transport": "streamable_http"}}
        )
        return runtime.run(client.get_tools(), timeout=20)

    def test_poc_discovery_annotations_async_only_and_sync_bridge(self) -> None:
        with McpStubServer() as stub:
            runtime = _PocAsyncRuntime()
            try:
                tools = self._load_tools(runtime, stub.url)
                by_name = {tool.name: tool for tool in tools}
                self.assertEqual(
                    {"echo", "big_text", "slow", "writer", "dup_name"},
                    set(by_name),
                )

                # 3) annotation fidelity through the adapter metadata
                self.assertIs(by_name["echo"].metadata.get("readOnlyHint"), True)
                self.assertIs(by_name["big_text"].metadata.get("readOnlyHint"), True)
                writer_meta = by_name["writer"].metadata or {}
                self.assertFalse(writer_meta.get("readOnlyHint"))

                # 4) adapter tools are async-only: sync invoke must fail
                with self.assertRaises(Exception):
                    by_name["echo"].invoke({"text": "hi"})

                # 5) sync bridge happy path
                result = runtime.run(
                    by_name["echo"].ainvoke({"text": "hello-bridge"}), timeout=10
                )
                self.assertIn("hello-bridge", str(result))

                # 5) timeout cancels the in-flight coroutine and returns control
                started = time.monotonic()
                with self.assertRaises(concurrent.futures.TimeoutError):
                    runtime.run(
                        by_name["slow"].ainvoke({"seconds": 5}), timeout=0.5
                    )
                self.assertLess(time.monotonic() - started, 4.0)

                # the loop is still usable after a cancelled call
                again = runtime.run(
                    by_name["echo"].ainvoke({"text": "after-cancel"}), timeout=10
                )
                self.assertIn("after-cancel", str(again))
            finally:
                runtime.shutdown()
            self.assertFalse(runtime.thread_alive)


if __name__ == "__main__":
    unittest.main()
