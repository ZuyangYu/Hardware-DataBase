"""P0b: admission control (per-user/tenant RPM, queue backpressure) and 429s."""

from __future__ import annotations

import gc
import os
import tempfile
import unittest

import httpx

import src.settings
from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.core.concurrency.admission import (
    AdmissionController,
    AdmissionRejected,
    get_admission_controller,
    reset_admission_controller,
)
from tests._api_stub import Server, StubPipeline, make_auth


class AdmissionControllerTests(unittest.TestCase):
    def tearDown(self):
        reset_admission_controller()
        super().tearDown()

    def test_user_rate_limit_rejects_second_call(self):
        controller = AdmissionController(enabled=True, chat_rpm_per_user=1, queue_depth_provider=None)
        controller.check(user_id=1, tenant_id=None)
        with self.assertRaises(AdmissionRejected) as ctx:
            controller.check(user_id=1, tenant_id=None)
        self.assertEqual(ctx.exception.reason, "rate_limit_user")
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_tenant_limit_is_shared_across_users(self):
        controller = AdmissionController(
            enabled=True,
            chat_rpm_per_user=1000,
            chat_rpm_per_tenant=1,
            queue_depth_provider=None,
        )
        controller.check(user_id=1, tenant_id="dept-1")
        with self.assertRaises(AdmissionRejected) as ctx:
            controller.check(user_id=2, tenant_id="dept-1")
        self.assertEqual(ctx.exception.reason, "rate_limit_tenant")

    def test_queue_depth_rejects_before_accepting(self):
        controller = AdmissionController(
            enabled=True,
            chat_rpm_per_user=1000,
            queue_depth_provider=lambda: 10,
            queue_depth_limit=5,
        )
        with self.assertRaises(AdmissionRejected) as ctx:
            controller.check(user_id=1)
        self.assertEqual(ctx.exception.reason, "queue_full")

    def test_disabled_controller_admits_everything(self):
        controller = AdmissionController(enabled=False)
        for _ in range(100):
            controller.check(user_id=1, tenant_id="dept-1")

    def test_resource_type_buckets_are_independent(self):
        controller = AdmissionController(
            enabled=True,
            chat_rpm_per_user=1000,
            heavy_rpm_per_user=1000,
            kind_limits={
                "deep": (1, 1000),
                "document": (1000, 1000),
                "export": (1000, 1000),
                "evaluation": (1000, 1000),
            },
            queue_depth_provider=None,
        )
        controller.check(user_id=1, resource="deep")
        with self.assertRaises(AdmissionRejected) as ctx:
            controller.check(user_id=1, resource="deep")
        self.assertEqual(ctx.exception.reason, "rate_limit_user")
        # Other resource types keep their own buckets.
        controller.check(user_id=1, resource="document")
        controller.check(user_id=1, resource="export")
        controller.check(user_id=1, resource="evaluation")

    def test_per_model_dimension(self):
        controller = AdmissionController(
            enabled=True,
            chat_rpm_per_user=1000,
            model_rpm_per_user=1,
            queue_depth_provider=None,
        )
        controller.check(user_id=1, model="model-a")
        with self.assertRaises(AdmissionRejected) as ctx:
            controller.check(user_id=1, model="model-a")
        self.assertEqual(ctx.exception.reason, "rate_limit_model")
        # A different model is not affected by model-a's bucket.
        controller.check(user_id=1, model="model-b")
        # No model key means no model bucket.
        for _ in range(5):
            controller.check(user_id=1)

    def test_unknown_resource_inherits_chat_and_heavy_defaults(self):
        controller = AdmissionController(
            enabled=True, chat_rpm_per_user=1, heavy_rpm_per_user=10, queue_depth_provider=None
        )
        controller.check(user_id=1, resource="deep")
        with self.assertRaises(AdmissionRejected):
            controller.check(user_id=1, resource="deep")
        controller.check(user_id=1, resource="document")

    def test_singleton_reset(self):
        first = get_admission_controller()
        self.assertIs(first, get_admission_controller())
        reset_admission_controller()
        self.assertIsNot(first, get_admission_controller())


class AdmissionApiTests(unittest.TestCase):
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
        self.addCleanup(setattr, src.settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw)
        self._old_rpm = src.settings.ADMISSION_CHAT_RPM_PER_USER
        src.settings.ADMISSION_CHAT_RPM_PER_USER = 1
        self.addCleanup(setattr, src.settings, "ADMISSION_CHAT_RPM_PER_USER", self._old_rpm)
        self._old_enabled = src.settings.ADMISSION_CONTROL_ENABLED
        src.settings.ADMISSION_CONTROL_ENABLED = True
        self.addCleanup(setattr, src.settings, "ADMISSION_CONTROL_ENABLED", self._old_enabled)
        reset_admission_controller()
        self.addCleanup(reset_admission_controller)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_turn_creation_returns_429_with_retry_after(self):
        login = self.client.post("/api/v1/login", json={"username": "user1", "password": "pw123456"})
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["token"]
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "限流测试"},
            headers=self._auth(token),
        ).json()
        first = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "第一个问题"},
            headers=self._auth(token),
        )
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "第二个问题"},
            headers=self._auth(token),
        )
        self.assertEqual(second.status_code, 429, second.text)
        self.assertIn("Retry-After", second.headers)
        self.assertEqual(second.json()["detail"]["reason"], "rate_limit_user")

    def test_idempotent_replay_bypasses_admission_and_returns_original_turn(self):
        login = self.client.post("/api/v1/login", json={"username": "user1", "password": "pw123456"})
        token = login.json()["token"]
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "幂等重放"},
            headers=self._auth(token),
        ).json()
        payload = {"query": "同一个问题", "client_request_id": "replay-1"}
        first = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json=payload,
            headers=self._auth(token),
        )
        self.assertEqual(first.status_code, 201, first.text)
        # The per-user RPM is 1; the replay must still return the original turn.
        replay = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json=payload,
            headers=self._auth(token),
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["turn"]["id"], first.json()["turn"]["id"])

    def test_session_and_user_backlog_return_429(self):
        login = self.client.post("/api/v1/login", json={"username": "user1", "password": "pw123456"})
        token = login.json()["token"]
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "积压测试"},
            headers=self._auth(token),
        ).json()
        old_session_cap = src.settings.ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION
        old_user_cap = src.settings.ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER
        src.settings.ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION = 1
        src.settings.ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER = 100
        src.settings.ADMISSION_CHAT_RPM_PER_USER = 1000
        self.addCleanup(
            setattr, src.settings, "ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION", old_session_cap
        )
        self.addCleanup(
            setattr, src.settings, "ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER", old_user_cap
        )
        first = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "积压一"},
            headers=self._auth(token),
        )
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "积压二"},
            headers=self._auth(token),
        )
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["detail"]["reason"], "session_backlog")
        self.assertIn("Retry-After", second.headers)


if __name__ == "__main__":
    unittest.main()
