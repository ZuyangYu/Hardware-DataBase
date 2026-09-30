"""T2: MCP server store, credential encryption and user subscriptions."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import src.settings as settings
from src.agents.mcp.store import (
    McpCredentialError,
    McpServerExistsError,
    McpServerNotFoundError,
    SqliteMcpServerStore,
    connection_fingerprint,
)


def _server_fields(slug: str, **overrides):
    fields = {
        "slug": slug,
        "name": f"服务 {slug}",
        "transport": "streamable_http",
        "url": f"https://mcp.example.test/{slug}",
        "description": "测试服务",
        "headers": {"Authorization": "Bearer secret-value"},
        "timeout_s": 30,
        "department_scope": "departments",
        "department_ids": [7, 9],
        "disabled_tools": ["writer"],
        "write_tools": ["kicad_create_schematic"],
        "tags": ["eda"],
        "icon": "k",
        "created_by": "admin",
    }
    fields.update(overrides)
    return fields


class McpStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "auth.db")
        self.key_a = Fernet.generate_key().decode("ascii")
        self.key_b = Fernet.generate_key().decode("ascii")
        self._key_patch = patch.object(settings, "MCP_CREDENTIALS_KEY", self.key_a)
        self._key_patch.start()
        self.store = SqliteMcpServerStore(self.db_path)

    def tearDown(self) -> None:
        self._key_patch.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    def test_create_get_list_and_unique_slug(self):
        created = self.store.create(**_server_fields("alpha"))
        self.assertFalse(created.enabled)
        self.assertEqual(created.department_ids, [7, 9])
        self.assertEqual(created.disabled_tools, ["writer"])
        self.assertEqual(created.write_tools, ["kicad_create_schematic"])
        self.assertTrue(created.connection_fingerprint)
        self.assertTrue(created.credentials_ok)

        fetched = self.store.get("alpha")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.headers["Authorization"], "Bearer secret-value")

        with self.assertRaises(McpServerExistsError):
            self.store.create(**_server_fields("alpha"))
        self.assertIsNone(self.store.get("missing"))
        self.assertEqual([record.slug for record in self.store.list_all()], ["alpha"])

    def test_credentials_are_encrypted_at_rest_and_never_plaintext(self):
        self.store.create(**_server_fields("crypto"))
        with sqlite3.connect(self.db_path) as conn:
            raw = conn.execute(
                "SELECT headers_ciphertext FROM mcp_servers WHERE slug = 'crypto'"
            ).fetchone()[0]
        self.assertNotIn("secret-value", raw)
        self.assertNotIn("Authorization", raw)

    def test_headers_require_key_and_rotated_key_degrades_one_server(self):
        with patch.object(settings, "MCP_CREDENTIALS_KEY", ""):
            with self.assertRaises(McpCredentialError):
                self.store.create(**_server_fields("no-key"))

        self.store.create(**_server_fields("rotated"))
        self.store.create(**_server_fields("plain", headers={}))
        with patch.object(settings, "MCP_CREDENTIALS_KEY", self.key_b):
            rotated = self.store.get("rotated")
            plain = self.store.get("plain")
        self.assertFalse(rotated.credentials_ok)
        self.assertTrue(rotated.headers_error)
        self.assertEqual(rotated.headers, {})
        # A server without credentials is unaffected by key rotation.
        self.assertTrue(plain.credentials_ok)

    def test_update_headers_keep_replace_and_clear(self):
        self.store.create(**_server_fields("keep"))
        kept = self.store.update("keep", name="新名字")
        self.assertEqual(kept.headers["Authorization"], "Bearer secret-value")
        self.assertEqual(kept.name, "新名字")

        replaced = self.store.update("keep", headers={"X-Api-Key": "another"})
        self.assertEqual(replaced.headers, {"X-Api-Key": "another"})

        cleared = self.store.update("keep", headers=None)
        self.assertEqual(cleared.headers, {})
        self.assertFalse(cleared.test_passed_for_current_config())

    def test_connection_change_auto_disables_and_voids_test(self):
        created = self.store.create(**_server_fields("conn"))
        self.store.record_test_result(
            "conn", ok=True, fingerprint=created.connection_fingerprint
        )
        enabled = self.store.set_enabled("conn", True, "admin")
        self.assertTrue(enabled.enabled)
        self.assertTrue(enabled.test_passed_for_current_config())

        changed = self.store.update("conn", url="https://mcp.example.test/other")
        self.assertFalse(changed.enabled)
        self.assertEqual(changed.last_test_fingerprint, "")
        self.assertFalse(changed.test_passed_for_current_config())

    def test_failed_test_clears_fingerprint_but_keeps_history(self):
        created = self.store.create(**_server_fields("failing"))
        self.store.record_test_result(
            "failing", ok=True, fingerprint=created.connection_fingerprint
        )
        self.store.record_test_result("failing", ok=False, fingerprint="", error="connect timeout")
        record = self.store.get("failing")
        self.assertEqual(record.last_test_fingerprint, "")
        self.assertEqual(record.last_test_error, "connect timeout")
        self.assertTrue(record.last_test_ok_at)
        self.assertFalse(record.test_passed_for_current_config())

    def test_toggle_tool_and_write_tools(self):
        self.store.create(**_server_fields("tools"))
        enabled, record = self.store.toggle_tool("tools", "writer", "admin")
        self.assertTrue(enabled)
        self.assertNotIn("writer", record.disabled_tools)
        enabled, record = self.store.toggle_tool("tools", "echo", "admin")
        self.assertFalse(enabled)
        self.assertIn("echo", record.disabled_tools)

        updated = self.store.set_write_tools("tools", ["a", "b", "a"], "admin")
        self.assertEqual(updated.write_tools, ["a", "b"])

    def test_delete_cascades_subscriptions(self):
        self.store.create(**_server_fields("gone"))
        self.store.upsert_subscription(5, "gone", allow_write=True)
        self.assertEqual(self.store.count_subscriptions(), 1)
        self.assertTrue(self.store.delete("gone"))
        self.assertFalse(self.store.delete("gone"))
        self.assertEqual(self.store.count_subscriptions(), 0)

    # ------------------------------------------------------------------
    def test_subscriptions_roundtrip_and_validation(self):
        self.store.create(**_server_fields("sub"))
        created = self.store.upsert_subscription(5, "sub", allow_write=True)
        self.assertTrue(created.allow_write)
        self.assertEqual(self.store.list_subscribed_slugs(5), {"sub"})
        self.assertEqual(self.store.list_subscribed_slugs(6), set())

        updated = self.store.upsert_subscription(5, "sub", allow_write=False)
        self.assertFalse(updated.allow_write)
        self.assertEqual(len(self.store.list_subscriptions(5)), 1)

        self.assertTrue(self.store.delete_subscription(5, "sub"))
        self.assertFalse(self.store.delete_subscription(5, "sub"))
        self.assertEqual(self.store.delete_user_subscriptions(5), 0)

        with self.assertRaises(McpServerNotFoundError):
            self.store.upsert_subscription(5, "missing")

    def test_repeat_init_is_idempotent_and_columns_exist(self):
        again = SqliteMcpServerStore(self.db_path)
        again.create(**_server_fields("idem"))
        with sqlite3.connect(self.db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(mcp_servers)").fetchall()
            }
        for expected in (
            "headers_ciphertext",
            "department_scope",
            "write_tools",
            "connection_fingerprint",
            "last_test_fingerprint",
        ):
            self.assertIn(expected, columns)

    def test_concurrent_short_transactions(self):
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                for step in range(5):
                    slug = f"c{index}"
                    if step == 0:
                        self.store.create(**_server_fields(slug, headers={}))
                    else:
                        self.store.toggle_tool(slug, f"tool{step}", "worker")
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.list_all()), 8)

    def test_fingerprint_depends_on_connection_fields(self):
        base = dict(transport="streamable_http", url="https://x.test/mcp", timeout_s=None, sse_read_timeout_s=None, headers={})
        same = connection_fingerprint(**base)
        self.assertEqual(same, connection_fingerprint(**base))
        other = connection_fingerprint(**{**base, "url": "https://y.test/mcp"})
        self.assertNotEqual(same, other)

    def test_admin_and_catalog_views_do_not_leak_credentials(self):
        created = self.store.create(**_server_fields("views"))
        admin = created.admin_view()
        self.assertEqual(admin["headers"], {"Authorization": "***"})
        self.assertEqual(admin["url"], "https://mcp.example.test/views")
        catalog = created.catalog_view(subscribed=True, allow_write=False, tool_count=3, read_only_tool_count=2)
        payload = json.dumps(catalog, ensure_ascii=False)
        self.assertNotIn("mcp.example.test", payload)
        self.assertNotIn("Authorization", payload)
        self.assertTrue(catalog["subscribed"])

    def test_store_stays_on_auth_db_with_postgres_control_plane(self):
        """Phase 1: MCP config lives in AUTH_DB_PATH, never in the PG control plane."""

        from src.agents.mcp.store import build_mcp_store

        with patch.object(settings, "CONTROL_PLANE_BACKEND", "postgres"):
            store = build_mcp_store()
        self.assertEqual(os.path.abspath(store.db_path), os.path.abspath(settings.AUTH_DB_PATH))


if __name__ == "__main__":
    unittest.main()
