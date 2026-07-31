import gc
import os
from contextlib import closing
import tempfile
import unittest

import config.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.core.auth import ROLE_USER

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

    def test_assets_are_readable_but_system_admin_is_blocked(self):
        user_token = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/assets", headers=self._auth(user_token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), [])

        system_token = self._token(config.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.get("/api/v1/kbs/shared/assets", headers=self._auth(system_token))
        self.assertEqual(r.status_code, 403, r.text)

    def test_asset_sources_normalize_indexed_files_to_completed(self):
        self.stub.list_file_infos = lambda kb_name, ctx=None: [
            type("IndexedFile", (), {
                "id": "indexed-1", "name": "BOM.xlsx", "status": "indexed",
                "processor_kind": "spreadsheet_table", "dataset_kind": "table",
            })(),
        ]
        token = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/asset-sources", headers=self._auth(token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()[0]["file_status"], "completed")

    def test_structured_spreadsheets_user_read_ok(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/structured/spreadsheets", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["totals"]["file_count"], 0)

    def test_structured_content_rejects_system_admin(self):
        t = self._token(config.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.get("/api/v1/kbs/shared/structured/spreadsheets", headers=self._auth(t))
        self.assertEqual(r.status_code, 403)

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

    def test_session_creation_requires_kb_permission(self):
        # user1 lacks read permission on "other_kb"; the turn flow gates at
        # session creation (the live entry point, now that /query is gone).
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "other_kb", "title": "新对话"},
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 403)

    def test_general_chat_session_does_not_require_kb_permission(self):
        t = self._token("user1")
        r = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "__general__", "title": "通用对话"},
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 200, r.text)
        session_id = r.json()["id"]
        r = self.client.post(
            f"/api/v1/conversations/{session_id}/messages",
            json={"role": "user", "content": "你好"},
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_general_chat_rejects_system_admin(self):
        t = self._token(config.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.get(
            "/api/v1/conversations",
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 403)
        r = self.client.get(
            "/api/v1/conversations?kb_name=__general__",
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "__general__", "title": "通用对话"},
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 403)

    def test_turn_persists_messages_and_replays_sse_events(self):
        t = self._token("user1")
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "新对话"},
            headers=self._auth(t),
        ).json()
        created = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "问", "client_request_id": "retry-safe-key"},
            headers=self._auth(t),
        )
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        turn_id = payload["turn"]["id"]
        self.assertEqual(payload["user_message"]["content"], "问")

        # Same idempotency key must not create another model turn/message pair.
        retried = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "问", "client_request_id": "retry-safe-key"},
            headers=self._auth(t),
        )
        self.assertEqual(retried.status_code, 201, retried.text)
        self.assertEqual(retried.json()["turn"]["id"], turn_id)

        started = self.client.post(f"/api/v1/turns/{turn_id}/start", headers=self._auth(t))
        self.assertEqual(started.status_code, 202, started.text)
        with self.client.stream("GET", f"/api/v1/turns/{turn_id}/events", headers=self._auth(t)) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes()).decode("utf-8")
        self.assertIn("id: 1", body)
        self.assertIn("event: delta", body)
        self.assertIn("第一段", body)
        self.assertIn("event: done", body)

        finished = self.client.get(f"/api/v1/turns/{turn_id}", headers=self._auth(t))
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["status"], "completed")
        self.assertEqual(finished.json()["answer"], "第一段第二段")
        messages = self.client.get(f"/api/v1/conversations/{session['id']}/messages", headers=self._auth(t)).json()
        self.assertEqual([message["content"] for message in messages], ["问", "第一段第二段"])
        self.assertEqual(messages[1]["footer"], "footer")

    def test_knowledge_base_turn_forces_deep_retrieval(self):
        t = self._token("user1")
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "新对话"},
            headers=self._auth(t),
        ).json()

        created = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "即使客户端请求快速模式", "query_mode": "fast"},
            headers=self._auth(t),
        )

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["turn"]["query_mode"], "deep")

    def test_pending_turn_can_be_cancelled_without_starting(self):
        t = self._token("user1")
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "新对话"},
            headers=self._auth(t),
        ).json()
        turn = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "不要启动"},
            headers=self._auth(t),
        ).json()["turn"]
        cancelled = self.client.post(f"/api/v1/turns/{turn['id']}/cancel", headers=self._auth(t))
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        messages = self.client.get(
            f"/api/v1/conversations/{session['id']}/messages",
            headers=self._auth(t),
        ).json()
        self.assertEqual(messages[-1]["content"], "已停止生成")

    def test_stale_turn_sse_closes_with_durable_worker_lost_error(self):
        token = self._token("user1")
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "失联任务"},
            headers=self._auth(token),
        ).json()
        turn = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "失联"},
            headers=self._auth(token),
        ).json()["turn"]
        with closing(self.auth._connect()) as conn:
            conn.execute(
                "UPDATE chat_turns SET status = 'streaming', worker_id = 'lost-worker', worker_heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (turn["id"],),
            )
        with self.client.stream("GET", f"/api/v1/turns/{turn['id']}/events", headers=self._auth(token)) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes()).decode("utf-8")
        self.assertIn("event: error", body)
        self.assertIn("任务执行器失去心跳", body)
        self.assertEqual(
            self.client.get(f"/api/v1/turns/{turn['id']}", headers=self._auth(token)).json()["status"],
            "failed",
        )

    def test_turn_cancel_does_not_disclose_another_users_turn(self):
        other = self.auth.create_user_as(self.admin, "user2", "pw123456", ROLE_USER, self.dept.id)
        self.auth.grant_kb_permission_as(self.admin, "shared", other.id, "read")
        owner_token = self._token("user1")
        other_token = self._token("user2")
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "新对话"},
            headers=self._auth(owner_token),
        ).json()
        turn = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "私有请求"},
            headers=self._auth(owner_token),
        ).json()["turn"]
        response = self.client.post(f"/api/v1/turns/{turn['id']}/cancel", headers=self._auth(other_token))
        self.assertEqual(response.status_code, 404, response.text)

    def test_session_and_turn_history_survive_department_changes(self):
        token = self._token("user1")
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "部门会话"},
            headers=self._auth(token),
        ).json()
        turn = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "部门范围"},
            headers=self._auth(token),
        ).json()["turn"]
        other_department = self.auth.create_department("other-department")
        with closing(self.auth._connect()) as conn:
            conn.execute("UPDATE users SET department_id = ? WHERE id = ?", (other_department.id, self.user.id))
        moved_token = self._token("user1")
        self.assertEqual(
            self.client.get(f"/api/v1/conversations/{session['id']}", headers=self._auth(moved_token)).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/turns/{turn['id']}", headers=self._auth(moved_token)).status_code,
            200,
        )

    def test_stale_turn_is_reclaimed_when_the_client_reconnects(self):
        token = self._token("user1")
        session = self.client.post(
            "/api/v1/conversations",
            json={"kb_name": "shared", "title": "恢复任务"},
            headers=self._auth(token),
        ).json()
        turn = self.client.post(
            f"/api/v1/conversations/{session['id']}/turns",
            json={"query": "恢复生成"},
            headers=self._auth(token),
        ).json()["turn"]
        with closing(self.auth._connect()) as conn:
            conn.execute(
                "UPDATE chat_turns SET status = 'streaming', worker_id = 'dead-worker', worker_heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (turn["id"],),
            )
        self.assertEqual(self.client.post(f"/api/v1/turns/{turn['id']}/start", headers=self._auth(token)).status_code, 202)
        with self.client.stream("GET", f"/api/v1/turns/{turn['id']}/events", headers=self._auth(token)) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("event: done", b"".join(response.iter_bytes()).decode("utf-8"))
        self.assertEqual(
            self.client.get(f"/api/v1/turns/{turn['id']}", headers=self._auth(token)).json()["status"],
            "completed",
        )
