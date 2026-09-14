"""HTTP API tests for chat attachments (upload/list/get/delete/retry + ACL)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest

import httpx

import src.settings
from src.attachments.store import AttachmentStore
from src.api.app import create_app
from src.api.deps import get_auth_service
from src.core.conversation import ConversationService

from tests._api_stub import Server, make_auth


class AttachmentApiTests(unittest.TestCase):
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
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._patch = []
        for name, value in (
            ("AUTH_DB_PATH", self.db_path),
            ("CHAT_ATTACHMENTS_ENABLED", True),
            ("CHAT_ATTACHMENT_INDEX_DB_PATH", os.path.join(self.tmp.name, "att.db")),
            ("CHAT_ATTACHMENT_STORAGE_DIR", os.path.join(self.tmp.name, "att-storage")),
        ):
            old = getattr(src.settings, name)
            setattr(src.settings, name, value)
            self.addCleanup(setattr, src.settings, name, old)
        self.auth, _dept, self.admin, self.user = make_auth(self.db_path)
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)
        self.session = ConversationService(self.db_path).create_session(self.user.id, "shared")

    def _token(self, username="user1"):
        response = self.client.post(
            "/api/v1/login", json={"username": username, "password": "pw123456"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def _headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _upload(self, token, filename="notes.txt", content=b"hello attachment", **params):
        files = {"file": (filename, io.BytesIO(content), "text/plain")}
        return self.client.post(
            f"/api/v1/conversations/{self.session.id}/attachments",
            headers=self._headers(token),
            files=files,
            params=params,
        )

    def test_flag_disabled_returns_404(self):
        old = src.settings.CHAT_ATTACHMENTS_ENABLED
        src.settings.CHAT_ATTACHMENTS_ENABLED = False
        self.addCleanup(setattr, src.settings, "CHAT_ATTACHMENTS_ENABLED", old)
        response = self._upload(self._token())
        self.assertEqual(response.status_code, 404, response.text)

    def test_upload_list_get_delete_roundtrip(self):
        token = self._token()
        created = self._upload(token, client_request_id="crid-1")
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["filename"], "notes.txt")
        self.assertEqual(body["parse_status"], "queued")
        self.assertEqual(body["usage_hint"], "reference")
        self.assertIsNotNone(body["expires_at"])

        # Idempotent re-upload with the same client_request_id.
        again = self._upload(token, client_request_id="crid-1")
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()["attachment_id"], body["attachment_id"])

        listed = self.client.get(
            f"/api/v1/conversations/{self.session.id}/attachments",
            headers=self._headers(token),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)

        detail = self.client.get(
            f"/api/v1/conversations/{self.session.id}/attachments/{body['attachment_id']}",
            headers=self._headers(token),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["sha256"], body["sha256"])

        deleted = self.client.delete(
            f"/api/v1/conversations/{self.session.id}/attachments/{body['attachment_id']}",
            headers=self._headers(token),
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        missing = self.client.get(
            f"/api/v1/conversations/{self.session.id}/attachments/{body['attachment_id']}",
            headers=self._headers(token),
        )
        self.assertEqual(missing.status_code, 404)

    def test_attachment_detail_exposes_manifest_and_degraded_reasons(self):
        token = self._token()
        created = self.client.post(
            f"/api/v1/conversations/{self.session.id}/attachments",
            headers=self._headers(token),
            files={"file": ("scan.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        )
        self.assertEqual(created.status_code, 201, created.text)
        attachment_id = created.json()["attachment_id"]
        asset_id = created.json()["asset_id"]
        AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH).update_asset_status(
            asset_id,
            parse_status="degraded",
            manifest={"format": "pdf", "degraded_reasons": ["第 1 页无可提取文本"]},
        )

        detail = self.client.get(
            f"/api/v1/conversations/{self.session.id}/attachments/{attachment_id}",
            headers=self._headers(token),
        )

        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["manifest"]["format"], "pdf")
        self.assertEqual(detail.json()["degraded_reasons"], ["第 1 页无可提取文本"])

    def test_unsupported_extension_rejected(self):
        token = self._token()
        response = self._upload(token, filename="legacy.xls", content=b"old")
        self.assertEqual(response.status_code, 400, response.text)

    def test_streaming_size_limit_returns_payload_too_large(self):
        old_limit = src.settings.CHAT_ATTACHMENT_MAX_BYTES
        src.settings.CHAT_ATTACHMENT_MAX_BYTES = 3
        self.addCleanup(setattr, src.settings, "CHAT_ATTACHMENT_MAX_BYTES", old_limit)

        response = self._upload(self._token(), content=b"more than three bytes")

        self.assertEqual(response.status_code, 413, response.text)

    def test_other_user_session_attachment_is_404(self):
        owner_token = self._token("user1")
        created = self._upload(owner_token)
        self.assertEqual(created.status_code, 201)
        attachment_id = created.json()["attachment_id"]

        # user2 uploads to their own session, then tries to read user1's row
        # through their own session id: must 404 without leaking existence.
        self.auth.create_user_as(
            self.admin, "user2", "pw123456", role="user", department_id=self.user.department_id
        )
        other_token = self._token("user2")
        other_session = ConversationService(self.db_path).create_session(
            self.auth.get_user_by_username("user2").id, "shared"
        )
        sneaky = self.client.get(
            f"/api/v1/conversations/{other_session.id}/attachments/{attachment_id}",
            headers=self._headers(other_token),
        )
        self.assertEqual(sneaky.status_code, 404)

    def test_create_turn_with_attachments(self):
        token = self._token()
        created = self._upload(token)
        attachment_id = created.json()["attachment_id"]
        turn = self.client.post(
            f"/api/v1/conversations/{self.session.id}/turns",
            headers=self._headers(token),
            json={
                "query": "总结附件内容",
                "attachment_ids": [attachment_id],
                "source_scope": "auto",
            },
        )
        self.assertEqual(turn.status_code, 201, turn.text)
        payload = turn.json()
        self.assertEqual(payload["turn"]["status"], "waiting_for_attachments")
        # KB session + attachment -> combined scope (design §5.2).
        self.assertEqual(payload["turn"]["source_scope"], "attachment_and_knowledge_base")
        self.assertEqual(len(payload["turn"]["attachments"]), 1)
        self.assertEqual(payload["user_message"]["attachments"][0]["attachment_id"], attachment_id)

    def test_history_marks_deleted_turn_attachment(self):
        token = self._token()
        created = self._upload(token, filename="design-review.txt")
        attachment_id = created.json()["attachment_id"]
        turn = self.client.post(
            f"/api/v1/conversations/{self.session.id}/turns",
            headers=self._headers(token),
            json={"query": "分析附件", "attachment_ids": [attachment_id]},
        )
        self.assertEqual(turn.status_code, 201, turn.text)

        deleted = self.client.delete(
            f"/api/v1/conversations/{self.session.id}/attachments/{attachment_id}",
            headers=self._headers(token),
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

        history = self.client.get(
            f"/api/v1/conversations/{self.session.id}/messages",
            headers=self._headers(token),
        )
        self.assertEqual(history.status_code, 200, history.text)
        user_message = next(item for item in history.json() if item["role"] == "user")
        self.assertTrue(user_message["attachments"][0]["deleted"])

    def test_knowledge_base_only_does_not_widen_to_attachment_scope(self):
        token = self._token()
        created = self._upload(token)
        attachment_id = created.json()["attachment_id"]
        turn = self.client.post(
            f"/api/v1/conversations/{self.session.id}/turns",
            headers=self._headers(token),
            json={
                "query": "只查询知识库",
                "attachment_ids": [attachment_id],
                "source_scope": "knowledge_base_only",
            },
        )
        self.assertEqual(turn.status_code, 201, turn.text)
        payload = turn.json()
        self.assertEqual(payload["turn"]["status"], "pending")
        self.assertEqual(payload["turn"]["source_scope"], "knowledge_base_only")
        self.assertEqual(payload["turn"]["attachments"], [])
        self.assertEqual(payload["user_message"].get("attachments", []), [])

    def test_create_turn_with_blocked_attachment_rejected(self):
        token = self._token()
        turn = self.client.post(
            f"/api/v1/conversations/{self.session.id}/turns",
            headers=self._headers(token),
            json={
                "query": "总结附件内容",
                "attachment_ids": ["att-does-not-exist"],
            },
        )
        self.assertEqual(turn.status_code, 400, turn.text)
        detail = turn.json()["detail"]
        self.assertEqual(detail["error_code"], "attachment_unavailable")


if __name__ == "__main__":
    unittest.main()
