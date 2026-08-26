from __future__ import annotations

import os
import tempfile
import unittest

import src.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from tests._api_stub import Server, StubPipeline, make_auth


class DocumentGenerationSessionApiTests(unittest.TestCase):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        auth_db = os.path.join(self.tmp.name, "auth.db")
        old_auth_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = auth_db
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", old_auth_db)
        self.auth, _, _, _ = make_auth(auth_db)
        self.stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.stub
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)

    def _headers(self, username="user1"):
        response = self.client.post(
            "/api/v1/login",
            json={"username": username, "password": "pw123456"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def test_create_answer_and_confirm_clarification_session(self):
        calls: list[tuple[str, object]] = []
        self.stub.create_document_generation_session = lambda ctx, **kwargs: (
            calls.append(("create", kwargs))
            or {
                "session_id": "generation-session-1",
                "status": "needs_clarification",
                "brief": {"confirmed": False},
                "messages": [{
                    "message_id": "m1",
                    "role": "assistant",
                    "content": "请确认项目版本",
                    "question_id": "scope.revision",
                    "options": ["当前发布版本"],
                }],
            }
        )
        self.stub.answer_document_generation_session = lambda ctx, session_id, **kwargs: (
            calls.append(("answer", (session_id, kwargs)))
            or {
                "session_id": session_id,
                "status": "needs_clarification",
                "brief": {"confirmed": False, "scope": {"revision": kwargs["answer"]}},
                "messages": [],
            }
        )
        self.stub.confirm_document_generation_session = lambda ctx, session_id: (
            calls.append(("confirm", session_id))
            or {
                "session_id": session_id,
                "status": "ready_to_generate",
                "brief": {"confirmed": True},
                "messages": [],
            }
        )
        headers = self._headers("admin1")

        created = self.client.post(
            "/api/v1/document-generation/sessions?kb=shared",
            headers=headers,
            json={"template_version_id": "tv1", "purpose": "生成评审表"},
        )
        answered = self.client.post(
            "/api/v1/document-generation/sessions/generation-session-1/messages?kb=shared",
            headers=headers,
            json={"question_id": "scope.revision", "answer": "当前发布版本"},
        )
        confirmed = self.client.post(
            "/api/v1/document-generation/sessions/generation-session-1/confirm?kb=shared",
            headers=headers,
            json={},
        )

        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "ready_to_generate")
        self.assertEqual([call[0] for call in calls], ["create", "answer", "confirm"])

    def test_session_creation_requires_write_permission(self):
        headers = self._headers()
        response = self.client.post(
            "/api/v1/document-generation/sessions?kb=shared",
            headers=headers,
            json={"template_version_id": "tv1"},
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_work_order_creation_requires_write_permission(self):
        response = self.client.post(
            "/api/v1/document-generation/work-orders?kb=shared",
            headers=self._headers(),
            json={
                "template_version_id": "tv1",
                "document_schema_id": "schema-1",
                "document_schema_version": "1",
            },
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_generation_start_requires_write_permission(self):
        response = self.client.post(
            "/api/v1/document-generation/work-orders/wo-1/generate?kb=shared",
            headers=self._headers(),
            json={},
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_other_document_mutations_require_write_permission(self):
        headers = self._headers()
        requests = [
            (
                "/api/v1/document-generation/work-orders/wo-1/icd-scope-resolution?kb=shared",
                {"resolutions": [], "comment": ""},
            ),
            ("/api/v1/document-generation/harness-runs/run-1/pause?kb=shared", {}),
            ("/api/v1/document-generation/harness-runs/run-1/cancel?kb=shared", {}),
            ("/api/v1/document-generation/artifacts/artifact-1/feedback?kb=shared", {"comment": "反馈"}),
            ("/api/v1/document-generation/artifacts/artifact-1/approve?kb=shared", {"comment": "批准"}),
        ]

        for url, payload in requests:
            response = self.client.post(url, headers=headers, json=payload)
            self.assertEqual(response.status_code, 403, response.text)

    def test_template_sanitization_summary_allows_read_access(self):
        response = self.client.get(
            "/api/v1/document-generation/templates/tv1/sanitization?kb=shared",
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200, response.text)

    def test_confirmed_session_id_is_forwarded_when_creating_work_order(self):
        captured = {}

        def prepare(ctx, *, knowledge_base_name, **kwargs):
            captured.update(kwargs)
            return {"stage": "ready", "work_order_id": "wo-brief"}

        self.stub.prepare_knowledge_base_document_generation = prepare
        response = self.client.post(
            "/api/v1/document-generation/work-orders?kb=shared",
            headers=self._headers("admin1"),
            json={
                "template_version_id": "tv1",
                "document_schema_id": "schema-1",
                "document_schema_version": "1",
                "generation_session_id": "generation-session-1",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured["generation_session_id"], "generation-session-1")
