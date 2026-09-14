"""Tests for the analyze-from-attachment bridge and attachment scope resolvers."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace

import src.settings
from src.agents.scopes import (
    AttachmentCircuitScopeResolver,
    AttachmentSpreadsheetScopeResolver,
    KnowledgeBaseSpreadsheetScopeResolver,
)
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore
from src.core.conversation import ConversationService


def _valid_office_stub() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    return output.getvalue()


class ScopeResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch("CHAT_ATTACHMENTS_ENABLED", True)
        self._patch("CHAT_ATTACHMENT_INDEX_DB_PATH", os.path.join(self.tmp.name, "att.db"))
        self._patch("CHAT_ATTACHMENT_STORAGE_DIR", os.path.join(self.tmp.name, "att-storage"))
        self.store = AttachmentStore(db_path=os.path.join(self.tmp.name, "att.db"))
        self.service = AttachmentService(store=self.store)

    def _patch(self, name, value):
        old = getattr(src.settings, name)
        setattr(src.settings, name, value)
        self.addCleanup(setattr, src.settings, name, old)

    def test_kb_spreadsheet_scope_requires_department(self):
        resolver = KnowledgeBaseSpreadsheetScopeResolver(
            lambda department_id, kb_name: "/tmp/unused.db"
        )
        with self.assertRaises(PermissionError):
            resolver.resolve(kb_name="shared", department_id=None)
        scope = resolver.resolve(kb_name="shared", department_id="3")
        self.assertEqual(scope.source_type, "knowledge_base")
        self.assertEqual(scope.department_id, "3")

    def test_attachment_spreadsheet_scope_from_manifest(self):
        conv = None  # scope resolvers only need asset rows + refs
        del conv
        record = self.service.upload(
            session_id=1, user_id=1, filename="t.xlsx",
            stream=io.BytesIO(_valid_office_stub()),
        )
        self.store.update_asset_status(
            record.asset_id, parse_status="ready",
            manifest={"index_db_path": "table_indexes/1/x/table_indexes.db", "record_id": 42},
        )
        ref = self.service.build_refs([record])[0]
        scope = AttachmentSpreadsheetScopeResolver(self.store).resolve(refs=[ref])
        self.assertEqual(scope.source_type, "chat_attachment")
        self.assertEqual(scope.allowed_record_ids, frozenset({42}))
        self.assertEqual(scope.db_path, "table_indexes/1/x/table_indexes.db")

    def test_attachment_circuit_scope_from_manifest(self):
        record = self.service.upload(
            session_id=1, user_id=1, filename="board.edf",
            stream=io.BytesIO(b"(edif board)"),
        )
        self.store.update_asset_status(
            record.asset_id, parse_status="ready",
            manifest={"circuit_root": "/tmp/att-root", "kb_name": "att_x"},
        )
        ref = self.service.build_refs([record])[0]
        scope = AttachmentCircuitScopeResolver(self.store).resolve(refs=[ref])
        self.assertEqual(scope.source_type, "chat_attachment")
        self.assertEqual(scope.store_root, "/tmp/att-root")
        self.assertIn(ref.attachment_id, scope.attachment_ids)


class AnalyzeFromAttachmentTests(unittest.TestCase):
    """Permission-first tests with a stubbed pipeline (no LLM)."""

    @classmethod
    def setUpClass(cls):
        from src.api.app import create_app
        from tests._api_stub import Server

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
        for name, value in (
            ("AUTH_DB_PATH", self.db_path),
            ("CHAT_ATTACHMENTS_ENABLED", True),
            ("CHAT_ATTACHMENT_INDEX_DB_PATH", os.path.join(self.tmp.name, "att.db")),
            ("CHAT_ATTACHMENT_STORAGE_DIR", os.path.join(self.tmp.name, "att-storage")),
        ):
            old = getattr(src.settings, name)
            setattr(src.settings, name, value)
            self.addCleanup(setattr, src.settings, name, old)
        from src.api.deps import get_auth_service, get_pipeline
        from tests._api_stub import make_auth

        self.auth, _dept, self.admin, self.user = make_auth(self.db_path)
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth

        self.captured: dict = {}

        class StubPipeline:
            def analyze_document_template(
                _pipeline,
                ctx,
                *,
                filename,
                content,
                template_name,
                origin_source_type=None,
                origin_attachment_id=None,
                origin_session_id=None,
                origin_content_hash=None,
            ):
                self.captured.update({
                    "filename": filename,
                    "content": content,
                    "kb": ctx.metadata.get("document_template_kb_name") if ctx else "",
                    "template_name": template_name,
                    "origin_source_type": origin_source_type,
                    "origin_attachment_id": origin_attachment_id,
                    "origin_session_id": origin_session_id,
                    "origin_content_hash": origin_content_hash,
                })
                return SimpleNamespace(
                    analysis_id="an-1", template_version_id="tv-1", format="docx",
                    status="ready_for_confirmation", units=[], suggestions=[],
                    activation_decision=None, locked_unit_ids=[],
                )

        self.pipeline_stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.pipeline_stub
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = __import__("httpx").Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)
        self.session = ConversationService(self.db_path).create_session(self.user.id, "shared")

    def _token(self, username="user1"):
        response = self.client.post(
            "/api/v1/login", json={"username": username, "password": "pw123456"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def test_convert_attachment_to_template_without_reupload(self):
        token = self._token()
        # user1 has only read permission on "shared" -> template conversion
        # (a KB write operation) must be rejected even though the upload is fine.
        upload = self.client.post(
            f"/api/v1/conversations/{self.session.id}/attachments",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("t.docx", io.BytesIO(_valid_office_stub()), "application/octet-stream")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        attachment_id = upload.json()["attachment_id"]

        denied = self.client.post(
            "/api/v1/document-generation/templates/analyze-from-attachment",
            headers={"Authorization": f"Bearer {token}"},
            params={"kb": "shared", "session_id": self.session.id, "attachment_id": attachment_id,
                    "template_name": "评审表"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        # Foreign attachment id -> still 403 for a caller without KB write
        # permission: permission checks run before existence is revealed.
        foreign = self.client.post(
            "/api/v1/document-generation/templates/analyze-from-attachment",
            headers={"Authorization": f"Bearer {token}"},
            params={"kb": "shared", "session_id": self.session.id,
                    "attachment_id": "att-missing", "template_name": "评审表"},
        )
        self.assertEqual(foreign.status_code, 403)
        self.assertNotIn("content", self.captured)

    def test_conversion_forwards_attachment_provenance_after_kb_write_grant(self):
        self.auth.grant_kb_permission_as(
            self.admin, "shared", self.user.id, "write"
        )
        token = self._token()
        upload = self.client.post(
            f"/api/v1/conversations/{self.session.id}/attachments",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("t.docx", io.BytesIO(_valid_office_stub()), "application/octet-stream")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        attachment = upload.json()

        response = self.client.post(
            "/api/v1/document-generation/templates/analyze-from-attachment",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "kb": "shared",
                "session_id": self.session.id,
                "attachment_id": attachment["attachment_id"],
                "template_name": "评审表",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.captured["filename"], "t.docx")
        self.assertEqual(self.captured["content"], _valid_office_stub())
        self.assertEqual(self.captured["template_name"], "评审表")
        self.assertEqual(self.captured["origin_source_type"], "chat_attachment")
        self.assertEqual(
            self.captured["origin_attachment_id"], attachment["attachment_id"]
        )
        self.assertEqual(self.captured["origin_session_id"], self.session.id)
        self.assertEqual(
            self.captured["origin_content_hash"], attachment["sha256"]
        )

    def test_conversion_auto_activates_an_auto_accepted_template(self):
        self.auth.grant_kb_permission_as(
            self.admin, "shared", self.user.id, "write"
        )
        old_enabled = src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES
        src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES = True
        self.addCleanup(
            setattr,
            src.settings,
            "DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES",
            old_enabled,
        )
        confirmed: list[tuple[str, str]] = []
        self.pipeline_stub.analyze_document_template = lambda ctx, **_kwargs: SimpleNamespace(
            analysis_id="an-auto",
            template_version_id="tv-auto",
            format="docx",
            status="ready_for_confirmation",
            units=[],
            suggestions=[],
            activation_decision=SimpleNamespace(status="auto_accepted"),
            locked_unit_ids=[],
        )
        self.pipeline_stub.confirm_document_template = lambda ctx, **kwargs: confirmed.append(
            (kwargs["analysis_id"], kwargs["display_name"])
        )
        token = self._token()
        upload = self.client.post(
            f"/api/v1/conversations/{self.session.id}/attachments",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("auto.docx", io.BytesIO(_valid_office_stub()), "application/octet-stream")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        response = self.client.post(
            "/api/v1/document-generation/templates/analyze-from-attachment",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "kb": "shared",
                "session_id": self.session.id,
                "attachment_id": upload.json()["attachment_id"],
                "template_name": "自动模板",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["auto_activated"])
        self.assertEqual(confirmed, [("an-auto", "自动模板")])


if __name__ == "__main__":
    unittest.main()
