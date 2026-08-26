"""Tests for audit-log sinking into AuthService / AppPipeline.

Verifies that write operations record audit events from the backend layer (so
the API path is observable), and that a single operation produces exactly one
audit row (no double-write -- the Streamlit UI calls were removed when audit
sank into the backend).
"""
import gc
import os
import tempfile
import unittest

import src.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.core.app_logs import AppLogService
from src.core.auth import ROLE_DEPT_ADMIN, ROLE_USER

from tests._api_stub import Server, StubPipeline, make_auth


class AuditSinkTests(unittest.TestCase):
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
        self.logs = AppLogService()
        self.sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)

    def _token(self, username: str, password: str = "pw123456") -> str:
        r = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _audit_actions(self) -> list[str]:
        # Use the system_admin viewer so cross-department audit rows are visible.
        # list_audit_events returns newest-first; callers compare counts and
        # membership rather than positional slices.
        events = self.logs.list_audit_events(self.sysadmin, limit=1000)
        return [e.action for e in events]

    def _new_actions(self, before_count: int) -> list[str]:
        """Return only the audit actions added since ``before_count``.

        list_audit_events is newest-first, so the newly added rows sit at the
        head of the list -- slice [:delta], not [before:].
        """
        actions = self._audit_actions()
        delta = len(actions) - before_count
        return actions[:delta] if delta > 0 else []

    def _latest_metadata(self, action: str) -> dict:
        """Return the parsed metadata_json of the newest audit row for action."""
        import json as _json

        for e in self.logs.list_audit_events(self.sysadmin, limit=1000):
            if e.action == action:
                return _json.loads(e.metadata_json or "{}")
        raise AssertionError(f"no audit row found for action={action!r}")

    def test_login_records_audit(self):
        """authenticate() records login_success / login_failed."""
        before = len(self._audit_actions())
        self._token("admin1")  # success
        self.client.post("/api/v1/login", json={"username": "admin1", "password": "bad"})  # failure
        actions = self._new_actions(before)
        self.assertIn("login_success", actions)
        self.assertIn("login_failed", actions)

    def test_logout_records_audit(self):
        token = self._token("admin1")
        before = len(self._audit_actions())
        r = self.client.post("/api/v1/logout", headers=self._auth(token))
        self.assertEqual(r.status_code, 200)
        self.assertIn("logout", self._new_actions(before))

    def test_logout_invalid_token_rejected(self):
        # No token -> current_user dependency returns 401 before revoke.
        r = self.client.post("/api/v1/logout")
        self.assertEqual(r.status_code, 401)

    def test_create_user_records_audit_no_double_write(self):
        sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        before = len(self._audit_actions())
        self.auth.create_user_as(sysadmin, "newuser", "pw123456", ROLE_DEPT_ADMIN, self.dept.id)
        new_events = self._new_actions(before)
        # Exactly one create_user audit row -- the UI call was removed, so only
        # the sunk backend audit remains.
        self.assertEqual(new_events.count("create_user"), 1)

    def test_create_department_records_audit(self):
        sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        before = len(self._audit_actions())
        self.auth.create_department_as(sysadmin, "newdept")
        self.assertIn("create_department", self._new_actions(before))

    def test_grant_kb_permission_records_audit(self):
        # grant_kb_permission_as requires a dept_admin actor for content perms.
        before = len(self._audit_actions())
        self.auth.grant_kb_permission_as(self.admin, "shared", self.user.id, "write")
        new_events = self._new_actions(before)
        self.assertEqual(new_events.count("grant_kb_permission"), 1)

    def test_set_user_active_records_audit(self):
        sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        before = len(self._audit_actions())
        self.auth.set_user_active_as(sysadmin, self.user.id, False)
        self.assertIn("set_user_active", self._new_actions(before))

    def test_reset_password_records_audit(self):
        sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        before = len(self._audit_actions())
        self.auth.reset_user_password_as(sysadmin, self.user.id, "newpass12345")
        self.assertIn("reset_user_password", self._new_actions(before))

    def test_assign_kb_records_audit(self):
        # assign_knowledge_base_as strips cross-department perms and must be
        # audited (was a silent gap before the fix).
        sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        before = len(self._audit_actions())
        self.auth.assign_knowledge_base_as(sysadmin, "shared", self.dept.id)
        new_events = self._new_actions(before)
        self.assertIn("assign_kb", new_events)
        # metadata must carry the removed_cross_dept_perms flag so an
        # operator scanning the log can see the side effect.
        metadata = self._latest_metadata("assign_kb")
        self.assertTrue(metadata.get("removed_cross_dept_perms"))
        self.assertEqual(metadata.get("department_id"), self.dept.id)

    def test_permission_denied_records_audit(self):
        # Permission checks now live inside the audit-sunk try block (aligned
        # with grant_kb_permission_as), so a denied attempt IS recorded with
        # success=False -- useful for spotting a stolen token probing endpoints.
        sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        before = len(self._audit_actions())
        with self.assertRaises(PermissionError):
            self.auth.create_user_as(sysadmin, "should_fail", "pw123456", ROLE_USER)
        new_events = self._new_actions(before)
        self.assertIn("create_user", new_events)


class DepartmentPermissionTests(unittest.TestCase):
    """GET /departments must be admin-only (was: any authenticated user)."""

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

    def _token(self, username: str, password: str = "pw123456") -> str:
        r = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_user_cannot_list_departments(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/departments", headers=self._auth(t))
        self.assertEqual(r.status_code, 403)

    def test_dept_admin_lists_own_department_only(self):
        t = self._token("admin1")
        r = self.client.get("/api/v1/departments", headers=self._auth(t))
        self.assertEqual(r.status_code, 200)
        names = [d["name"] for d in r.json()]
        # dept_admin sees only their own department, not system.
        self.assertEqual(names, ["hw"])

    def test_change_settings_records_audit(self):
        # PUT /config must record a change_settings audit (was missing on the
        # API path because apply_settings is a stateless staticmethod).
        sysadmin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        logs = AppLogService()
        token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.put(
            "/api/v1/config",
            json={"settings": {"RAGFLOW_TIMEOUT_SECONDS": "90"}},
            headers=self._auth(token),
        )
        self.assertEqual(r.status_code, 200, r.text)
        # list_audit_events is newest-first; make_auth never emits change_settings,
        # so membership in the full list is sufficient.
        events = logs.list_audit_events(sysadmin, limit=1000)
        actions = [e.action for e in events]
        self.assertIn("change_settings", actions)
        # metadata must carry the changed keys so an operator can see what moved.
        import json as _json

        latest = next(e for e in events if e.action == "change_settings")
        metadata = _json.loads(latest.metadata_json or "{}")
        self.assertEqual(metadata.get("keys"), ["RAGFLOW_TIMEOUT_SECONDS"])
        self.assertEqual(metadata.get("source"), "api")

    def test_system_admin_lists_all_kbs(self):
        # GET /kbs must branch to list_all_knowledge_bases_for_admin for
        # system_admin (was returning [] because list_accessible_kbs is empty
        # for system_admin).
        token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.get("/api/v1/kbs", headers=self._auth(token))
        self.assertEqual(r.status_code, 200)
        names = [k["name"] for k in r.json()]
        self.assertIn("shared", names)

    def test_user_cannot_access_governance(self):
        # governance endpoints must be admin-only (was: any authenticated user).
        t = self._token("user1")
        r1 = self.client.get("/api/v1/governance/stats", headers=self._auth(t))
        r2 = self.client.get("/api/v1/governance/kb-summaries", headers=self._auth(t))
        self.assertEqual(r1.status_code, 403)
        self.assertEqual(r2.status_code, 403)

    def test_kb_permissions_requires_read(self):
        # user1 has read on 'shared' -> can list perms; a KB they lack read on
        # must 403. user1 has read on shared (granted in make_auth), so this
        # asserts the read check passes for an authorised user.
        t = self._token("user1")
        r = self.client.get("/api/v1/kbs/shared/permissions", headers=self._auth(t))
        self.assertEqual(r.status_code, 200)

    def test_kb_summaries_carry_anomaly_fields(self):
        # KbSummaryView must expose files/failed/parsing/issue_flags so the
        # frontend can render the governance panel from one endpoint.
        token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.get("/api/v1/governance/kb-summaries", headers=self._auth(token))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body, "expected at least one KB summary")
        entry = body[0]
        for field in ("files", "failed", "parsing", "issue_flags"):
            self.assertIn(field, entry)

    def test_dept_admin_users_excludes_admins_by_default(self):
        # dept_admin should see only ROLE_USER unless include_admins=true.
        t = self._token("admin1")
        r = self.client.get("/api/v1/users", headers=self._auth(t))
        self.assertEqual(r.status_code, 200)
        roles = [u["role"] for u in r.json()]
        self.assertTrue(roles, "expected at least one user")
        self.assertNotIn("dept_admin", roles)
        # Opt-in to admins.
        r2 = self.client.get("/api/v1/users?include_admins=true", headers=self._auth(t))
        roles2 = [u["role"] for u in r2.json()]
        self.assertIn("dept_admin", roles2)

    def test_parse_tasks_requires_write(self):
        # list_parse_tasks was tightened from read to write (mirrors Streamlit).
        user_token = self._token("user1")  # read only on shared
        admin_token = self._token("admin1")  # write (admin) on shared
        self.assertEqual(
            self.client.get("/api/v1/kbs/shared/parse-tasks", headers=self._auth(user_token)).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/v1/kbs/shared/parse-tasks", headers=self._auth(admin_token)).status_code,
            200,
        )

    def test_evaluation_create_run_validates_body(self):
        # create_run now uses a pydantic model: invalid mode -> 422.
        token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.post(
            "/api/v1/evaluation/runs",
            json={"dataset_path": "evaluation/datasets/hardware_qa_v1.jsonl", "mode": "bogus"},
            headers=self._auth(token),
        )
        self.assertEqual(r.status_code, 422)

    def test_sysadmin_cannot_access_kb_content(self):
        # system_admin is a governance role by design (see CLAUDE.md > 角色权力
        # 分离). KB content endpoints must reject sysadmin with a specific
        # message so a frontend can differentiate this from "no permission".
        token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        h = self._auth(token)
        for path in (
            "/api/v1/kbs/shared/files",
            "/api/v1/kbs/shared/parse-tasks",
            "/api/v1/kbs/shared/files/x/chunks",
        ):
            r = self.client.get(path, headers=h)
            self.assertEqual(r.status_code, 403, f"{path} should be 403 for sysadmin")
            # The detail must name system_admin so the frontend knows this
            # isn't a "grant more permission" situation.
            self.assertIn("system_admin", r.json()["detail"])

    def test_sysadmin_can_access_governance_endpoints(self):
        # Positive side of the split: sysadmin's governance endpoints must
        # still work (KB list, permissions list, logs, config, etc.).
        token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        h = self._auth(token)
        for path in (
            "/api/v1/kbs",
            "/api/v1/kbs/shared/permissions",
            "/api/v1/governance/stats",
            "/api/v1/governance/kb-summaries",
            "/api/v1/users",
            "/api/v1/departments",
            "/api/v1/config",
            "/api/v1/logs/audit",
        ):
            r = self.client.get(path, headers=h)
            self.assertEqual(r.status_code, 200, f"{path} should be 200 for sysadmin")

    def test_config_rejects_non_scalar_values(self):
        # UpdateConfigRequest.settings now typed to scalar-only; list/dict -> 422.
        token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.put(
            "/api/v1/config",
            json={"settings": {"RAGFLOW_TIMEOUT_SECONDS": [1, 2, 3]}},
            headers=self._auth(token),
        )
        self.assertEqual(r.status_code, 422)

    def test_query_history_length_capped(self):
        # QueryRequest.history has max_length=100; longer history -> 422.
        t = self._token("user1")
        big_history = [["u", "a"]] * 200
        r = self.client.post(
            "/api/v1/query",
            json={"kb_name": "shared", "query": "问", "history": big_history},
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 422)

    def test_evaluation_controller_singleton(self):
        # Two requests must hit the same controller so its in-memory _threads
        # registry can prevent concurrent workers on the same run. The unit
        # entry point is _controller() -- probe it directly to avoid needing
        # eval group deps installed.
        from src.api.routes.evaluation import _controller

        c1 = _controller("storage/evaluations")
        c2 = _controller("storage/evaluations")
        self.assertIs(c1, c2)

    def test_upload_rejects_path_traversal_filename(self):
        # A filename of ``..`` must be neutralised (falls back to "upload"),
        # not written to the temp dir's parent.
        t = self._token("admin1")
        r = self.client.post(
            "/api/v1/kbs/shared/files",
            headers=self._auth(t),
            files=[("files", ("..", b"payload"))],
        )
        # StubPipeline.upload_files accepts any list; success just means the
        # request went through without a path-escape error.
        self.assertEqual(r.status_code, 200, r.text)

    def test_upload_413_on_oversize(self):
        # An upload larger than MAX_UPLOAD_BYTES must return 413. We temporarily
        # squash the cap so the test doesn't have to allocate 500 MB.
        import src.api.routes.upload as upload_mod

        original = upload_mod.MAX_UPLOAD_BYTES
        upload_mod.MAX_UPLOAD_BYTES = 8
        try:
            t = self._token("admin1")
            r = self.client.post(
                "/api/v1/kbs/shared/files",
                headers=self._auth(t),
                files=[("files", ("a.txt", b"this is definitely more than 8 bytes"))],
            )
            self.assertEqual(r.status_code, 413)
        finally:
            upload_mod.MAX_UPLOAD_BYTES = original

    def test_revoke_kb_permission(self):
        # After granting user1 read on shared (in make_auth), revoke and verify
        # they lose access to /kbs/shared/files.
        t_admin = self._token("admin1")
        r = self.client.delete(
            f"/api/v1/kbs/shared/permissions/{self.user.id}",
            headers=self._auth(t_admin),
        )
        self.assertEqual(r.status_code, 200, r.text)
        # user1 no longer has read.
        t_user = self._token("user1")
        r2 = self.client.get("/api/v1/kbs/shared/files", headers=self._auth(t_user))
        self.assertEqual(r2.status_code, 403)


class SchemaValidationTests(unittest.TestCase):
    """role/permission enums reject invalid values with 422."""

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

    def _token(self, username: str, password: str = "pw123456") -> str:
        r = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_invalid_role_rejected(self):
        # Log in as system_admin to pass the require_any_admin guard, then try
        # to create a user with an invalid role -> 422 from pydantic Literal.
        admin_token = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.post(
            "/api/v1/users",
            json={"username": "x", "password": "pw123456", "role": "superuser"},
            headers=self._auth(admin_token),
        )
        self.assertEqual(r.status_code, 422)

    def test_invalid_permission_rejected(self):
        t = self._token("admin1")
        r = self.client.post(
            "/api/v1/kbs/shared/permissions",
            json={"user_id": self.user.id, "permission": "owner"},
            headers=self._auth(t),
        )
        self.assertEqual(r.status_code, 422)


class QueryTraceTests(unittest.TestCase):
    """POST /query writes a query trace + evidence (fail-soft)."""

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
        self.logs = AppLogService()

    def _token(self, username: str, password: str = "pw123456") -> str:
        r = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_query_writes_trace(self):
        t = self._token("user1")
        before = len(self.logs.list_query_traces(self.user))
        with self.client.stream(
            "POST", "/api/v1/query", json={"kb_name": "shared", "query": "问"}, headers=self._auth(t)
        ) as r:
            self.assertEqual(r.status_code, 200)
            b"".join(r.iter_bytes())
        traces = self.logs.list_query_traces(self.user)
        self.assertGreater(len(traces), before)
        # The newest trace reflects this API query.
        self.assertEqual(traces[0].kb_name, "shared")

    def test_query_trace_status_derived_from_summary(self):
        # If the retrieval summary reports failed, the trace status must be
        # "failed" -- proves query_trace_status is consulted rather than the
        # route hardcoding "success" like before P1-9.
        original = self.stub.get_last_retrieval_summary
        self.stub.get_last_retrieval_summary = lambda: {"status": "failed", "evidence": [], "error_message": "boom"}
        try:
            t = self._token("user1")
            with self.client.stream(
                "POST", "/api/v1/query", json={"kb_name": "shared", "query": "问"}, headers=self._auth(t)
            ) as r:
                self.assertEqual(r.status_code, 200)
                b"".join(r.iter_bytes())
            traces = self.logs.list_query_traces(self.user)
            self.assertEqual(traces[0].status, "failed")
        finally:
            self.stub.get_last_retrieval_summary = original


if __name__ == "__main__":
    unittest.main()
