from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

import httpx
from fastapi import HTTPException

import src.settings
from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.api.routes.exports import _authorize_snapshot
from src.core.conversation import ConversationService
from src.core.auth import AuthUser
from src.result_exports.store import ResultExportStore
from src.result_exports.worker import ResultExportWorker

from tests._api_stub import Server, make_auth


class ResultExportApiTests(unittest.TestCase):
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
        old_db = src.settings.AUTH_DB_PATH
        old_export_storage = getattr(src.settings, "RESULT_EXPORT_STORAGE_DIR", "")
        src.settings.AUTH_DB_PATH = self.db_path
        src.settings.RESULT_EXPORT_STORAGE_DIR = os.path.join(self.tmp.name, "exports")
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", old_db)
        self.addCleanup(setattr, src.settings, "RESULT_EXPORT_STORAGE_DIR", old_export_storage)
        self.auth, _dept, _admin, self.user = make_auth(self.db_path)
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)
        self.session = ConversationService(self.db_path).create_session(self.user.id, "shared")
        conversation = ConversationService(self.db_path)
        turn = conversation.create_turn(self.user.id, self.session.id, "查询电源芯片", client_request_id="turn-1", query_mode="deep")
        self.turn = conversation.complete_turn(
            self.user.id,
            turn.id,
            "找到 1 条记录。",
            {"evidence": [{"file_name": "power.xlsx", "text": "TPS62130"}]},
        )

    def _token(self, username, password="pw123456"):
        response = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_create_export_returns_queued_jobs_for_a_completed_turn(self):
        token = self._token("user1")
        response = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["md", "xlsx"],
                "content_shape": "report",
                "client_request_id": "batch-1",
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["session_id"], self.session.id)
        self.assertEqual({job["format"] for job in payload["jobs"]}, {"md", "xlsx"})
        self.assertTrue(all(job["status"] == "queued" for job in payload["jobs"]))
        self.assertTrue(all(job["knowledge_base_name"] == "shared" for job in payload["jobs"]))
        self.assertTrue(all(job["department_id"] == str(self.user.department_id) for job in payload["jobs"]))

        listed = self.client.get(
            f"/api/v1/exports?session_id={self.session.id}",
            headers=self._auth(token),
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()), 2)

        messages = self.client.get(
            f"/api/v1/conversations/{self.session.id}/messages",
            headers=self._auth(token),
        )
        self.assertEqual(messages.status_code, 200, messages.text)
        assistant = next(item for item in messages.json() if item["role"] == "assistant")
        self.assertEqual(assistant["turn_id"], self.turn.id)

    def test_existing_snapshot_can_be_reused_without_rerunning_the_turn(self):
        token = self._token("user1")
        first = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["md"],
                "client_request_id": "snapshot-source-first",
            },
        )
        self.assertEqual(first.status_code, 202, first.text)
        snapshot_id = first.json()["snapshot_id"]

        second = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "snapshot", "id": snapshot_id},
                "formats": ["pdf"],
                "client_request_id": "snapshot-source-second",
            },
        )

        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["snapshot_id"], snapshot_id)
        self.assertEqual(second.json()["jobs"][0]["format"], "pdf")

    def test_other_user_cannot_export_or_download_this_turn(self):
        token = self._token("admin1")
        response = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["md"],
                "client_request_id": "foreign-1",
            },
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_create_export_supports_document_and_presentation_formats(self):
        token = self._token("user1")
        response = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["word", "pdf", "powerpoint"],
                "options": {"theme": "dark", "include_charts": True},
                "client_request_id": "rich-formats-1",
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual({job["format"] for job in payload["jobs"]}, {"docx", "pdf", "pptx"})
        self.assertTrue(all(job["status"] == "queued" for job in payload["jobs"]))

    def test_create_export_rejects_unknown_renderer_options(self):
        token = self._token("user1")
        response = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["pptx"],
                "options": {"theme": "neon"},
                "client_request_id": "invalid-render-options-1",
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("presentation theme", response.text)

    def test_formats_endpoint_returns_only_server_enabled_formats(self):
        token = self._token("user1")
        response = self.client.get("/api/v1/exports/formats", headers=self._auth(token))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), ["md", "xlsx", "docx", "pdf", "pptx"])

    def test_create_export_rejects_a_disabled_format_before_creating_a_snapshot(self):
        token = self._token("user1")
        previous = src.settings.RESULT_EXPORT_PDF_ENABLED
        src.settings.RESULT_EXPORT_PDF_ENABLED = False
        self.addCleanup(setattr, src.settings, "RESULT_EXPORT_PDF_ENABLED", previous)

        response = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["pdf"],
                "client_request_id": "disabled-pdf-1",
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("disabled", response.text)
        store = ResultExportStore(self.db_path, storage_dir=os.path.join(self.tmp.name, "exports"))
        self.assertEqual(store.list_export_jobs(self.user.id, session_id=self.session.id), [])

    def test_scoped_result_is_not_authorized_when_current_user_has_no_department(self):
        user_without_department = AuthUser(
            id=self.user.id,
            username=self.user.username,
            role="user",
            is_active=True,
            department_id=None,
        )
        snapshot = SimpleNamespace(
            department_id=str(self.user.department_id),
            knowledge_base_name="",
            envelope=SimpleNamespace(metadata={}),
        )

        with self.assertRaises(HTTPException) as raised:
            # Keep the assertion at the HTTP helper boundary without allowing
            # a real request to mutate the test database.
            _authorize_snapshot(user_without_department, snapshot, self.auth)
        self.assertEqual(raised.exception.status_code, 403)

    def test_worker_artifact_has_authenticated_preview_and_download_endpoint(self):
        token = self._token("user1")
        created = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["md"],
                "client_request_id": "download-1",
            },
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["jobs"][0]["export_job_id"]

        store = ResultExportStore(self.db_path, storage_dir=os.path.join(self.tmp.name, "exports"))
        self.assertTrue(ResultExportWorker(store=store, worker_id="test-export-worker").run_once())
        job = store.get_export_job(self.user.id, job_id)
        self.assertIsNotNone(job)
        self.assertIsNotNone(job.artifact_id)
        artifact = store.get_artifact(self.user.id, job.artifact_id)
        self.assertIsNotNone(artifact)

        preview = self.client.get(
            f"/api/v1/artifacts/{job.artifact_id}/preview",
            headers=self._auth(token),
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["format"], "md")
        self.assertEqual(
            preview.json()["download_url"],
            f"/api/v1/artifacts/{job.artifact_id}/download",
        )
        self.assertEqual(
            preview.json()["preview_url"],
            f"/api/v1/artifacts/{job.artifact_id}/preview",
        )

        download = self.client.get(
            f"/api/v1/artifacts/{job.artifact_id}/download",
            headers=self._auth(token),
        )
        self.assertEqual(download.status_code, 200, download.text)
        self.assertIn("找到 1 条记录".encode("utf-8"), download.content)
        self.assertIn("attachment", download.headers.get("content-disposition", ""))

        with sqlite3.connect(self.db_path) as connection:
            actions = {
                row[0]
                for row in connection.execute(
                    "SELECT action FROM audit_events WHERE target_type = 'export_artifact'"
                ).fetchall()
            }
        self.assertIn("download_export_artifact", actions)

    def test_artifact_preview_contains_bounded_content_and_history_survives_expiry(self):
        token = self._token("user1")
        created = self.client.post(
            "/api/v1/exports",
            headers=self._auth(token),
            json={
                "source_ref": {"kind": "turn", "id": self.turn.id},
                "formats": ["xlsx"],
                "client_request_id": "preview-history-1",
            },
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["jobs"][0]["export_job_id"]
        store = ResultExportStore(self.db_path, storage_dir=os.path.join(self.tmp.name, "exports"))
        self.assertTrue(ResultExportWorker(store=store, worker_id="preview-worker").run_once())
        job = store.get_export_job(self.user.id, job_id)
        self.assertIsNotNone(job)
        self.assertIsNotNone(job.artifact_id)
        artifact = store.get_artifact(self.user.id, job.artifact_id)
        self.assertIsNotNone(artifact)

        preview = self.client.get(
            f"/api/v1/artifacts/{job.artifact_id}/preview",
            headers=self._auth(token),
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["preview"]["sheets"][0]["rows"])

        store.cleanup_expired(now=datetime.now(timezone.utc) + timedelta(days=31))
        history = self.client.get(
            f"/api/v1/artifacts?session_id={self.session.id}",
            headers=self._auth(token),
        )
        self.assertEqual(history.status_code, 200, history.text)
        entry = next(item for item in history.json() if item["artifact_id"] == job.artifact_id)
        self.assertFalse(entry["available"])
        self.assertEqual(entry["preview_url"], "")
        self.assertEqual(entry["download_url"], "")
        self.assertEqual(entry["snapshot_id"], created.json()["snapshot_id"])
        self.assertEqual(entry["sha256"], artifact.sha256)

    def test_legacy_document_artifact_has_unified_preview_and_download_routes(self):
        token = self._token("user1")
        content = b"legacy-document-bytes"
        legacy_store = SimpleNamespace(
            get_artifact=lambda artifact_id: SimpleNamespace(
                artifact_id=artifact_id,
                tenant_id="default",
                work_order_id="legacy-wo-1",
                stage="review_candidate",
                created_at=datetime.now(timezone.utc),
            ),
            get_work_order=lambda _work_order_id: SimpleNamespace(
                tenant_id="default",
                knowledge_base_name="shared",
                resource_department_id=str(self.user.department_id),
                target_format="docx",
            ),
            read_artifact_content=lambda _artifact_id: content,
        )
        legacy_service = SimpleNamespace(store=legacy_store)
        self.app.dependency_overrides[get_pipeline] = lambda: SimpleNamespace(
            document_generation=legacy_service,
        )

        preview = self.client.get(
            "/api/v1/artifacts/document/legacy-artifact-1/preview",
            headers=self._auth(token),
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["artifact_id"], "legacy-artifact-1")
        self.assertEqual(preview.json()["format"], "docx")
        self.assertEqual(
            preview.json()["download_url"],
            "/api/v1/artifacts/document/legacy-artifact-1/download",
        )

        download = self.client.get(
            "/api/v1/artifacts/document/legacy-artifact-1/download",
            headers=self._auth(token),
        )
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, content)
        self.assertIn("attachment", download.headers.get("content-disposition", ""))

    def test_project_document_artifact_uses_project_capability_for_unified_route(self):
        token = self._token("user1")
        content = b"project-document-bytes"
        capability_calls = []
        legacy_store = SimpleNamespace(
            get_artifact=lambda artifact_id: SimpleNamespace(
                artifact_id=artifact_id,
                tenant_id="default",
                work_order_id="project-wo-1",
                stage="approved_release",
                created_at=datetime.now(timezone.utc),
            ),
            get_work_order=lambda _work_order_id: SimpleNamespace(
                tenant_id="default",
                scope_type="project",
                project_id="project-1",
                knowledge_base_name=None,
                resource_department_id=None,
                target_format="xlsx",
            ),
            read_artifact_content=lambda _artifact_id: content,
        )
        legacy_service = SimpleNamespace(
            store=legacy_store,
            require_work_order_capability=lambda _ctx, _order, capability: capability_calls.append(capability),
        )
        self.app.dependency_overrides[get_pipeline] = lambda: SimpleNamespace(
            document_generation=legacy_service,
        )

        preview = self.client.get(
            "/api/v1/artifacts/document/project-artifact-1/preview",
            headers=self._auth(token),
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["format"], "xlsx")
        self.assertEqual(capability_calls, ["download_approved_release"])
