"""T3: discovery, connection-fingerprint cache and the sync bridge."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import src.settings as settings
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.client import (
    McpDiscoveryError,
    get_mcp_runtime,
    load_server_tools,
    reset_mcp_runtime,
    stringify_result,
    sync_call_tool,
    tool_is_read_only,
)
from src.agents.mcp.store import SqliteMcpServerStore
from tests._mcp_stub import McpStubServer


class McpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteMcpServerStore(os.path.join(self._tmp.name, "auth.db"))
        self.key = Fernet.generate_key().decode("ascii")
        self._patches = [
            patch.object(settings, "MCP_CREDENTIALS_KEY", self.key),
            patch.object(settings, "MCP_ALLOW_INSECURE_HTTP", True),
            patch.object(settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
            patch.object(settings, "MCP_CONNECT_TIMEOUT_SECONDS", 10),
            patch.object(settings, "MCP_CALL_TIMEOUT_SECONDS", 5),
            patch.object(settings, "MCP_MAX_TOOLS_PER_SERVER", 50),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        reset_mcp_runtime()
        get_tool_cache().clear_all()
        self._tmp.cleanup()

    def _record(self, url: str, slug: str = "stub"):
        return self.store.create(
            slug=slug,
            name="stub",
            transport="streamable_http",
            url=url,
            created_by="admin",
        )

    # ------------------------------------------------------------------
    def test_discovery_stats_and_read_only_annotation(self):
        with McpStubServer() as stub:
            discovered = load_server_tools(self._record(stub.url))
        self.assertFalse(discovered.from_cache)
        self.assertEqual(discovered.stats["total"], 5)
        self.assertEqual(discovered.stats["read_only"], 4)
        self.assertEqual(discovered.stats["writable"], 1)
        by_name = {tool.name: tool for tool in discovered.tools}
        self.assertTrue(tool_is_read_only(by_name["echo"]))
        self.assertFalse(tool_is_read_only(by_name["writer"]))
        self.assertTrue(all(tool_is_read_only(tool) for tool in discovered.read_only_tools))

    def test_cache_hit_survives_server_shutdown_and_force_refresh_bypasses(self):
        stub = McpStubServer()
        stub.__enter__()
        record = self._record(stub.url)
        first = load_server_tools(record)
        self.assertFalse(first.from_cache)
        stub.__exit__(None, None, None)

        cached = load_server_tools(record)
        self.assertTrue(cached.from_cache)
        self.assertEqual(len(cached.tools), len(first.tools))

        with self.assertRaises(McpDiscoveryError):
            load_server_tools(record, force_refresh=True)

    def test_connection_change_misses_cache(self):
        with McpStubServer() as stub:
            record = self._record(stub.url)
            load_server_tools(record)
            changed = self.store.update("stub", url="http://127.0.0.1:1/mcp")
            with self.assertRaises(McpDiscoveryError) as ctx:
                load_server_tools(changed)
        self.assertIn(ctx.exception.kind, {"connect", "timeout", "protocol"})

    def test_tool_limit_rejects_discovery(self):
        with McpStubServer() as stub, patch.object(settings, "MCP_MAX_TOOLS_PER_SERVER", 2):
            with self.assertRaises(McpDiscoveryError) as ctx:
                load_server_tools(self._record(stub.url))
        self.assertEqual(ctx.exception.kind, "tool_limit")

    def test_rotated_key_degrades_before_connecting(self):
        self.store.create(
            slug="creds",
            name="creds",
            transport="streamable_http",
            url="http://127.0.0.1:1/mcp",
            headers={"Authorization": "Bearer x"},
            created_by="admin",
        )
        with patch.object(settings, "MCP_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii")):
            rotated = self.store.get("creds")
        with self.assertRaises(McpDiscoveryError) as ctx:
            load_server_tools(rotated)
        self.assertEqual(ctx.exception.kind, "credentials")

    def test_sync_bridge_call_and_result_stringification(self):
        with McpStubServer() as stub:
            discovered = load_server_tools(self._record(stub.url))
            by_name = {tool.name: tool for tool in discovered.tools}
            runtime = get_mcp_runtime()
            # Adapter tools open a fresh session per call, so the stub must
            # still be running when the bridged invocation happens.
            result = sync_call_tool(runtime, by_name["echo"], {"text": "桥接"}, timeout=10)
            self.assertIn("桥接", stringify_result(result))

    def test_http_client_factory_ignores_proxies_and_redirects(self):
        from src.agents.mcp.client import mcp_http_client_factory

        client = mcp_http_client_factory()
        self.assertFalse(client.trust_env)
        self.assertFalse(client.follow_redirects)
        import asyncio

        asyncio.run(client.aclose())

    def test_runtime_shutdown_and_reset_leave_no_thread(self):
        runtime = get_mcp_runtime()
        self.assertTrue(runtime.alive)
        reset_mcp_runtime()
        self.assertFalse(runtime.alive)
        replacement = get_mcp_runtime()
        self.assertTrue(replacement.alive)
        self.assertIsNot(replacement, runtime)


if __name__ == "__main__":
    unittest.main()
