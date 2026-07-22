"""Tests for the MCP server (src/mcp/server.py).

The MCP tools are thin wrappers over the CLI's ApiClient, so these tests stand
up the same stub API server the CLI tests use, point the MCP server at it via
HDB_API_URL/HDB_TOKEN, and call the tool functions directly (the @mcp.tool
decorator preserves direct callability). Covers the happy path for every tool
plus structured-error handling on permission failures and a down server.
"""
import os
import tempfile
import unittest

import httpx

import config.settings

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.mcp import server as mcpserver

from tests._api_stub import Server, StubPipeline, make_auth


class McpServerTests(unittest.TestCase):
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
        self._old_pw = config.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        config.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, config.settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._old_db = config.settings.AUTH_DB_PATH
        config.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, config.settings, "AUTH_DB_PATH", self._old_db)
        self.auth, self.dept, self.admin, self.user = make_auth(self.db_path)
        self.stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.stub
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)

        # Point the MCP server at the stub and auth as admin1 (dept_admin).
        self._old_env = (os.environ.get("HDB_API_URL"), os.environ.get("HDB_TOKEN"))
        os.environ["HDB_API_URL"] = self.url
        os.environ["HDB_TOKEN"] = self._login("admin1", "pw123456")
        self.addCleanup(self._restore_env)

    def _login(self, username: str, password: str) -> str:
        return httpx.post(
            f"{self.url}/login", json={"username": username, "password": password}
        ).json()["token"]

    def _restore_env(self) -> None:
        for key, val in zip(("HDB_API_URL", "HDB_TOKEN"), self._old_env):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_health_ok(self):
        r = mcpserver.health()
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["authed"])
        self.assertEqual(r["user"]["username"], "admin1")

    def test_whoami_and_list_kbs(self):
        self.assertEqual(mcpserver.whoami()["role"], "dept_admin")
        kbs = mcpserver.list_kbs()
        self.assertEqual(kbs[0]["name"], "shared")

    def test_list_files(self):
        files = mcpserver.list_files("shared")
        self.assertEqual(files[0]["name"], "a.pdf")
        self.assertEqual(files[0]["processor_kind"], "document_rag")

    def test_query_aggregates_sse(self):
        r = mcpserver.query("shared", "电源拓扑怎么选")
        self.assertEqual(r["answer"], "第一段第二段")
        self.assertEqual(r["summary"]["status"], "success")
        self.assertEqual(r["footer"], "footer")
        self.assertEqual(r["token_usage"]["total_tokens"], 10)

    def test_upload_admin_ok(self):
        path = os.path.join(self.tmp.name, "f.pdf")
        with open(path, "wb") as wb:
            wb.write(b"x")
        r = mcpserver.upload("shared", [path])
        self.assertEqual(r["success_count"], 1)
        self.assertEqual(self.stub.uploaded[0][0], "shared")

    def test_upload_user_forbidden_returns_error_dict(self):
        # user1 is read-only -> upload must 403, surfaced as a dict, not raised.
        os.environ["HDB_TOKEN"] = self._login("user1", "pw123456")
        path = os.path.join(self.tmp.name, "g.pdf")
        with open(path, "wb") as wb:
            wb.write(b"x")
        r = mcpserver.upload("shared", [path])
        self.assertEqual(r.get("status_code"), 403)
        self.assertIn("error", r)

    def test_health_server_down(self):
        old = os.environ["HDB_API_URL"]
        os.environ["HDB_API_URL"] = "http://127.0.0.1:1"
        try:
            r = mcpserver.health()
        finally:
            os.environ["HDB_API_URL"] = old
        self.assertEqual(r["status"], "server_down")

    def test_health_unauthed(self):
        # No token -> server_up, authed=False (server is reachable but no creds).
        old = os.environ.pop("HDB_TOKEN", None)
        try:
            r = mcpserver.health()
        finally:
            if old is not None:
                os.environ["HDB_TOKEN"] = old
        self.assertEqual(r["status"], "server_up")
        self.assertFalse(r["authed"])

    def test_all_tools_registered(self):
        import asyncio

        names = {t.name for t in asyncio.run(mcpserver.mcp.list_tools())}
        self.assertEqual(
            names, {"health", "whoami", "list_kbs", "list_files", "query", "upload", "delete"}
        )
