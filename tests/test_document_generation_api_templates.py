import os
import tempfile
import unittest

import config.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline

from tests._api_stub import Server, StubPipeline, make_auth


class _Analysis:
    analysis_id = "a1"
    template_version_id = "tv1"
    format = "xlsx"
    status = "ready_for_confirmation"
    units = [type("U", (), {"unit_id": "u1", "label": "型号", "writable": True, "blocked_reason": None})()]
    suggestions = [type("S", (), {"semantic_unit_id": "s1", "label": "型号", "confidence": 0.9})()]


class DocGenTemplateApiTests(unittest.TestCase):
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
        self.addCleanup(setattr, config.settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        old_db = config.settings.AUTH_DB_PATH
        config.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, config.settings, "AUTH_DB_PATH", old_db)
        self.auth, self.dept, self.admin, self.user = make_auth(self.db_path)
        self.stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.stub
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)

    def _token(self, username, password="pw123456"):
        r = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_template_analyze_returns_safe_view(self):
        self.stub.analyze_document_template = lambda ctx, *, filename, content, template_name: _Analysis()
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(t),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["analysis_id"], "a1")
        self.assertEqual(body["units"][0]["writable"], True)
        self.assertNotIn("locator", str(body))  # 绝不暴露 OOXML locator
        self.assertNotIn("PK", str(body))  # 绝不回传上传字节

    def test_template_analyze_system_admin_blocked(self):
        t = self._token(config.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(t),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )
        self.assertEqual(r.status_code, 403)

    def test_template_analyze_permission_denied_403(self):
        def _denied(ctx, *, filename, content, template_name):
            raise PermissionError("denied")

        self.stub.analyze_document_template = _denied
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(t),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("denied", r.text)

    def test_options_require_read_permission(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/document-generation/options?kb=shared", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["knowledge_bases"], ["shared"])