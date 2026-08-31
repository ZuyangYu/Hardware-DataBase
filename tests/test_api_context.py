import gc
import os
import tempfile
import unittest

import src.settings
from src.core.auth import ROLE_DEPT_ADMIN
from src.api.context import build_context_for_user

from tests._api_stub import make_auth


class BuildContextForUserTests(unittest.TestCase):
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
        self.addCleanup(gc.collect)

    def test_fills_kb_id_and_department(self):
        ctx = build_context_for_user(self.admin, kb_name="shared", auth=self.auth)
        kb_id = self.auth.get_knowledge_base_id("shared", department_id=self.dept.id)
        self.assertEqual(ctx.metadata.get("kb_id"), kb_id)
        self.assertEqual(ctx.metadata.get("resource_department_id"), self.dept.id)
        self.assertIn(ROLE_DEPT_ADMIN, ctx.roles)
        self.assertIn("admin", ctx.kb_permissions.values())

    def test_no_kb_name_leaves_kb_id_none(self):
        ctx = build_context_for_user(self.admin, auth=self.auth)
        self.assertIsNone(ctx.metadata.get("kb_id"))
        self.assertEqual(ctx.metadata.get("resource_department_id"), self.dept.id)

    def test_inactive_user_collapses_to_anonymous(self):
        self.auth.set_user_active(self.admin.id, False)
        ctx = build_context_for_user(self.admin, kb_name="shared", auth=self.auth)
        self.assertEqual(ctx.roles, ["anonymous"])
