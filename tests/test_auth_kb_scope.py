import gc
import os
import sqlite3
import tempfile
import unittest

import config.settings
from src.core.auth import AuthService, ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, ROLE_USER


class AuthKnowledgeBaseScopeTests(unittest.TestCase):
    def setUp(self):
        self._old_admin_password = config.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        config.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"

    def tearDown(self):
        config.settings.AUTH_DEFAULT_ADMIN_PASSWORD = self._old_admin_password
        gc.collect()

    def _service(self, tmp):
        return AuthService(db_path=os.path.join(tmp, "auth.db"))

    def test_same_kb_name_is_scoped_by_department_and_kb_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(config.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department("dept_a")
            dept_b = auth.create_department("dept_b")
            admin_a = auth.create_user_as(system_admin, "admin_a", "password123", ROLE_DEPT_ADMIN, dept_a.id)
            admin_b = auth.create_user_as(system_admin, "admin_b", "password123", ROLE_DEPT_ADMIN, dept_b.id)
            user_a = auth.create_user_as(admin_a, "user_a", "password123", ROLE_USER, dept_a.id)
            user_b = auth.create_user_as(admin_b, "user_b", "password123", ROLE_USER, dept_b.id)

            auth.register_knowledge_base("shared", owner=admin_a)
            auth.register_knowledge_base("shared", owner=admin_b)
            auth.grant_kb_permission_as(admin_a, "shared", user_a.id, "read")
            auth.grant_kb_permission_as(admin_b, "shared", user_b.id, "write")

            summaries = auth.list_knowledge_base_summaries(["shared"])
            scoped = {(item.department_id, item.name): item for item in summaries if item.registered}
            self.assertIn((dept_a.id, "shared"), scoped)
            self.assertIn((dept_b.id, "shared"), scoped)
            self.assertNotEqual(scoped[(dept_a.id, "shared")].kb_id, scoped[(dept_b.id, "shared")].kb_id)

            perms_a = auth.list_knowledge_base_permissions("shared", department_id=dept_a.id)
            perms_b = auth.list_knowledge_base_permissions("shared", department_id=dept_b.id)
            self.assertEqual({item.username for item in perms_a}, {"admin_a", "user_a"})
            self.assertEqual({item.username for item in perms_b}, {"admin_b", "user_b"})
            user_a_permissions = auth.get_kb_permissions_for_user(user_a)
            user_b_permissions = auth.get_kb_permissions_for_user(user_b)
            self.assertEqual(user_a_permissions[f"{dept_a.id}:shared"], "read")
            self.assertEqual(user_b_permissions[f"{dept_b.id}:shared"], "write")
            self.assertNotIn("shared", user_a_permissions)
            self.assertNotIn("shared", user_b_permissions)

            del auth
            gc.collect()

    def test_department_scoped_delete_does_not_remove_other_department_kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(config.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department("dept_a")
            dept_b = auth.create_department("dept_b")
            admin_a = auth.create_user_as(system_admin, "admin_a", "password123", ROLE_DEPT_ADMIN, dept_a.id)
            admin_b = auth.create_user_as(system_admin, "admin_b", "password123", ROLE_DEPT_ADMIN, dept_b.id)

            auth.register_knowledge_base("shared", owner=admin_a)
            auth.register_knowledge_base("shared", owner=admin_b)
            auth.delete_knowledge_base_record("shared", department_id=dept_a.id)

            summaries = [item for item in auth.list_knowledge_base_summaries(["shared"]) if item.registered]
            self.assertEqual([(item.department_id, item.name) for item in summaries], [(dept_b.id, "shared")])

            conn = sqlite3.connect(auth.db_path)
            try:
                kb_count = conn.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0]
                perm_count = conn.execute("SELECT COUNT(*) FROM kb_permissions").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(kb_count, 1)
            self.assertEqual(perm_count, 1)

            del auth
            gc.collect()

    def test_legacy_permission_migration_maps_by_user_department(self):
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
                        (1, config.settings.AUTH_DEFAULT_ADMIN_USERNAME, "placeholder", ROLE_SYSTEM_ADMIN, 1, now, now),
                        (2, "user_a", "placeholder", ROLE_USER, 2, now, now),
                        (3, "user_b", "placeholder", ROLE_USER, 3, now, now),
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
            rows = []
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

    def test_admin_management_requires_actor_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._service(tmp)
            system_admin = auth.get_user_by_username(config.settings.AUTH_DEFAULT_ADMIN_USERNAME)
            dept_a = auth.create_department_as(system_admin, "dept_a")
            dept_b = auth.create_department_as(system_admin, "dept_b")
            empty_dept = auth.create_department_as(system_admin, "empty_dept")
            admin_a = auth.create_user_as(system_admin, "admin_a", "password123", ROLE_DEPT_ADMIN, dept_a.id)
            admin_b = auth.create_user_as(system_admin, "admin_b", "password123", ROLE_DEPT_ADMIN, dept_b.id)
            user_a = auth.create_user_as(admin_a, "user_a", "password123", ROLE_USER, dept_a.id)
            user_b = auth.create_user_as(admin_b, "user_b", "password123", ROLE_USER, dept_b.id)

            with self.assertRaises(PermissionError):
                auth.list_users_as(user_a)
            self.assertEqual({user.username for user in auth.list_users_as(admin_a)}, {"admin_a", "user_a"})

            auth.set_user_active_as(admin_a, user_a.id, False)
            self.assertFalse(auth.get_user_by_username("user_a").is_active)
            with self.assertRaises(PermissionError):
                auth.set_user_active_as(admin_a, user_b.id, False)
            with self.assertRaises(PermissionError):
                auth.set_user_active_as(admin_a, admin_b.id, False)

            with self.assertRaises(PermissionError):
                auth.create_department_as(admin_a, "blocked_dept")
            with self.assertRaises(PermissionError):
                auth.delete_department_as(admin_a, empty_dept.id)

            auth.delete_department_as(system_admin, empty_dept.id)
            self.assertNotIn("empty_dept", {dept.name for dept in auth.list_departments()})

            del auth
            gc.collect()


if __name__ == "__main__":
    unittest.main()
