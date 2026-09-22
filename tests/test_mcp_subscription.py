"""T12: employee subscription contract over real HTTP.

Covers idempotency, cross-department/disabled rejection, immediate effect of
unsubscribe/disable, cascade delete, catalog masking and write-consent gating.
"""

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
from src.agents.mcp.policy import resolve_visible_servers
from src.agents.mcp.service import McpPluginService
from src.agents.mcp.store import SqliteMcpServerStore
from src.api.app import create_app
from src.api.deps import get_auth_service
from src.api.routes.plugins import get_mcp_plugin_service
from src.pipelines.document_rag.schemas import RequestContext
from tests._api_stub import Server, make_auth


class McpSubscriptionTests(unittest.TestCase):
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
            patch.object(
                src.settings, "MCP_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii")
            ),
            patch.object(src.settings, "MCP_ALLOW_INSECURE_HTTP", True),
            patch.object(src.settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
            patch.object(src.settings, "MCP_CONNECT_TIMEOUT_SECONDS", 10),
            patch.object(src.settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True),
            patch.object(src.settings, "RESOURCE_GOVERNOR_ENABLED", True),
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
        self.user_token = self._token("user1")

    def _token(self, username: str, password: str = "pw123456") -> str:
        response = self.client.post(
            "/api/v1/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _enable(self, slug: str, *, department_ids=None, scope="departments", **extra):
        record = self.store.create(
            slug=slug,
            name=f"服务 {slug}",
            transport="streamable_http",
            url="http://127.0.0.1:1/mcp",
            department_scope=scope,
            department_ids=list(department_ids if department_ids is not None else [self.dept.id]),
            created_by="admin",
            **extra,
        )
        self.store.set_enabled(slug, True, "admin")
        self.store.record_test_result(slug, ok=True, fingerprint=record.connection_fingerprint)
        return record

    def _visible_slugs(self) -> set[str]:
        ctx = RequestContext(
            user_id="user1", roles=["employee"], metadata={"department_id": self.dept.id}
        )
        snapshot = resolve_visible_servers(ctx, user_id=self.user.id, store=self.store)
        return {record.slug for record in snapshot.servers}

    def _audit_actions(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
            except sqlite3.OperationalError:
                return []
        return [str(row[0]) for row in rows]

    # ------------------------------------------------------------------
    def test_subscribe_is_idempotent_and_audited(self):
        self._enable("stub")
        for _ in range(2):
            response = self.client.post(
                "/api/v1/plugins/mcp/stub/subscribe",
                json={},
                headers=self._auth(self.user_token),
            )
            self.assertEqual(response.status_code, 200, response.text)
        subscriptions = self.client.get(
            "/api/v1/plugins/mcp/subscriptions", headers=self._auth(self.user_token)
        ).json()["items"]
        self.assertEqual([item["slug"] for item in subscriptions], ["stub"])
        self.assertEqual(self._visible_slugs(), {"stub"})
        self.assertEqual(self._audit_actions().count("plugins.mcp.user_subscribe"), 2)

    def test_cross_department_and_disabled_subscriptions_are_rejected(self):
        self._enable("foreign", department_ids=[self.dept.id + 999])
        self.store.create(
            slug="disabled",
            name="未启用",
            transport="streamable_http",
            url="http://127.0.0.1:1/mcp",
            department_scope="departments",
            department_ids=[self.dept.id],
            created_by="admin",
        )
        forbidden = self.client.post(
            "/api/v1/plugins/mcp/foreign/subscribe",
            json={},
            headers=self._auth(self.user_token),
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        conflict = self.client.post(
            "/api/v1/plugins/mcp/disabled/subscribe",
            json={},
            headers=self._auth(self.user_token),
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(self._visible_slugs(), set())
        self.assertEqual(self.store.list_subscribed_slugs(self.user.id), set())

    def test_unsubscribe_is_immediate_and_catalog_stays_masked(self):
        self._enable("stub")
        self.client.post(
            "/api/v1/plugins/mcp/stub/subscribe",
            json={},
            headers=self._auth(self.user_token),
        )
        catalog = self.client.get(
            "/api/v1/plugins/mcp", headers=self._auth(self.user_token)
        ).json()
        item = catalog["items"][0]
        self.assertEqual(
            set(item),
            {
                "slug",
                "name",
                "description",
                "icon",
                "tags",
                "tool_count",
                "read_only_tool_count",
                "subscribed",
                "allow_write",
                "writable",
            },
        )
        rendered = str(catalog)
        self.assertNotIn("127.0.0.1", rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("schema", rendered)

        removed = self.client.delete(
            "/api/v1/plugins/mcp/stub/subscribe", headers=self._auth(self.user_token)
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self._visible_slugs(), set())
        self.assertEqual(
            self.client.get(
                "/api/v1/plugins/mcp/subscriptions", headers=self._auth(self.user_token)
            ).json()["items"],
            [],
        )

    def test_admin_disable_overrides_subscription_and_delete_cascades(self):
        self._enable("stub")
        self.client.post(
            "/api/v1/plugins/mcp/stub/subscribe",
            json={},
            headers=self._auth(self.user_token),
        )
        self.assertEqual(self._visible_slugs(), {"stub"})

        self.store.set_enabled("stub", False, "admin")
        self.assertEqual(self._visible_slugs(), set())
        self.assertEqual(self.store.list_subscribed_slugs(self.user.id), {"stub"})

        self.store.delete("stub")
        self.assertEqual(self.store.list_subscriptions(self.user.id), [])

    def test_write_consent_gating_and_audit(self):
        self._enable("writable", write_tools=["writer"])
        # Consent before subscribing is a 404.
        consent = self.client.post(
            "/api/v1/plugins/mcp/writable/write-consent",
            json={"allow_write": True},
            headers=self._auth(self.user_token),
        )
        self.assertEqual(consent.status_code, 404, consent.text)
        # Write channel closed: subscribing with consent is refused.
        refused = self.client.post(
            "/api/v1/plugins/mcp/writable/subscribe",
            json={"allow_write": True},
            headers=self._auth(self.user_token),
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.client.post(
            "/api/v1/plugins/mcp/writable/subscribe",
            json={},
            headers=self._auth(self.user_token),
        )
        with patch.object(src.settings, "MCP_WRITE_ENABLED", True):
            granted = self.client.post(
                "/api/v1/plugins/mcp/writable/write-consent",
                json={"allow_write": True},
                headers=self._auth(self.user_token),
            )
            self.assertEqual(granted.status_code, 200, granted.text)
            self.assertTrue(granted.json()["subscription"]["allow_write"])
            revoked = self.client.post(
                "/api/v1/plugins/mcp/writable/write-consent",
                json={"allow_write": False},
                headers=self._auth(self.user_token),
            )
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertFalse(revoked.json()["subscription"]["allow_write"])
        self.assertEqual(self._audit_actions().count("plugins.mcp.write_consent"), 2)


if __name__ == "__main__":
    unittest.main()
