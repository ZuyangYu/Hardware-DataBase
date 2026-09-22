"""T8: MCP observability -- span attributes, bounded metric labels, health states."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.observability.tracing as tracing_mod
import src.settings as settings
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.client import reset_mcp_runtime
from src.agents.mcp.executor import MCPToolExecutor
from src.agents.mcp.policy import load_tools, resolve_visible_servers
from src.agents.mcp.store import SqliteMcpServerStore
from src.agents.tools.runtime import ToolRuntime
from src.observability.health import _check_mcp, check_dependencies
from src.pipelines.document_rag.schemas import RequestContext
from tests._mcp_stub import McpStubServer


class _McpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_db = os.path.join(self._tmp.name, "auth.db")
        self.store = SqliteMcpServerStore(self.auth_db)
        self.key = Fernet.generate_key().decode("ascii")
        self._patches = [
            patch.object(settings, "AUTH_DB_PATH", self.auth_db),
            patch.object(settings, "MCP_CREDENTIALS_KEY", self.key),
            patch.object(settings, "MCP_ENABLED", True),
            patch.object(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True),
            patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", True),
            patch.object(settings, "MCP_ALLOW_INSECURE_HTTP", True),
            patch.object(settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
            patch.object(settings, "MCP_CONNECT_TIMEOUT_SECONDS", 10),
            patch.object(settings, "MCP_CALL_TIMEOUT_SECONDS", 5),
            patch.object(settings, "MCP_MAX_RESULT_CHARS", 20000),
            patch.object(settings, "MCP_MAX_RESULT_BYTES", 65536),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        reset_mcp_runtime()
        get_tool_cache().clear_all()
        self._tmp.cleanup()

    def _seed(self, url: str) -> None:
        self.store.create(
            slug="stub",
            name="桩服务",
            transport="streamable_http",
            url=url,
            department_scope="departments",
            department_ids=[7],
            created_by="admin",
        )
        self.store.set_enabled("stub", True, "admin")
        self.store.upsert_subscription(42, "stub")

    def _executor_and_binding(self, *, department: int = 7):
        ctx = RequestContext(
            user_id="alice", roles=["employee"], metadata={"department_id": department}
        )
        rt = ToolRuntime(kb_name="kb_hw", ctx=ctx, question="q", evidence_budget=10)
        snapshot = resolve_visible_servers(ctx, user_id=42, store=self.store)
        plan = load_tools(snapshot, effective_capabilities={"external_tool.invoke"})
        bindings = {binding.tool_name: binding for binding in plan.bindings}
        executor = MCPToolExecutor(rt, user_id=42, department_id=department, store=self.store)
        return executor, bindings


class McpSpanTests(_McpTestBase):
    def test_span_attributes_and_result_body_absence(self):
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        original = tracing_mod._TRACER
        tracing_mod._TRACER = provider.get_tracer("mcp-test")
        try:
            with McpStubServer() as stub:
                self._seed(stub.url)
                executor, bindings = self._executor_and_binding()
                result = executor.call(bindings["echo"], {"text": "机密正文XYZ"})
            self.assertIn("机密正文XYZ", result)
        finally:
            tracing_mod._TRACER = original

        spans = {span.name: span for span in exporter.get_finished_spans()}
        span = spans["mcp.stub.echo"]
        attributes = dict(span.attributes)
        self.assertEqual(attributes["mcp.server.slug"], "stub")
        self.assertEqual(attributes["mcp.transport"], "streamable_http")
        self.assertEqual(attributes["mcp.tool"], "echo")
        self.assertTrue(attributes["mcp.tool.read_only"])
        self.assertEqual(attributes["outcome"], "ok")
        self.assertGreater(attributes["mcp.result.bytes"], 0)
        # Default privacy gate: the tool result/arguments must not be captured.
        rendered = json.dumps(attributes, ensure_ascii=False, default=str)
        self.assertNotIn("机密正文XYZ", rendered)
        self.assertNotIn("input.value", attributes)
        self.assertNotIn("output.value", attributes)


class McpMetricTests(unittest.TestCase):
    def test_helpers_normalize_labels_and_drop_high_cardinality(self):
        import src.observability.metrics as metrics

        calls: list = []
        with (
            patch.object(metrics, "counter", lambda name, **kw: calls.append(("counter", name, kw))),
            patch.object(
                metrics,
                "histogram",
                lambda name, value, **kw: calls.append(("histogram", name, value, kw)),
            ),
        ):
            metrics.record_mcp_call(transport="streamable_http", status="ok", duration_s=0.25)
            metrics.record_mcp_call(transport="weird", status="weird", duration_s=1.0)
            metrics.record_mcp_discovery(transport="sse", status="connect")
            metrics.record_mcp_tools(transport="streamable_http", read_only=True, count=3)
            metrics.record_mcp_truncated(transport="streamable_http")
            metrics.record_mcp_skipped(reason="not_read_only")
            metrics.record_mcp_skipped(reason="never-seen")

        counter_calls = [
            (item[1], item[2].get("attributes")) for item in calls if item[0] == "counter"
        ]
        histogram_calls = [
            (item[1], item[2], item[3].get("attributes"))
            for item in calls
            if item[0] == "histogram"
        ]
        self.assertIn(
            ("hdb.mcp.call_count", {"transport": "streamable_http", "status": "ok"}),
            counter_calls,
        )
        self.assertIn(
            ("hdb.mcp.call_count", {"transport": "other", "status": "failed"}),
            counter_calls,
        )
        self.assertIn(
            ("hdb.mcp.discovery_count", {"transport": "sse", "status": "connect"}),
            counter_calls,
        )
        self.assertIn(
            (
                "hdb.mcp.tools.discovered",
                3,
                {"transport": "streamable_http", "kind": "read_only"},
            ),
            histogram_calls,
        )
        self.assertIn(
            ("hdb.mcp.truncated_total", {"transport": "streamable_http"}),
            counter_calls,
        )
        self.assertIn(
            ("hdb.mcp.skipped_total", {"reason": "not_read_only"}),
            counter_calls,
        )
        self.assertIn(("hdb.mcp.skipped_total", {"reason": "policy"}), counter_calls)
        # Server slugs and tool names must never become metric dimensions.
        self.assertEqual(
            metrics.metric_attributes({"transport": "streamable_http", "server": "secret-slug"}),
            {"transport": "streamable_http"},
        )


class McpExecutorMetricTests(_McpTestBase):
    def test_executor_emits_bounded_call_metrics(self):
        import src.agents.mcp.executor as executor_mod

        captured: list = []
        with McpStubServer() as stub:
            self._seed(stub.url)
            executor, bindings = self._executor_and_binding()
            with patch.object(
                executor_mod,
                "record_mcp_call",
                lambda **kw: captured.append(kw),
            ):
                executor.call(bindings["echo"], {"text": "hi"})

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["status"], "ok")
        self.assertEqual(captured[0]["transport"], "streamable_http")
        self.assertNotIn("stub", repr(captured[0]))


class McpHealthTests(_McpTestBase):
    def test_not_configured_when_switch_is_off(self):
        with patch.object(settings, "MCP_ENABLED", False):
            self.assertEqual(_check_mcp()["status"], "not_configured")
            self.assertEqual(
                check_dependencies()["dependencies"]["mcp"]["status"], "not_configured"
            )

    def test_degraded_when_prerequisites_missing(self):
        with patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", False):
            component = _check_mcp()
        self.assertEqual(component["status"], "degraded")
        self.assertEqual(component["error"], "governor_disabled")

    def test_degraded_when_no_enabled_server(self):
        self.store.create(
            slug="idle",
            name="未启用",
            transport="streamable_http",
            url="http://127.0.0.1:1/mcp",
            created_by="admin",
        )
        component = _check_mcp()
        self.assertEqual(component["status"], "degraded")
        self.assertEqual(component["error"], "no_enabled_server")
        self.assertEqual(component["servers"], 1)
        self.assertEqual(component["enabled"], 0)

    def test_up_with_enabled_server_and_subscriptions(self):
        self.store.create(
            slug="live",
            name="可用",
            transport="streamable_http",
            url="http://127.0.0.1:1/mcp",
            created_by="admin",
        )
        record = self.store.get("live")
        self.store.set_enabled("live", True, "admin")
        self.store.record_test_result(
            "live", ok=True, fingerprint=record.connection_fingerprint
        )
        self.store.upsert_subscription(42, "live")
        component = _check_mcp()
        self.assertEqual(component["status"], "up")
        self.assertEqual(component["enabled"], 1)
        self.assertEqual(component["subscriptions"], 1)
        self.assertFalse(component["write_enabled"])

    def test_degraded_when_enabled_credentials_cannot_be_decrypted(self):
        self.store.create(
            slug="rotated",
            name="密钥轮换",
            transport="streamable_http",
            url="http://127.0.0.1:1/mcp",
            headers={"Authorization": "Bearer x"},
            created_by="admin",
        )
        self.store.set_enabled("rotated", True, "admin")
        with patch.object(settings, "MCP_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii")):
            component = _check_mcp()
        self.assertEqual(component["status"], "degraded")
        self.assertEqual(component["error"], "credentials_unavailable")


if __name__ == "__main__":
    unittest.main()
