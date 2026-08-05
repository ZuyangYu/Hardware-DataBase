import tempfile
import unittest

import config.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline

from tests._api_stub import Server, StubPipeline, make_auth


class DocGenWorkOrderApiTests(unittest.TestCase):
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

    def test_create_returns_ready_stage(self):
        self.stub.prepare_knowledge_base_document_generation = (
            lambda ctx, *, knowledge_base_name, **kwargs: {"stage": "ready", "work_order_id": "wo-1"}
        )
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/document-generation/work-orders?kb=shared",
            headers=self._auth(t),
            json={"template_version_id": "t1", "document_schema_id": "s1", "document_schema_version": "1"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["stage"], "ready")
        self.assertEqual(r.json()["work_order_id"], "wo-1")

    def test_generate_submits_background(self):
        self.stub.submit_knowledge_base_document_generation = lambda ctx, work_order_id: "bg-7"
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/document-generation/work-orders/wo-1/generate?kb=shared",
            headers=self._auth(t),
            json={},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["run_id"], "bg-7")

    def test_list_work_orders(self):
        self.stub.list_knowledge_base_document_work_orders = lambda ctx, knowledge_base_name: [
            type("WO", (), {"work_order_id": "wo-1", "status": "planned", "model_dump": lambda self: {"work_order_id": self.work_order_id, "status": self.status}})()
        ]
        t = self._token("user1")
        r = self.client.get("/api/v1/document-generation/work-orders?kb=shared", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()[0]["work_order_id"], "wo-1")

    def test_status_requires_permission(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/document-generation/work-orders/wo-1/status?kb=shared", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)