"""T9: MCP phase-1 end-to-end -- API lifecycle + real turn + ledgers.

The model seam is stubbed (``create_deep_agent`` receives the runner-assembled
tools and calls one during its stream), so the whole chain under test is real:
HTTP governance -> subscription -> policy -> discovery -> guarded executor ->
events/audit/metrics/trace, while retrieval evidence and citations stay
untouched.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.observability.tracing as tracing_mod
import src.settings as settings
from src.agents import runner as runner_mod
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.client import reset_mcp_runtime
from src.agents.mcp.service import McpPluginService
from src.agents.mcp.store import SqliteMcpServerStore
from src.api.app import create_app
from src.api.deps import get_auth_service
from src.api.routes.plugins import get_mcp_plugin_service
from src.pipelines.document_rag.schemas import RequestContext
from tests._api_stub import Server, make_auth
from tests._mcp_stub import McpStubServer


class _FakeRAGBackend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


class McpE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.server = Server(cls.app)
        cls.server.start()
        cls.url = cls.server.url

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self._old_pw = settings.AUTH_DEFAULT_ADMIN_PASSWORD
        settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self.addCleanup(
            setattr, settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._old_db = settings.AUTH_DB_PATH
        settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, settings, "AUTH_DB_PATH", self._old_db)
        self.auth, self.dept, self.admin, self.user = make_auth(self.db_path)
        self.store = SqliteMcpServerStore(self.db_path)
        self.service = McpPluginService(store=self.store)

        self._settings_patches = [
            patch.object(settings, "MCP_ENABLED", True),
            patch.object(settings, "MCP_WRITE_ENABLED", False),
            patch.object(
                settings, "MCP_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii")
            ),
            patch.object(settings, "MCP_ALLOW_INSECURE_HTTP", True),
            patch.object(settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
            patch.object(settings, "MCP_CONNECT_TIMEOUT_SECONDS", 10),
            patch.object(settings, "MCP_CALL_TIMEOUT_SECONDS", 5),
            patch.object(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True),
            patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", True),
        ]
        for item in self._settings_patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(reset_mcp_runtime)
        self.addCleanup(get_tool_cache().clear_all)

        self.app.dependency_overrides[get_mcp_plugin_service] = lambda: self.service
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)

        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)
        self.sysadmin_token = self._token(
            settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!"
        )
        self.user_token = self._token("user1")

    def _token(self, username: str, password: str = "pw123456") -> str:
        r = self.client.post(
            "/api/v1/login", json={"username": username, "password": password}
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _provision(self, stub_url: str) -> None:
        """Admin registers/tests/enables a server; the employee subscribes."""

        created = self.client.post(
            "/api/v1/plugins/mcp",
            json={
                "slug": "stub",
                "name": "桩服务",
                "transport": "streamable_http",
                "url": stub_url,
                "department_scope": "departments",
                "department_ids": [self.dept.id],
            },
            headers=self._auth(self.sysadmin_token),
        )
        self.assertEqual(created.status_code, 200, created.text)
        tested = self.client.post(
            "/api/v1/plugins/mcp/stub/test", headers=self._auth(self.sysadmin_token)
        )
        self.assertEqual(tested.status_code, 200, tested.text)
        enabled = self.client.put(
            "/api/v1/plugins/mcp/stub/status",
            json={"enabled": True},
            headers=self._auth(self.sysadmin_token),
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        subscribed = self.client.post(
            "/api/v1/plugins/mcp/stub/subscribe",
            json={},
            headers=self._auth(self.user_token),
        )
        self.assertEqual(subscribed.status_code, 200, subscribed.text)

    def _run_turn(self, *, invoke: str | None, events: list | None = None):
        captured: dict = {}
        chunks = [AIMessage(content="最终回答")]

        def stream(*args, **kwargs):
            if invoke is not None:
                tool = next(
                    item
                    for item in captured["tools"]
                    if getattr(item, "name", "") == invoke
                )
                captured["tool_result"] = tool.invoke({"text": "E2E-外部结果"})
            for chunk in chunks:
                yield chunk, {"langgraph_node": "model"}

        fake_agent = type("F", (), {})()
        fake_agent.stream = stream

        def fake_create_deep_agent(**kwargs):
            captured.update(kwargs)
            return fake_agent

        with (
            patch.object(runner_mod, "create_chat_model", lambda: object()),
            patch.object(runner_mod, "create_deep_agent", fake_create_deep_agent),
            patch.object(runner_mod, "record_agent", Mock()),
        ):
            runner = runner_mod.MultiSourceAgentRunner(
                rag_backend=_FakeRAGBackend(), circuit_service=None
            )
            ctx = RequestContext(
                user_id="user1",
                roles=["employee"],
                metadata={"department_id": self.dept.id},
            )
            deltas = list(
                runner.stream(
                    query="查一下外部数据",
                    kb_name="kb_hw",
                    history=[],
                    ctx=ctx,
                    thread_id="t-e2e",
                    event_callback=(events.append if events is not None else None),
                    attachment_user_id=self.user.id,
                )
            )
        return runner, deltas, captured

    def _audit_rows(self, action: str) -> list[tuple]:
        with sqlite3.connect(self.db_path) as conn:
            try:
                return conn.execute(
                    "SELECT target_id, success FROM audit_events WHERE action = ?", (action,)
                ).fetchall()
            except sqlite3.OperationalError:
                return []

    # ------------------------------------------------------------------
    def test_subscribed_turn_calls_tool_and_keeps_ledgers_clean(self):
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        original_tracer = tracing_mod._TRACER
        tracing_mod._TRACER = provider.get_tracer("mcp-e2e")
        events: list = []
        metric_calls: list = []
        import src.agents.mcp.executor as executor_mod

        try:
            with McpStubServer() as stub:
                self._provision(stub.url)
                with patch.object(
                    executor_mod, "record_mcp_call", lambda **kw: metric_calls.append(kw)
                ):
                    runner, deltas, captured = self._run_turn(
                        invoke="mcp__stub__echo", events=events
                    )
        finally:
            tracing_mod._TRACER = original_tracer

        # The model seam really invoked the guarded tool end to end.
        names = {getattr(tool, "name", "") for tool in captured["tools"]}
        self.assertIn("mcp__stub__echo", names)
        self.assertIn("E2E-外部结果", str(captured["tool_result"]))
        self.assertEqual(deltas, ["最终回答"])

        # Events: MCP timeline visible, no degraded entry on the happy path.
        mcp_events = [
            event
            for event in events
            if (event.get("payload") or {}).get("tool_kind") == "mcp"
        ]
        self.assertEqual(
            sorted({event["type"] for event in mcp_events}),
            ["tool_result", "tool_started"],
        )
        self.assertNotIn("degraded", [event["type"] for event in events])

        # Audit: management + call facts are durable.
        self.assertEqual(self._audit_rows("plugins.mcp.user_subscribe"), [("stub", 1)])
        self.assertEqual(self._audit_rows("plugins.mcp.call"), [("stub", 1)])

        # Metrics and trace: bounded labels, span present, body absent.
        self.assertEqual(len(metric_calls), 1)
        self.assertEqual(metric_calls[0]["status"], "ok")
        self.assertNotIn("stub", repr(metric_calls[0]))
        span = next(span for span in exporter.get_finished_spans() if span.name == "mcp.stub.echo")
        self.assertEqual(span.attributes["mcp.server.slug"], "stub")
        self.assertNotIn("E2E-外部结果", str(dict(span.attributes)))

        # Evidence chain untouched: no citations, no retrieval diagnostics.
        summary = runner.get_last_retrieval_summary()
        self.assertEqual(summary["evidence"], [])
        self.assertFalse(
            any(
                str(item.get("tool_name", "")).startswith("mcp__")
                for item in summary["tool_diagnostics"]
            )
        )
        self.assertNotIn("[1]", "".join(deltas))
        self.assertNotIn("外部", runner.get_last_footer() or "")
        rounds_with_mcp = summary["retrieval_rounds"]
        diagnostic_names = sorted(
            str(item.get("tool_name", "")) for item in summary["tool_diagnostics"]
        )

        # Disabling the server makes the very next turn invisible again.
        disabled = self.client.put(
            "/api/v1/plugins/mcp/stub/status",
            json={"enabled": False},
            headers=self._auth(self.sysadmin_token),
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        events_after: list = []
        runner_after, _deltas, captured_after = self._run_turn(invoke=None, events=events_after)
        names_after = {getattr(tool, "name", "") for tool in captured_after["tools"]}
        self.assertFalse(any(name.startswith("mcp__") for name in names_after))
        self.assertNotIn(
            "mcp",
            [
                (event.get("payload") or {}).get("tool_kind")
                for event in events_after
            ],
        )
        self.assertEqual(len(self._audit_rows("plugins.mcp.call")), 1)
        # Same retrieval ledger as the MCP turn: external calls change nothing.
        summary_after = runner_after.get_last_retrieval_summary()
        self.assertEqual(summary_after["evidence"], [])
        self.assertEqual(summary_after["retrieval_rounds"], rounds_with_mcp)
        self.assertEqual(
            sorted(
                str(item.get("tool_name", "")) for item in summary_after["tool_diagnostics"]
            ),
            diagnostic_names,
        )

    def test_large_catalogue_searches_then_calls_real_tool(self):
        from langchain.agents.middleware.types import ModelRequest

        from src.agents.middleware.mcp_tool_search import McpToolSearchMiddleware

        with McpStubServer(extra_read_tools=10) as stub:
            self._provision(stub.url)
            _runner, _deltas, captured = self._run_turn(invoke=None, events=[])

            tools = {getattr(tool, "name", ""): tool for tool in captured["tools"]}
            self.assertIn("mcp_tool_search", tools)
            self.assertIn("mcp__stub__extra_7", tools)
            middleware = next(
                item
                for item in captured["middleware"]
                if isinstance(item, McpToolSearchMiddleware)
            )
            request = ModelRequest(
                model=object(),
                messages=[],
                tools=captured["tools"],
                state={"messages": []},
            )

            before = {
                getattr(tool, "name", "")
                for tool in middleware.wrap_model_call(
                    request, lambda current: current.tools
                )
            }
            self.assertIn("mcp_tool_search", before)
            self.assertFalse(any(name.startswith("mcp__") for name in before))

            rendered = tools["mcp_tool_search"].invoke({"query": "extra_7"})
            self.assertIn("mcp__stub__extra_7", rendered)

            after = {
                getattr(tool, "name", "")
                for tool in middleware.wrap_model_call(
                    request, lambda current: current.tools
                )
            }
            self.assertIn("mcp__stub__extra_7", after)
            self.assertNotIn("mcp__stub__echo", after)

            # Selection only changes visibility; the real guarded wrapper still runs.
            self.assertIn(
                "extra-7:E2E",
                tools["mcp__stub__extra_7"].invoke({"text": "E2E"}),
            )

    def test_outage_degrades_without_failing_the_turn(self):
        events: list = []
        with McpStubServer() as stub:
            self._provision(stub.url)
            # Load tools while the server is up, then kill it before the call.
            _runner, _deltas, captured = self._run_turn(invoke=None, events=[])
        echo = next(
            tool for tool in captured["tools"] if getattr(tool, "name", "") == "mcp__stub__echo"
        )
        failed_call = echo.invoke({"text": "offline"})
        self.assertIn("code=connect_failed", failed_call)

        # A cold-cache turn while the server is down still completes and reports
        # degraded instead of failing; a warm-cache call fails with a fixed code
        # (asserted above) and never breaks the turn either.
        get_tool_cache().clear_all()
        runner, deltas, captured_down = self._run_turn(invoke=None, events=events)
        self.assertEqual(deltas, ["最终回答"])
        names = {getattr(tool, "name", "") for tool in captured_down["tools"]}
        self.assertFalse(any(name.startswith("mcp__") for name in names))
        degraded = [event for event in events if event["type"] == "degraded"]
        self.assertTrue(degraded)
        self.assertEqual(degraded[-1]["payload"]["stage"], "mcp_load")
        self.assertEqual(degraded[-1]["payload"]["server"], "stub")
        summary = runner.get_last_retrieval_summary()
        self.assertEqual(summary["evidence"], [])


class McpReloadTests(unittest.TestCase):
    def test_reload_settings_applies_mcp_switch(self):
        previous = settings.MCP_ENABLED
        try:
            with patch.dict(os.environ, {"MCP_ENABLED": "true"}), patch(
                "src.settings.load_dotenv"
            ):
                settings.reload_settings()
            self.assertTrue(settings.MCP_ENABLED)
            with patch.dict(os.environ, {"MCP_ENABLED": "false"}), patch(
                "src.settings.load_dotenv"
            ):
                settings.reload_settings()
            self.assertFalse(settings.MCP_ENABLED)
        finally:
            settings.MCP_ENABLED = previous

    def test_worker_reloads_only_at_a_new_turn_boundary(self):
        from src.workers.main import HardwareWorker

        worker = HardwareWorker.__new__(HardwareWorker)
        worker._env_mtime_ns = 0
        with (
            patch.object(HardwareWorker, "_settings_mtime_ns", staticmethod(lambda: 123)),
            patch("src.settings.reload_settings") as reload_mock,
            patch("src.core.model_factory.invalidate_chat_model_cache"),
        ):
            worker._reload_runtime_settings_if_changed()
            reload_mock.assert_called_once()
            # Unchanged .env mtime: the next turn boundary must not reload.
            worker._reload_runtime_settings_if_changed()
            reload_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
