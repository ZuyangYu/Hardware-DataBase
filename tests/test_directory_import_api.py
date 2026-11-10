from __future__ import annotations

import gc
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import src.settings
from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.pipelines.document_store import PipelineDocumentStore
from src.services.pipeline_directories import PipelineDirectoryService

from tests._api_stub import Server, make_auth


class DirectoryImportApiTests(unittest.TestCase):
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
        old_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = os.path.join(self.tmp.name, "auth.db")
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", old_db)
        old_storage = src.settings.STORAGE_DIR
        src.settings.STORAGE_DIR = self.tmp.name
        self.addCleanup(setattr, src.settings, "STORAGE_DIR", old_storage)
        self.auth, self.dept, self.admin, self.user = make_auth(src.settings.AUTH_DB_PATH)
        self.store = PipelineDocumentStore(db_path=os.path.join(self.tmp.name, "pipeline.db"))
        self.directories = PipelineDirectoryService(store=self.store)
        self.pipeline = SimpleNamespace(
            backend=SimpleNamespace(store=self.store),
            documents=SimpleNamespace(),
        )
        from src.services.pipeline_directories import delete_kb_directory_tree

        self.pipeline.delete_directory_recursive = (
            lambda kb_name, directory_id, *, ctx=None: delete_kb_directory_tree(
                self.pipeline, kb_name, directory_id, ctx=ctx
            )
        )
        self.app.dependency_overrides[get_pipeline] = lambda: self.pipeline
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.addCleanup(gc.collect)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)

    def _token(self, username: str) -> str:
        response = self.client.post(
            "/api/v1/login", json={"username": username, "password": "pw123456"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token('user1')}"}

    def test_create_and_query_import_batch(self):
        manifest = json.dumps(
            {
                "entries": [
                    {"index": 0, "relative_path": "docs/spec.pdf", "source_group": "文档资料"},
                    {"index": 1, "relative_path": "bom/bom.xlsx", "source_group": "物料数据"},
                ]
            },
            ensure_ascii=False,
        )
        response = self.client.post(
            "/api/v1/kbs/shared/directory-imports",
            headers=self._headers(),
            files=[
                ("files", ("spec.pdf", b"%PDF-1.4", "application/pdf")),
                ("files", ("bom.xlsx", b"PK", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ],
            data={"directory_id": "0", "manifest": manifest},
        )
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual(job["total_files"], 2)
        self.assertEqual(job["status"], "queued")
        staged_rows = self.store.list_directory_import_files(job["id"], "shared", self.dept.id)
        self.assertTrue(staged_rows[0]["staged_path"].endswith(".pdf"))
        self.assertTrue(staged_rows[1]["staged_path"].endswith(".xlsx"))

        listing = self.client.get(
            "/api/v1/kbs/shared/directory-imports", headers=self._headers()
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([item["id"] for item in listing.json()], [job["id"]])

        detail = self.client.get(
            f"/api/v1/kbs/shared/directory-imports/{job['id']}", headers=self._headers()
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        paths = [item["relative_path"] for item in detail.json()["files"]]
        self.assertEqual(paths, ["docs/spec.pdf", "bom/bom.xlsx"])

        cancelled = self.client.post(
            f"/api/v1/kbs/shared/directory-imports/{job['id']}/cancel",
            headers=self._headers(),
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")

        retried = self.client.post(
            f"/api/v1/kbs/shared/directory-imports/{job['id']}/retry",
            headers=self._headers(),
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["status"], "queued")

    def test_plan_then_upload_single_file_supports_file_level_resume(self):
        planned = self.client.post(
            "/api/v1/kbs/shared/directory-imports/plan",
            headers=self._headers(),
            json={
                "directory_id": 0,
                "entries": [
                    {"relative_path": "docs/spec.pdf", "source_group": "文档资料"},
                    {"relative_path": "docs/other.pdf", "source_group": "文档资料"},
                ],
            },
        )
        self.assertEqual(planned.status_code, 200, planned.text)
        job = planned.json()
        self.assertEqual(job["total_files"], 2)
        first = job["files"][0]

        uploaded = self.client.put(
            f"/api/v1/kbs/shared/directory-imports/{job['id']}/files/{first['id']}/content",
            headers=self._headers(),
            files={"file": ("spec.pdf", b"%PDF-1.4")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(uploaded.json()["status"], "queued")
        rows = self.store.list_directory_import_files(job["id"], "shared", self.dept.id)
        staged = next(row for row in rows if row["id"] == first["id"])
        self.assertTrue(staged["staged_path"])
        self.assertTrue(staged["staged_path"].endswith(".pdf"))
        self.assertTrue(os.path.isfile(staged["staged_path"]))

    def test_directory_move_and_recursive_delete(self):
        headers = self._headers()
        first = self.client.post(
            "/api/v1/kbs/shared/directories", headers=headers,
            json={"parent_id": 0, "name": "docs"},
        ).json()
        second = self.client.post(
            "/api/v1/kbs/shared/directories", headers=headers,
            json={"parent_id": 0, "name": "design"},
        ).json()
        child = self.client.post(
            "/api/v1/kbs/shared/directories", headers=headers,
            json={"parent_id": first["id"], "name": "power"},
        ).json()

        moved = self.client.patch(
            f"/api/v1/kbs/shared/directories/{child['id']}",
            headers=headers,
            json={"name": "power", "parent_id": second["id"]},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["path"], "design/power")

        missing_confirm = self.client.delete(
            f"/api/v1/kbs/shared/directories/{second['id']}?recursive=true",
            headers=headers,
        )
        self.assertEqual(missing_confirm.status_code, 400, missing_confirm.text)

        deleted = self.client.delete(
            f"/api/v1/kbs/shared/directories/{second['id']}?recursive=true&confirm=true",
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertGreaterEqual(deleted.json()["deleted_directories"], 2)

    def test_import_history_delete_requires_terminal_batch(self):
        response = self.client.post(
            "/api/v1/kbs/shared/directory-imports",
            headers=self._headers(),
            files=[("files", ("a.pdf", b"%PDF"))],
            data={"directory_id": "0"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()["id"]

        blocked = self.client.delete(
            f"/api/v1/kbs/shared/directory-imports/{job_id}",
            headers=self._headers(),
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)

        self.client.post(
            f"/api/v1/kbs/shared/directory-imports/{job_id}/cancel",
            headers=self._headers(),
        )
        deleted = self.client.delete(
            f"/api/v1/kbs/shared/directory-imports/{job_id}",
            headers=self._headers(),
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        missing = self.client.get(
            f"/api/v1/kbs/shared/directory-imports/{job_id}",
            headers=self._headers(),
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_invalid_source_group_is_rejected(self):
        response = self.client.post(
            "/api/v1/kbs/shared/directory-imports/plan",
            headers=self._headers(),
            json={
                "directory_id": 0,
                "entries": [{"relative_path": "docs/spec.pdf", "source_group": "not-a-group"}],
            },
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_manifest_path_traversal_is_rejected(self):
        response = self.client.post(
            "/api/v1/kbs/shared/directory-imports",
            headers=self._headers(),
            files=[("files", ("escape.pdf", b"x"))],
            data={
                "directory_id": "0",
                "manifest": json.dumps({"entries": [{"index": 0, "relative_path": "../escape.pdf"}]}),
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.list_directories("shared", self.dept.id), [])


    def test_plan_upload_rejects_terminal_job(self):
        planned = self.client.post(
            "/api/v1/kbs/shared/directory-imports/plan",
            headers=self._headers(),
            json={
                "directory_id": 0,
                "entries": [{"relative_path": "docs/spec.pdf", "source_group": "文档资料"}],
            },
        )
        self.assertEqual(planned.status_code, 200, planned.text)
        job = planned.json()
        first = job["files"][0]
        cancelled = self.client.post(
            f"/api/v1/kbs/shared/directory-imports/{job['id']}/cancel",
            headers=self._headers(),
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

        uploaded = self.client.put(
            f"/api/v1/kbs/shared/directory-imports/{job['id']}/files/{first['id']}/content",
            headers=self._headers(),
            files={"file": ("spec.pdf", b"%PDF-1.4")},
        )
        self.assertEqual(uploaded.status_code, 409, uploaded.text)

    def test_plan_upload_enforces_job_total_bytes(self):
        planned = self.client.post(
            "/api/v1/kbs/shared/directory-imports/plan",
            headers=self._headers(),
            json={
                "directory_id": 0,
                "entries": [
                    {"relative_path": "docs/a.pdf", "source_group": "文档资料"},
                    {"relative_path": "docs/b.pdf", "source_group": "文档资料"},
                ],
            },
        )
        self.assertEqual(planned.status_code, 200, planned.text)
        job = planned.json()
        first, second = job["files"]

        with patch("src.api.routes.directory_imports.MAX_IMPORT_BYTES", 10):
            uploaded_first = self.client.put(
                f"/api/v1/kbs/shared/directory-imports/{job['id']}/files/{first['id']}/content",
                headers=self._headers(),
                files={"file": ("a.pdf", b"123456")},
            )
            self.assertEqual(uploaded_first.status_code, 200, uploaded_first.text)
            uploaded_second = self.client.put(
                f"/api/v1/kbs/shared/directory-imports/{job['id']}/files/{second['id']}/content",
                headers=self._headers(),
                files={"file": ("b.pdf", b"123456")},
            )
            self.assertEqual(uploaded_second.status_code, 413, uploaded_second.text)
