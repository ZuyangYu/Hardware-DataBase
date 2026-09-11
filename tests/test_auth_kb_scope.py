import gc
import os
import sqlite3
import tempfile
import unittest

import src.settings
from src.core.auth import AuthService, ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN


class AuthKnowledgeBaseScopeTests(unittest.TestCase):
    def setUp(self):
        self._old_admin_password = src.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"

    def tearDown(self):
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = self._old_admin_password
        gc.collect()

    def _service(self, tmp):
        return AuthService(db_path=os.path.join(tmp, "auth.db"))

    def test_same_kb_name_is_scoped_by_department_and_kb_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department("dept_a")
            dept_b = auth.create_department("dept_b")
            emp_a = auth.create_user_as(system_admin, "emp_a", "password123", ROLE_EMPLOYEE, dept_a.id)
            emp_b = auth.create_user_as(system_admin, "emp_b", "password123", ROLE_EMPLOYEE, dept_b.id)

            auth.register_knowledge_base("shared", owner=emp_a)
            auth.register_knowledge_base("shared", owner=emp_b)

            summaries = auth.list_knowledge_base_summaries(["shared"])
            scoped = {(item.department_id, item.name): item for item in summaries if item.registered}
            self.assertIn((dept_a.id, "shared"), scoped)
            self.assertIn((dept_b.id, "shared"), scoped)
            self.assertNotEqual(scoped[(dept_a.id, "shared")].kb_id, scoped[(dept_b.id, "shared")].kb_id)

            # 员工对本部门 KB 隐式 admin; 跨部门互不可见
            perms_a = auth.get_kb_permissions_for_user(emp_a)
            perms_b = auth.get_kb_permissions_for_user(emp_b)
            self.assertEqual(perms_a[f"{dept_a.id}:shared"], "admin")
            self.assertEqual(perms_b[f"{dept_b.id}:shared"], "admin")
            self.assertEqual(len(perms_a), 1)
            self.assertEqual(len(perms_b), 1)

            accessible_a = auth.list_accessible_kbs(emp_a, ["shared"])
            accessible_b = auth.list_accessible_kbs(emp_b, ["shared"])
            self.assertEqual(accessible_a, ["shared"])
            self.assertEqual(accessible_b, ["shared"])
            self.assertEqual(auth.get_kb_permissions_for_user(system_admin), {})

            del auth
            gc.collect()

    def test_department_scoped_delete_does_not_remove_other_department_kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department("dept_a")
            dept_b = auth.create_department("dept_b")
            emp_a = auth.create_user_as(system_admin, "emp_a", "password123", ROLE_EMPLOYEE, dept_a.id)
            emp_b = auth.create_user_as(system_admin, "emp_b", "password123", ROLE_EMPLOYEE, dept_b.id)

            auth.register_knowledge_base("shared", owner=emp_a)
            auth.register_knowledge_base("shared", owner=emp_b)
            auth.delete_knowledge_base_record("shared", department_id=dept_a.id)

            summaries = [item for item in auth.list_knowledge_base_summaries(["shared"]) if item.registered]
            self.assertEqual([(item.department_id, item.name) for item in summaries], [(dept_b.id, "shared")])

            conn = sqlite3.connect(auth.db_path)
            try:
                kb_count = conn.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(kb_count, 1)

            del auth
            gc.collect()

    def test_assign_kb_rejects_system_department(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            system_dept = next(dept for dept in auth.list_departments() if dept.name == "system")
            dept_a = auth.create_department("dept_a")
            emp_a = auth.create_user_as(system_admin, "emp_a", "password123", ROLE_EMPLOYEE, dept_a.id)
            auth.register_knowledge_base("shared", owner=emp_a)

            with self.assertRaises(ValueError):
                auth.assign_knowledge_base_as(system_admin, "shared", system_dept.id)

            del auth
            gc.collect()

    def test_assign_kb_blocks_cross_department_move_until_assets_can_migrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department("dept_a")
            dept_b = auth.create_department("dept_b")
            emp_a = auth.create_user_as(system_admin, "emp_a", "password123", ROLE_EMPLOYEE, dept_a.id)
            auth.register_knowledge_base("shared", owner=emp_a)
            source_id = auth.get_knowledge_base_id("shared", department_id=dept_a.id)

            with self.assertRaises(ValueError):
                auth.assign_knowledge_base_as(system_admin, "shared", dept_b.id, source_kb_id=source_id)

            summaries = [item for item in auth.list_knowledge_base_summaries(["shared"]) if item.registered]
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].kb_id, source_id)
            self.assertEqual(summaries[0].department_id, dept_a.id)

            del auth
            gc.collect()

    def test_assign_kb_rejects_target_duplicate_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department("dept_a")
            dept_b = auth.create_department("dept_b")
            emp_a = auth.create_user_as(system_admin, "emp_a", "password123", ROLE_EMPLOYEE, dept_a.id)
            emp_b = auth.create_user_as(system_admin, "emp_b", "password123", ROLE_EMPLOYEE, dept_b.id)
            auth.register_knowledge_base("shared", owner=emp_a)
            auth.register_knowledge_base("shared", owner=emp_b)
            source_id = auth.get_knowledge_base_id("shared", department_id=dept_a.id)

            with self.assertRaises(ValueError):
                auth.assign_knowledge_base_as(system_admin, "shared", dept_b.id, source_kb_id=source_id)

            del auth
            gc.collect()

    def test_assign_kb_owner_must_be_department_employee(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department("dept_a")
            dept_b = auth.create_department("dept_b")
            emp_b = auth.create_user_as(system_admin, "emp_b", "password123", ROLE_EMPLOYEE, dept_b.id)

            with self.assertRaises(ValueError):
                auth.assign_knowledge_base_as(system_admin, "shared", dept_a.id, owner_user_id=emp_b.id)
            with self.assertRaises(ValueError):
                auth.assign_knowledge_base_as(system_admin, "shared", dept_a.id, owner_user_id=system_admin.id)

            del auth
            gc.collect()

    def test_legacy_role_and_permission_rows_migrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "auth.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE departments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'user',
                        department_id INTEGER,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        managed_by_env INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE knowledge_bases (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        department_id INTEGER,
                        owner_user_id INTEGER,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE kb_permissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kb_name TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        permission TEXT NOT NULL DEFAULT 'read',
                        created_at TEXT NOT NULL,
                        UNIQUE(kb_name, user_id)
                    );
                    """
                )
                now = "2026-01-01T00:00:00+00:00"
                conn.executemany("INSERT INTO departments (id, name, created_at) VALUES (?, ?, ?)", [(1, "system", now), (2, "dept_a", now), (3, "dept_b", now)])
                conn.executemany(
                    "INSERT INTO users (id, username, password_hash, role, department_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (1, src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "placeholder", ROLE_SYSTEM_ADMIN, 1, now, now),
                        (2, "legacy_admin", "placeholder", "dept_admin", 2, now, now),
                        (3, "legacy_user", "placeholder", "user", 3, now, now),
                    ],
                )
                conn.executemany(
                    "INSERT INTO knowledge_bases (id, name, department_id, created_at) VALUES (?, ?, ?, ?)",
                    [(1, "shared", 2, now), (2, "shared", 3, now)],
                )
                conn.executemany(
                    "INSERT INTO kb_permissions (kb_name, user_id, permission, created_at) VALUES (?, ?, ?, ?)",
                    [("shared", 2, "read", now), ("shared", 3, "write", now)],
                )
                conn.commit()
            finally:
                conn.close()

            auth = self._service(tmp)
            # 旧角色统一并入 employee
            roles = {user.username: user.role for user in auth.list_users()}
            self.assertEqual(roles["legacy_admin"], ROLE_EMPLOYEE)
            self.assertEqual(roles["legacy_user"], ROLE_EMPLOYEE)

            # 旧授权行仍按用户部门映射到 kb_id (schema 迁移未破坏)
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT kb.department_id, p.user_id, p.permission
                    FROM kb_permissions p
                    JOIN knowledge_bases kb ON kb.id = p.kb_id
                    ORDER BY p.user_id
                    """
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [(2, 2, "read"), (3, 3, "write")])

            del auth
            gc.collect()

    def test_management_requires_system_admin_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department_as(system_admin, "dept_a")
            empty_dept = auth.create_department_as(system_admin, "empty_dept")
            emp_a = auth.create_user_as(system_admin, "emp_a", "password123", ROLE_EMPLOYEE, dept_a.id)
            emp_b = auth.create_user_as(system_admin, "emp_b", "password123", ROLE_EMPLOYEE, dept_a.id)

            # 员工不能创建账号 / 管理账号 / 列用户
            with self.assertRaises(PermissionError):
                auth.create_user_as(emp_a, "emp_c", "password123", ROLE_EMPLOYEE, dept_a.id)
            with self.assertRaises(PermissionError):
                auth.list_users_as(emp_a)
            with self.assertRaises(PermissionError):
                auth.set_user_active_as(emp_a, emp_b.id, False)
            with self.assertRaises(PermissionError):
                auth.reset_user_password_as(emp_a, emp_b.id, "password456")

            # 系统管理员可以管理账号
            self.assertEqual({u.username for u in auth.list_users_as(system_admin)} >= {"emp_a", "emp_b"}, True)
            auth.set_user_active_as(system_admin, emp_b.id, False)
            self.assertFalse(auth.get_user_by_username("emp_b").is_active)

            # 部门管理仍是系统管理员专属
            with self.assertRaises(PermissionError):
                auth.create_department_as(emp_a, "blocked_dept")
            with self.assertRaises(PermissionError):
                auth.delete_department_as(emp_a, empty_dept.id)
            auth.delete_department_as(system_admin, empty_dept.id)
            self.assertNotIn("empty_dept", {dept.name for dept in auth.list_departments()})

            # 员工必须归属业务部门
            with self.assertRaises(ValueError):
                auth.create_user_as(system_admin, "emp_no_dept", "password123", ROLE_EMPLOYEE, None)
            system_dept = next(dept for dept in auth.list_departments() if dept.name == "system")
            with self.assertRaises(ValueError):
                auth.create_user_as(system_admin, "emp_sys", "password123", ROLE_EMPLOYEE, system_dept.id)

            # 系统管理员账号由部署环境管理, 不能通过 API 自增殖
            with self.assertRaises(ValueError):
                auth.create_user_as(system_admin, "admin_2", "password123", ROLE_SYSTEM_ADMIN, None)
            self.assertNotIn("admin_2", {u.username for u in auth.list_users()})

            del auth
            gc.collect()


if __name__ == "__main__":
    unittest.main()
