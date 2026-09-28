"""T4: MCP visibility policy and per-turn tool plan."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import src.settings as settings
from src.agents.mcp.client import reset_mcp_runtime
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.policy import (
    append_mcp_prompt,
    is_mcp_tool_name,
    load_tools,
    mcp_tool_name,
    parse_mcp_tool_name,
    resolve_visible_servers,
)
from src.agents.mcp.store import SqliteMcpServerStore
from src.pipelines.document_rag.schemas import RequestContext
from tests._mcp_stub import McpStubServer


class _BrokenStore:
    def list_enabled(self):
        raise RuntimeError("db down")

    def list_subscribed_slugs(self, _user_id):
        raise RuntimeError("db down")


class McpPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteMcpServerStore(os.path.join(self._tmp.name, "auth.db"))
        self.key = Fernet.generate_key().decode("ascii")
        self._patches = [
            patch.object(settings, "MCP_CREDENTIALS_KEY", self.key),
            patch.object(settings, "MCP_ENABLED", True),
            patch.object(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True),
            patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", True),
            patch.object(settings, "MCP_ALLOW_INSECURE_HTTP", True),
            patch.object(settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
            patch.object(settings, "MCP_MAX_SERVERS_PER_RUN", 5),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        reset_mcp_runtime()
        get_tool_cache().clear_all()
        self._tmp.cleanup()

    def _ctx(self, department: int = 7):
        return RequestContext(
            user_id="alice",
            roles=["employee"],
            metadata={"department_id": department},
        )

    def _server(
        self,
        slug: str,
        *,
        scope="departments",
        departments=(7,),
        url="http://127.0.0.1:1/mcp",
        enable: bool = True,
        **extra,
    ):
        record = self.store.create(
            slug=slug,
            name=f"服务 {slug}",
            transport="streamable_http",
            url=url,
            department_scope=scope,
            department_ids=list(departments),
            created_by="admin",
            **extra,
        )
        if enable:
            record = self.store.set_enabled(slug, True, "admin")
        return record

    # ------------------------------------------------------------------
    def test_prerequisites_gate(self):
        with patch.object(settings, "MCP_ENABLED", False):
            self.assertEqual(resolve_visible_servers(self._ctx(), user_id=42).reason, "disabled")
        with patch.object(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", False):
            self.assertEqual(
                resolve_visible_servers(self._ctx(), user_id=42).reason, "unified_tools_disabled"
            )
        with patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", False):
            self.assertEqual(
                resolve_visible_servers(self._ctx(), user_id=42).reason, "governor_disabled"
            )

    def test_missing_context_user_and_store_error_are_fail_closed(self):
        self.assertEqual(resolve_visible_servers(None, user_id=42, store=self.store).reason, "missing_context")
        self.assertEqual(
            resolve_visible_servers(self._ctx(), user_id=None, store=self.store).reason,
            "missing_user_id",
        )
        admin = RequestContext(user_id="root", roles=["system_admin"], metadata={"department_id": 7})
        self.assertEqual(
            resolve_visible_servers(admin, user_id=1, store=self.store).reason, "governance_role"
        )
        self.assertEqual(
            resolve_visible_servers(self._ctx(), user_id=1, store=_BrokenStore()).reason, "store_error"
        )

    def test_visibility_requires_subscription_department_and_scope(self):
        self._server("visible")
        self._server("not-subscribed")
        self._server("other-department", departments=(9,))
        self._server("scoped-none", scope="none")
        self._server("scoped-all", scope="all")
        self.store.upsert_subscription(42, "visible")
        self.store.upsert_subscription(42, "other-department")
        self.store.upsert_subscription(42, "scoped-none")
        self.store.upsert_subscription(42, "scoped-all")

        snapshot = resolve_visible_servers(self._ctx(), user_id=42, store=self.store)
        self.assertEqual({record.slug for record in snapshot.servers}, {"visible", "scoped-all"})
        reasons = {(item["slug"], item["reason"]) for item in snapshot.skipped}
        self.assertIn(("not-subscribed", "not_subscribed"), reasons)
        self.assertIn(("other-department", "department"), reasons)
        self.assertIn(("scoped-none", "department"), reasons)

    def test_disabled_and_credentials_broken_servers_are_skipped(self):
        self._server("disabled", enable=False)
        self.store.upsert_subscription(42, "disabled")
        with patch.object(settings, "MCP_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii")):
            self._server("rotated", headers={"Authorization": "Bearer x"})
        self.store.upsert_subscription(42, "rotated")

        snapshot = resolve_visible_servers(self._ctx(), user_id=42, store=self.store)
        self.assertEqual(snapshot.servers, ())
        self.assertEqual({item["reason"] for item in snapshot.skipped}, {"credentials"})

    def test_server_limit_keeps_newest_and_records_skip(self):
        for slug in ("s1", "s2", "s3"):
            self._server(slug)
            self.store.upsert_subscription(42, slug)
        with patch.object(settings, "MCP_MAX_SERVERS_PER_RUN", 2):
            snapshot = resolve_visible_servers(self._ctx(), user_id=42, store=self.store)
        self.assertEqual(len(snapshot.servers), 2)
        self.assertIn({"slug": snapshot.skipped[0]["slug"], "reason": "server_limit"}, snapshot.skipped)

    # ------------------------------------------------------------------
    def test_load_tools_requires_capability(self):
        self._server("cap")
        self.store.upsert_subscription(42, "cap")
        snapshot = resolve_visible_servers(self._ctx(), user_id=42, store=self.store)
        plan = load_tools(snapshot, effective_capabilities=set())
        self.assertEqual(plan.reason, "capability")
        self.assertEqual(plan.tools, [])

    def test_load_tools_filters_read_only_disabled_and_name_conflicts(self):
        with McpStubServer() as stub:
            self._server("stub", url=stub.url)
            self.store.upsert_subscription(42, "stub")
            self.store.toggle_tool("stub", "echo", "admin")
            snapshot = resolve_visible_servers(self._ctx(), user_id=42, store=self.store)
            wrapped: list[str] = []
            plan = load_tools(
                snapshot,
                effective_capabilities={"external_tool.invoke"},
                local_tool_names={"mcp__stub__dup_name"},
                wrap=lambda binding: wrapped.append(binding.safe_name) or binding.safe_name,
            )
        names = {binding.safe_name for binding in plan.bindings}
        self.assertIn(mcp_tool_name("stub", "big_text"), names)
        self.assertIn(mcp_tool_name("stub", "slow"), names)
        self.assertNotIn(mcp_tool_name("stub", "writer"), names)
        self.assertNotIn(mcp_tool_name("stub", "echo"), names)
        self.assertNotIn(mcp_tool_name("stub", "dup_name"), names)
        reasons = {item.get("tool"): item["reason"] for item in plan.skipped if "tool" in item}
        self.assertEqual(reasons.get("writer"), "not_read_only")
        self.assertEqual(reasons.get("echo"), "disabled")
        self.assertEqual(reasons.get("dup_name"), "name_conflict")
        self.assertEqual(plan.tools, wrapped)
        self.assertTrue(plan.has_tools)

    def test_discovery_failure_is_per_server_and_keeps_others(self):
        with McpStubServer() as stub:
            self._server("good", url=stub.url)
            self._server("bad", url="http://127.0.0.1:1/mcp")
            self.store.upsert_subscription(42, "good")
            self.store.upsert_subscription(42, "bad")
            snapshot = resolve_visible_servers(self._ctx(), user_id=42, store=self.store)
            plan = load_tools(snapshot, effective_capabilities={"external_tool.invoke"})
        self.assertTrue(any(binding.server.slug == "good" for binding in plan.bindings))
        self.assertEqual([error["slug"] for error in plan.errors], ["bad"])

    def test_prompt_appended_only_with_tools_and_names_are_parseable(self):
        self.assertEqual(append_mcp_prompt("base", load_tools(resolve_visible_servers(None), effective_capabilities=set())), "base")
        self.assertTrue(is_mcp_tool_name("mcp__stub__echo"))
        self.assertEqual(parse_mcp_tool_name("mcp__stub__echo"), ("stub", "echo"))
        self.assertIsNone(parse_mcp_tool_name("document_search"))
        self.assertIsNone(parse_mcp_tool_name("mcp__broken"))


if __name__ == "__main__":
    unittest.main()
