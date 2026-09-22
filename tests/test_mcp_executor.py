"""T5: guarded MCP tool execution (policy, quota, timeout, truncation, audit)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import src.settings as settings
from src.agents.mcp.client import reset_mcp_runtime
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.executor import MCPToolExecutor
from src.agents.mcp.policy import load_tools, resolve_visible_servers
from src.agents.mcp.store import SqliteMcpServerStore
from src.agents.tools.runtime import ToolRuntime
from src.pipelines.document_rag.schemas import RequestContext
from tests._mcp_stub import McpStubServer


class McpExecutorTests(unittest.TestCase):
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
            patch.object(settings, "MCP_CALLS_PER_TURN", 8),
            patch.object(settings, "MCP_MAX_RESULT_CHARS", 20000),
            patch.object(settings, "MCP_MAX_RESULT_BYTES", 65536),
            patch.object(settings, "MCP_MAX_ARGUMENT_CHARS", 12000),
        ]
        for item in self._patches:
            item.start()
        self.events: list[dict] = []

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        reset_mcp_runtime()
        get_tool_cache().clear_all()
        self._tmp.cleanup()

    def _rt(self, *, department: int = 7) -> ToolRuntime:
        ctx = RequestContext(
            user_id="alice", roles=["employee"], metadata={"department_id": department}
        )
        return ToolRuntime(
            kb_name="kb_hw",
            ctx=ctx,
            question="q",
            evidence_budget=10,
            on_event=self.events.append,
        )

    def _prepare(self, stub_url: str, *, department: int = 7):
        self.store.create(
            slug="stub",
            name="桩服务",
            transport="streamable_http",
            url=stub_url,
            department_scope="departments",
            department_ids=[department],
            created_by="admin",
        )
        self.store.set_enabled("stub", True, "admin")
        self.store.upsert_subscription(42, "stub")
        ctx = RequestContext(
            user_id="alice", roles=["employee"], metadata={"department_id": department}
        )
        snapshot = resolve_visible_servers(ctx, user_id=42, store=self.store)
        plan = load_tools(snapshot, effective_capabilities={"external_tool.invoke"})
        bindings = {binding.tool_name: binding for binding in plan.bindings}
        executor = MCPToolExecutor(
            self._rt(department=department), user_id=42, department_id=department, store=self.store
        )
        return executor, bindings

    def _audit_rows(self, action: str) -> list[sqlite3.Row]:
        with sqlite3.connect(self.auth_db) as conn:
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(
                    "SELECT * FROM audit_events WHERE action = ?", (action,)
                ).fetchall()
            except sqlite3.OperationalError:
                return []

    # ------------------------------------------------------------------
    def test_happy_path_records_diagnostics_events_and_audit_without_evidence(self):
        with McpStubServer() as stub:
            executor, bindings = self._prepare(stub.url)
            result = executor.call(bindings["echo"], {"text": "你好"})
        self.assertIn("你好", result)
        rt = executor.rt
        self.assertEqual(len(rt.mcp_diagnostics), 1)
        self.assertEqual(rt.mcp_diagnostics[0]["status"], "ok")
        self.assertEqual(rt.diagnostics, [])
        self.assertEqual(rt.evidence, [])
        self.assertEqual(rt.queries, [])
        kinds = [(event["type"], event["payload"].get("tool_kind")) for event in self.events]
        self.assertIn(("tool_started", "mcp"), kinds)
        self.assertIn(("tool_result", "mcp"), kinds)
        self.assertNotIn("degraded", [event["type"] for event in self.events])
        rows = self._audit_rows("plugins.mcp.call")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["target_id"], "stub")

    def test_policy_rejections_do_not_call_the_server(self):
        with McpStubServer() as stub:
            executor, bindings = self._prepare(stub.url)
        # The stub is now closed: every rejection below must happen before any
        # network attempt, otherwise the call would fail with a transport error.
        # unsubscribed
        self.store.delete_subscription(42, "stub")
        self.assertIn("policy_rejected", executor.call(bindings["echo"], {"text": "x"}))
        # disabled
        self.store.upsert_subscription(42, "stub")
        self.store.set_enabled("stub", False, "admin")
        self.assertIn("policy_rejected", executor.call(bindings["echo"], {"text": "x"}))
        # department moved away
        self.store.set_enabled("stub", True, "admin")
        self.store.update("stub", department_ids=[9])
        self.assertIn("policy_rejected", executor.call(bindings["echo"], {"text": "x"}))
        # global switch off
        self.store.update("stub", department_ids=[7])
        with patch.object(settings, "MCP_ENABLED", False):
            self.assertIn("policy_rejected", executor.call(bindings["echo"], {"text": "x"}))
        statuses = [item["status"] for item in executor.rt.mcp_diagnostics]
        self.assertEqual(statuses, ["rejected"] * 4)

    def test_turn_quota_is_atomic(self):
        with McpStubServer() as stub, patch.object(settings, "MCP_CALLS_PER_TURN", 1):
            executor, bindings = self._prepare(stub.url)
            first = executor.call(bindings["echo"], {"text": "one"})
            second = executor.call(bindings["echo"], {"text": "two"})
        self.assertIn("one", first)
        self.assertIn("turn_quota_exceeded", second)
        self.assertEqual(executor.rt.mcp_call_count, 1)

    def test_timeout_returns_fixed_code_and_degraded_event(self):
        with McpStubServer() as stub, patch.object(settings, "MCP_CALL_TIMEOUT_SECONDS", 1):
            executor, bindings = self._prepare(stub.url)
            result = executor.call(bindings["slow"], {"seconds": 5})
        self.assertIn("code=timeout", result)
        self.assertEqual(executor.rt.mcp_diagnostics[0]["status"], "timeout")
        degraded = [event for event in self.events if event["type"] == "degraded"]
        self.assertTrue(degraded)
        self.assertEqual(degraded[-1]["payload"]["stage"], "mcp_call")

    def test_truncation_and_argument_limit(self):
        with McpStubServer() as stub:
            executor, bindings = self._prepare(stub.url)
            with patch.object(settings, "MCP_MAX_RESULT_CHARS", 100):
                text = executor.call(bindings["big_text"], {"size": 5000})
            self.assertTrue(text.endswith("…[已截断]"))
            self.assertTrue(executor.rt.mcp_diagnostics[-1]["truncated"])
            with patch.object(settings, "MCP_MAX_ARGUMENT_CHARS", 10):
                limited = executor.call(bindings["echo"], {"text": "x" * 100})
        self.assertIn("code=invalid_argument", limited)

    def test_cancelled_turn_records_no_late_result(self):
        from src.core.cancellation import QueryCancelled

        with McpStubServer() as stub:
            executor, bindings = self._prepare(stub.url)
            executor.rt.should_cancel = lambda: True
            with self.assertRaises(QueryCancelled):
                executor.call(bindings["echo"], {"text": "late"})
        self.assertEqual(executor.rt.mcp_diagnostics, [])
        self.assertEqual(self._audit_rows("plugins.mcp.call"), [])

    def test_audit_failure_degrades_instead_of_breaking_the_call(self):
        with McpStubServer() as stub:
            executor, bindings = self._prepare(stub.url)
            with patch(
                "src.core.app_logs.AppLogService.record_audit",
                side_effect=RuntimeError("audit down"),
            ):
                result = executor.call(bindings["echo"], {"text": "ok"})
        self.assertIn("ok", result)
        degraded = [event for event in self.events if event["type"] == "degraded"]
        self.assertTrue(any(event["payload"]["failure_type"] == "audit_failed" for event in degraded))


if __name__ == "__main__":
    unittest.main()
