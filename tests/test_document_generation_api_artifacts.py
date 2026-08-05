import tempfile
import unittest

import config.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline

from tests._api_stub import Server, StubPipeline, make_auth


class DocGenArtifactApiTests(unittest.TestCase):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = __import__("os").path.join(self.tmp.name, "auth.db")
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

    def test_icd_scope_resolution_submits_and_continues(self):
        submitted = {}
        self.stub.submit_icd_scope_resolution = lambda ctx, work_order_id, *, resolutions, comment: submitted.setdefault("resolutions", resolutions)
        self.stub.submit_knowledge_base_document_generation = lambda ctx, work_order_id: "bg-9"
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/document-generation/work-orders/wo-1/icd-scope-resolution?kb=shared",
            headers=self._auth(t),
            json={"resolutions": [{"exception_id": "e1", "action": "include"}], "comment": "ok"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["run_id"], "bg-9")
        self.assertEqual(submitted["resolutions"][0]["action"], "include")

    def test_download_artifact_returns_bytes(self):
        self.stub.download_document_artifact = lambda ctx, artifact_id: b"FILEBYTES"
        t = self._token("user1")
        r = self.client.get("/api/v1/document-generation/artifacts/art-1/download?kb=shared", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, b"FILEBYTES")

    def test_preview_returns_safe_sheets(self):
        self.stub.preview_document_artifact = lambda ctx, artifact_id: {
            "format": "xlsx", "warnings": [], "sheets": [{"name": "S1", "rows": []}], "truncated": False,
        }
        t = self._token("user1")
        r = self.client.get("/api/v1/document-generation/artifacts/art-1/preview?kb=shared", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["sheets"][0]["name"], "S1")

    def test_feedback_and_approve(self):
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/document-generation/artifacts/art-1/feedback?kb=shared",
            headers=self._auth(t), json={"comment": "改一下"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r2 = self.client.post(
            "/api/v1/document-generation/artifacts/art-1/approve?kb=shared",
            headers=self._auth(t), json={"comment": "批准"},
        )
        self.assertEqual(r2.status_code, 200, r.text)