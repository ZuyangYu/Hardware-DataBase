import gc
import os
import tempfile
import unittest

import config.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline

from tests._api_stub import Server, StubPipeline, make_auth


class ApiRoutesTests(unittest.TestCase):
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
        self.addCleanup(gc.collect)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)

    def _token(self, username: str, password: str = "pw123456") -> str:
        r = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_login_wrong_password(self):
        r = self.client.post("/api/v1/login", json={"username": "admin1", "password": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_whoami_requires_token(self):
        self.assertEqual(self.client.get("/api/v1/whoami").status_code, 401)

    def test_whoami_ok(self):
        t = self._token("admin1")
        r = self.client.get("/api/v1/whoami", headers=self._auth(t))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["username"], "admin1")
        self.assertEqual(body["role"], "dept_admin")

    def test_list_kb_returns_accessible(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/kbs", headers=self._auth(t))
        self.assertEqual(r.status_code, 200)
        kbs = r.json()
        self.assertEqual([k["name"] for k in kbs], ["shared"])
        self.assertEqual(kbs[0]["permission"], "read")

    def test_list_files_user_read_ok(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/files", headers=self._auth(t))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()[0]["name"], "a.pdf")

    def test_upload_user_forbidden(self):
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/kbs/shared/files",
            headers=self._auth(t),
            files=[("files", ("a.txt", b"hello"))],
        )
        self.assertEqual(r.status_code, 403)

    def test_upload_admin_ok(self):
        t = self._token("admin1")
        r = self.client.post(
            "/api/v1/kbs/shared/files",
            headers=self._auth(t),
            files=[("files", ("a.txt", b"hello"))],
            data={"source_group": "文档资料"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.stub.uploaded[0][0], "shared")
        self.assertEqual(self.stub.uploaded[0][2], "文档资料")

    def test_create_kb_requires_dept_admin(self):
        t = self._token("user1")
        r = self.client.post("/api/v1/kbs", json={"name": "newkb"}, headers=self._auth(t))
        self.assertEqual(r.status_code, 403)

    def test_create_kb_admin_ok(self):
        t = self._token("admin1")
        r = self.client.post("/api/v1/kbs", json={"name": "newkb"}, headers=self._auth(t))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stub.created, ["newkb"])

    def test_delete_requires_admin(self):
        t = self._token("user1")
        r = self.client.delete("/api/v1/kbs/shared/files/a.pdf", headers=self._auth(t))
        self.assertEqual(r.status_code, 403)

    def test_delete_admin_ok(self):
        t = self._token("admin1")
        r = self.client.delete("/api/v1/kbs/shared/files/a.pdf", headers=self._auth(t))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stub.deleted, [("shared", "a.pdf")])

    def test_query_no_permission(self):
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/query",
            json={"kb_name": "other_kb", "query": "问"},
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 403)

    def test_query_sse(self):
        t = self._token("user1")
        with self.client.stream(
            "POST", "/api/v1/query", json={"kb_name": "shared", "query": "问"}, headers=self._auth(t)
        ) as r:
            self.assertEqual(r.status_code, 200)
            body = b"".join(r.iter_bytes()).decode("utf-8")
        self.assertIn("event: delta", body)
        self.assertIn("第一段", body)
        self.assertIn("event: done", body)
        self.assertIn('"answer": "第一段第二段"', body)
        self.assertIn('"status": "success"', body)
