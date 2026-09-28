"""Self-service password change: ``PUT /api/v1/users/me/password``.

Employees and system administrators may rotate their own password from the
account settings page; the current password is required and every session of
the account is revoked afterwards.
"""
import gc
import os
import tempfile
import unittest

import httpx

import src.settings

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from tests._api_stub import Server, StubPipeline, make_auth


class UsersSelfPasswordTests(unittest.TestCase):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, src.settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._old_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", self._old_db)
        self.auth, self.dept, self.admin, self.user = make_auth(self.db_path)
        self.stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.stub
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.addCleanup(gc.collect)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)

    # -- helpers ---------------------------------------------------------
    def _login(self, username: str, password: str):
        return self.client.post("/api/v1/login", json={"username": username, "password": password})

    def _token(self, username: str, password: str = "pw123456") -> str:
        r = self._login(username, password)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _change(self, token: str, current: str, new: str):
        return self.client.put(
            "/api/v1/users/me/password",
            json={"current_password": current, "new_password": new},
            headers=self._headers(token),
        )

    # -- tests -----------------------------------------------------------
    def test_employee_changes_own_password(self):
        token = self._token("user1")
        r = self._change(token, "pw123456", "BrandNewPass1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

        # 旧密码失效,新密码可登录
        self.assertEqual(self._login("user1", "pw123456").status_code, 401)
        self.assertEqual(self._login("user1", "BrandNewPass1").status_code, 200)

    def test_change_revokes_existing_sessions(self):
        token = self._token("user1")
        self.assertEqual(self._change(token, "pw123456", "BrandNewPass2").status_code, 200)
        # 改密后旧 token 立即失效
        r = self.client.get("/api/v1/kbs", headers=self._headers(token))
        self.assertEqual(r.status_code, 401)

    def test_wrong_current_password_is_rejected(self):
        token = self._token("user1")
        r = self._change(token, "not-my-password", "BrandNewPass3")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "当前密码不正确")
        # 原密码仍然有效
        self.assertEqual(self._login("user1", "pw123456").status_code, 200)

    def test_short_new_password_is_rejected(self):
        token = self._token("user1")
        r = self._change(token, "pw123456", "short")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "密码长度不能少于 8 位")

    def test_same_password_is_rejected(self):
        token = self._token("user1")
        r = self._change(token, "pw123456", "pw123456")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "新密码不能与当前密码相同")

    def test_requires_authentication(self):
        r = self.client.put(
            "/api/v1/users/me/password",
            json={"current_password": "pw123456", "new_password": "BrandNewPass4"},
        )
        self.assertEqual(r.status_code, 401)

    def test_system_admin_changes_own_password(self):
        admin_name = src.settings.AUTH_DEFAULT_ADMIN_USERNAME
        token = self._token(admin_name, "StrongTestPassword123!")
        r = self._change(token, "StrongTestPassword123!", "AdminOwnPass1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._login(admin_name, "AdminOwnPass1").status_code, 200)


if __name__ == "__main__":
    unittest.main()
