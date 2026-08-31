import gc
import os
import tempfile
import unittest

import src.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline

from tests._api_stub import Server, make_auth


class _StubConversations:
    def __init__(self):
        self.rows = [
            {
                "conversation_id": "c1",
                "title": "标题-c1",
                "source_file": "c1.md",
                "origin": "upload",
                "source_group": "外部数据",
                "turn_count": 2,
                "block_count": 0,
                "status": "indexed",
                "created_at": "2026-08-25",
            }
        ]

    def list_conversations(self, department_id, kb_name):
        return [dict(r) for r in self.rows if kb_name == "shared"]

    def get_conversation(self, department_id, kb_name, conversation_id):
        for row in self.list_conversations(department_id, kb_name):
            if row["conversation_id"] == conversation_id:
                return row
        return None

    def delete_conversation(self, department_id, kb_name, conversation_id):
        return True


class StubConvPipeline:
    def __init__(self):
        self.engine = _StubConversations()
        self.store = None
        self.deleted = []
        self.summarized = []

    def list_external_conversations(self, kb_name, ctx=None):
        return self.engine.list_conversations(ctx.metadata.get("department_id", ""), kb_name)

    def get_external_conversation(self, kb_name, conversation_id, ctx=None):
        meta = self.engine.get_conversation(ctx.metadata.get("department_id", ""), kb_name, conversation_id)
        if meta is None:
            return None
        return {
            **meta,
            "turns": [{"role": "user", "content": "LDO 压差?", "ts": "", "start_offset": 0, "end_offset": 8}],
            "blocks": [],
            "preview": "用户: LDO 压差?",
        }

    def delete_external_conversation(self, kb_name, conversation_id, ctx=None):
        self.deleted.append(conversation_id)
        return True

    def regenerate_external_conversation_summary(self, kb_name, conversation_id, ctx=None):
        meta = self.engine.get_conversation(ctx.metadata.get("department_id", ""), kb_name, conversation_id)
        if meta is None:
            return None
        self.summarized.append(conversation_id)
        detail = self.get_external_conversation(kb_name, conversation_id, ctx=ctx)
        detail["summary"] = "讨论了LDO压差结论。"
        detail["key_points"] = ["最大压差0.3V"]
        detail["summary_generated_at"] = "2026-08-25"
        return detail


class ExternalConversationApiTests(unittest.TestCase):
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
        self.stub = StubConvPipeline()
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

    def test_list_returns_department_scoped_conversations(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/external-conversations", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["totals"]["count"], 1)
        self.assertEqual(body["items"][0]["conversation_id"], "c1")

    def test_detail_returns_turns_and_preview(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/external-conversations/c1", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["turns"][0]["content"], "LDO 压差?")
        self.assertIn("LDO", body["preview"])

    def test_unknown_conversation_returns_404(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/external-conversations/nope", headers=self._auth(t))
        self.assertEqual(r.status_code, 404)

    def test_delete_requires_write_permission(self):
        t = self._token("user1")  # read-only user
        r = self.client.delete("/api/v1/kbs/shared/external-conversations/c1", headers=self._auth(t))
        self.assertEqual(r.status_code, 403)

    def test_delete_admin_ok(self):
        t = self._token("admin1")
        r = self.client.delete("/api/v1/kbs/shared/external-conversations/c1", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.stub.deleted, ["c1"])

    def test_regenerate_summary_admin_ok(self):
        t = self._token("admin1")
        r = self.client.post("/api/v1/kbs/shared/external-conversations/c1/summary", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("LDO压差", body["summary"])
        self.assertEqual(self.stub.summarized, ["c1"])

    def test_regenerate_summary_user_forbidden(self):
        t = self._token("user1")
        r = self.client.post("/api/v1/kbs/shared/external-conversations/c1/summary", headers=self._auth(t))
        self.assertEqual(r.status_code, 403)

    def test_system_admin_blocked_from_kb_content(self):
        t = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.get("/api/v1/kbs/shared/external-conversations", headers=self._auth(t))
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
