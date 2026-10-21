"""T6/T12: /api/v1/plugins/mcp management + subscription HTTP contract."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet

import src.settings
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.client import reset_mcp_runtime
from src.agents.mcp.service import McpPluginService
from src.agents.mcp.store import SqliteMcpServerStore
from src.api.app import create_app
from src.api.deps import get_auth_service
from src.api.routes.plugins import get_mcp_plugin_service
from tests._api_stub import Server, make_auth
from tests._mcp_stub import McpStubServer


class McpPluginsApiTests(unittest.TestCase):
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
        self._old_pw = src.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self.addCleanup(
            setattr, src.settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._old_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", self._old_db)
        self.auth, self.dept, self.admin, self.user = make_auth(self.db_path)
        self.store = SqliteMcpServerStore(self.db_path)
        self.service = McpPluginService(store=self.store)

        self._settings_patches = [
            patch.object(src.settings, "MCP_ENABLED", True),
            patch.object(src.settings, "MCP_WRITE_ENABLED", False),
            patch.object(src.settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True),
            patch.object(src.settings, "RESOURCE_GOVERNOR_ENABLED", True),
            patch.object(src.settings, "MCP_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii")),
            patch.object(src.settings, "MCP_ALLOW_INSECURE_HTTP", True),
            patch.object(src.settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
            patch.object(src.settings, "MCP_CONNECT_TIMEOUT_SECONDS", 10),
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
            src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!"
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

    def _create(self, stub_url: str, slug: str = "stub", **overrides):
        payload = {
            "slug": slug,
            "name": f"服务 {slug}",
            "transport": "streamable_http",
            "url": stub_url,
            "department_scope": "departments",
            "department_ids": [self.dept.id],
            "headers": {"Authorization": "Bearer secret-value"},
        }
        payload.update(overrides)
        return self.client.post(
            "/api/v1/plugins/mcp",
            json=payload,
            headers=self._auth(self.sysadmin_token),
        )

    def _audit_actions(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT action FROM audit_events ORDER BY id"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [str(row[0]) for row in rows]

    # ------------------------------------------------------------------
    def test_full_lifecycle_create_test_enable_subscribe_and_govern(self):
        with McpStubServer() as stub:
            created = self._create(stub.url)
            self.assertEqual(created.status_code, 200, created.text)
            view = created.json()["server"]
            self.assertFalse(view["enabled"])
            self.assertTrue(view["has_credentials"])
            self.assertNotIn("secret-value", created.text)

            # Disabled servers never reach the employee catalog.
            catalog = self.client.get(
                "/api/v1/plugins/mcp", headers=self._auth(self.user_token)
            ).json()
            self.assertEqual(catalog["items"], [])
            self.assertTrue(catalog["enabled"])

            # Enable precondition: a passing test for the current config first.
            blocked = self.client.put(
                "/api/v1/plugins/mcp/stub/status",
                json={"enabled": True},
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(blocked.status_code, 409, blocked.text)

            tested = self.client.post(
                "/api/v1/plugins/mcp/stub/test",
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(tested.status_code, 200, tested.text)
            self.assertTrue(tested.json()["ok"])
            self.assertGreaterEqual(tested.json()["tool_count"], 4)

            enabled = self.client.put(
                "/api/v1/plugins/mcp/stub/status",
                json={"enabled": True},
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            self.assertTrue(enabled.json()["server"]["enabled"])

            catalog = self.client.get(
                "/api/v1/plugins/mcp", headers=self._auth(self.user_token)
            ).json()
            self.assertEqual([item["slug"] for item in catalog["items"]], ["stub"])
            self.assertFalse(catalog["items"][0]["subscribed"])
            self.assertGreaterEqual(catalog["items"][0]["tool_count"], 4)
            self.assertGreaterEqual(catalog["items"][0]["read_only_tool_count"], 1)

            subscribed = self.client.post(
                "/api/v1/plugins/mcp/stub/subscribe",
                json={},
                headers=self._auth(self.user_token),
            )
            self.assertEqual(subscribed.status_code, 200, subscribed.text)
            subscriptions = self.client.get(
                "/api/v1/plugins/mcp/subscriptions", headers=self._auth(self.user_token)
            ).json()
            self.assertEqual([item["slug"] for item in subscriptions["items"]], ["stub"])

            tools = self.client.get(
                "/api/v1/plugins/mcp/stub/tools",
                headers=self._auth(self.sysadmin_token),
            ).json()["items"]
            by_name = {item["name"]: item for item in tools}
            self.assertTrue(by_name["echo"]["read_only"])
            self.assertEqual(by_name["writer"]["blocked_reason"], "not_read_only")
            self.assertFalse(by_name["echo"]["disabled"])

            toggled = self.client.put(
                "/api/v1/plugins/mcp/stub/tools/echo/toggle",
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(toggled.status_code, 200, toggled.text)
            by_name = {item["name"]: item for item in toggled.json()["items"]}
            self.assertTrue(by_name["echo"]["disabled"])
            self.assertEqual(by_name["echo"]["blocked_reason"], "disabled")

            refreshed = self.client.post(
                "/api/v1/plugins/mcp/stub/refresh",
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertTrue(
                any(item["name"] == "writer" for item in refreshed.json()["items"])
            )

            removed = self.client.delete(
                "/api/v1/plugins/mcp/stub/subscribe",
                headers=self._auth(self.user_token),
            )
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual(
                self.client.get(
                    "/api/v1/plugins/mcp/subscriptions",
                    headers=self._auth(self.user_token),
                ).json()["items"],
                [],
            )

        actions = self._audit_actions()
        for expected in (
            "plugins.mcp.create",
            "plugins.mcp.test",
            "plugins.mcp.enable",
            "plugins.mcp.user_subscribe",
            "plugins.mcp.tool_toggle",
            "plugins.mcp.user_unsubscribe",
        ):
            self.assertIn(expected, actions)
            self.assertIn(f"{expected}.requested", actions)

    def test_connection_change_auto_disables_and_voids_the_test(self):
        with McpStubServer() as stub:
            self._create(stub.url)
            self.client.post(
                "/api/v1/plugins/mcp/stub/test", headers=self._auth(self.sysadmin_token)
            )
            self.client.put(
                "/api/v1/plugins/mcp/stub/status",
                json={"enabled": True},
                headers=self._auth(self.sysadmin_token),
            )
            updated = self.client.put(
                "/api/v1/plugins/mcp/stub",
                json={"url": f"{stub.url}/changed"},
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            view = updated.json()["server"]
            self.assertFalse(view["enabled"])
            self.assertFalse(view["test_passed_for_current_config"])

            blocked = self.client.put(
                "/api/v1/plugins/mcp/stub/status",
                json={"enabled": True},
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(blocked.status_code, 409, blocked.text)

    def test_role_boundaries_and_subscription_rules(self):
        with McpStubServer() as stub:
            self._create(stub.url)
            self._create(stub.url, slug="second")
            # Employee cannot govern.
            forbidden = self.client.post(
                "/api/v1/plugins/mcp",
                json={"slug": "x", "name": "x", "url": stub.url},
                headers=self._auth(self.user_token),
            )
            self.assertEqual(forbidden.status_code, 403)
            # System admin cannot subscribe.
            forbidden = self.client.post(
                "/api/v1/plugins/mcp/stub/subscribe",
                json={},
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(forbidden.status_code, 403)
            self.assertEqual(
                self.client.get(
                    "/api/v1/plugins/mcp/subscriptions",
                    headers=self._auth(self.sysadmin_token),
                ).status_code,
                403,
            )
            # Write consent needs a subscription first.
            consent = self.client.post(
                "/api/v1/plugins/mcp/second/write-consent",
                json={"allow_write": True},
                headers=self._auth(self.user_token),
            )
            self.assertEqual(consent.status_code, 404, consent.text)
            # Subscribing while disabled is refused.
            subscribe = self.client.post(
                "/api/v1/plugins/mcp/stub/subscribe",
                json={},
                headers=self._auth(self.user_token),
            )
            self.assertEqual(subscribe.status_code, 409, subscribe.text)

    def test_other_department_server_is_invisible(self):
        with McpStubServer() as stub:
            self._create(stub.url, slug="foreign", department_scope="all")
            self._create(
                stub.url,
                slug="elsewhere",
                department_scope="departments",
                department_ids=[self.dept.id + 999],
            )
            for slug in ("foreign", "elsewhere"):
                self.store.set_enabled(slug, True, "admin")
                self.store.record_test_result(
                    slug,
                    ok=True,
                    fingerprint=self.store.get(slug).connection_fingerprint,
                )
            catalog = self.client.get(
                "/api/v1/plugins/mcp", headers=self._auth(self.user_token)
            ).json()["items"]
            slugs = {item["slug"] for item in catalog}
            self.assertIn("foreign", slugs)
            self.assertNotIn("elsewhere", slugs)

    def test_unknown_server_is_404(self):
        self.assertEqual(
            self.client.get(
                "/api/v1/plugins/mcp/nope", headers=self._auth(self.sysadmin_token)
            ).status_code,
            404,
        )

    def test_governance_audit_actions_are_readable(self):
        with McpStubServer() as stub:
            self._create(stub.url)
            self.client.post(
                "/api/v1/plugins/mcp/stub/test", headers=self._auth(self.sysadmin_token)
            )
            self.client.put(
                "/api/v1/plugins/mcp/stub/status",
                json={"enabled": True},
                headers=self._auth(self.sysadmin_token),
            )
            self.client.put(
                "/api/v1/plugins/mcp/stub",
                json={"name": "改名后的服务"},
                headers=self._auth(self.sysadmin_token),
            )
            self.client.put(
                "/api/v1/plugins/mcp/stub/write-tools",
                json={"write_tools": ["writer"]},
                headers=self._auth(self.sysadmin_token),
            )
            self.client.put(
                "/api/v1/plugins/mcp/stub/status",
                json={"enabled": False},
                headers=self._auth(self.sysadmin_token),
            )
            deleted = self.client.delete(
                "/api/v1/plugins/mcp/stub", headers=self._auth(self.sysadmin_token)
            )
            self.assertEqual(deleted.status_code, 200, deleted.text)

        actions = self._audit_actions()
        for expected in (
            "plugins.mcp.create",
            "plugins.mcp.test",
            "plugins.mcp.enable",
            "plugins.mcp.update",
            "plugins.mcp.write_tools",
            "plugins.mcp.disable",
            "plugins.mcp.delete",
        ):
            self.assertIn(expected, actions)
            self.assertIn(f"{expected}.requested", actions)

    def test_registration_allowed_while_switch_off_but_enable_is_blocked(self):
        """The admin UI states registration stays available with MCP_ENABLED=false."""

        with McpStubServer() as stub:
            with patch.object(src.settings, "MCP_ENABLED", False):
                created = self._create(stub.url, slug="dark")
                self.assertEqual(created.status_code, 200, created.text)
                self.assertFalse(created.json()["server"]["enabled"])
                blocked = self.client.put(
                    "/api/v1/plugins/mcp/dark/status",
                    json={"enabled": True},
                    headers=self._auth(self.sysadmin_token),
                )
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertIn("总开关", blocked.text)
            listed = self.client.get(
                "/api/v1/plugins/mcp", headers=self._auth(self.sysadmin_token)
            )
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertIn(
                "dark", [item["slug"] for item in listed.json()["items"]]
            )

    def test_system_status_reports_mcp_three_states(self):
        # /api/v1/system/status is system-admin only, so the three-state
        # assertions must use the administrator token.
        headers = self._auth(self.sysadmin_token)

        # Built-in and on, but no server is enabled yet -> idle, not degraded.
        component = self.client.get("/api/v1/system/status", headers=headers).json()[
            "dependencies"
        ]["dependencies"]["mcp"]
        self.assertEqual(component["status"], "idle")
        self.assertEqual(component["error"], "")

        # A passing test + enable flips the component to up.
        with McpStubServer() as stub:
            created = self._create(stub.url)
            self.assertEqual(created.status_code, 200, created.text)
            tested = self.client.post(
                "/api/v1/plugins/mcp/stub/test",
                headers=self._auth(self.sysadmin_token),
            )
            self.assertEqual(tested.status_code, 200, tested.text)
            self.client.put(
                "/api/v1/plugins/mcp/stub/status",
                json={"enabled": True},
                headers=self._auth(self.sysadmin_token),
            )
            component = self.client.get(
                "/api/v1/system/status", headers=headers
            ).json()["dependencies"]["dependencies"]["mcp"]
            self.assertEqual(component["status"], "up")
            self.assertEqual(component["enabled"], 1)
            self.assertFalse(component["write_enabled"])

        # Global switch off -> not_configured, never down.
        with patch.object(src.settings, "MCP_ENABLED", False):
            component = self.client.get(
                "/api/v1/system/status", headers=headers
            ).json()["dependencies"]["dependencies"]["mcp"]
        self.assertEqual(component["status"], "not_configured")

        # Readiness must not be blocked by an optional MCP component.
        ready = self.client.get("/health/ready")
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertNotIn("mcp", ready.json().get("dependencies", {}))


if __name__ == "__main__":
    unittest.main()
