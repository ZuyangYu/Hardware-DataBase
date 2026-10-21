"""PG parity + fail-closed guards for the auth domain (convergence phase 3).

``AuthService`` / ``AppLogService`` gain a PostgreSQL backend via
``src/storage/domain_pg.py`` + ``src/storage/auth_domain.py``.  The identity
and audit tables use the *mechanical* type mapping (TEXT timestamps, BIGINT
flags) because both services speak the SQLite dialect.

These tests also pin the two guards that protect the login path:

- a table left in the old semantic shape (TIMESTAMPTZ/BOOLEAN) must make
  construction fail with the rebuild command, never a half-broken login;
- the ``auth_mirror`` bridge must become a no-op once the auth domain itself
  runs on PostgreSQL.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings as app_settings
from src.core.app_logs import AppLogService
from src.core.auth import ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN, AuthService
from src.storage import domain_pg
from src.storage.auth_domain import TABLES, domain_classes

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")
ADMIN_PASSWORD = "test-admin-password-123"


def _dsn_with_database(dsn: str, name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = name
    return make_conninfo(**values)


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class AuthDomainPostgresTests(unittest.TestCase):
    """每个测试用独立的一次性数据库：这些用例会故意造"旧形状"的表。"""

    def setUp(self) -> None:
        self.database = f"hdb_auth_domain_{uuid.uuid4().hex[:8]}"
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{self.database}"')
        finally:
            admin.close()

        def _drop() -> None:
            admin = psycopg.connect(
                _dsn_with_database(DSN, "postgres"), autocommit=True
            )
            try:
                admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
            finally:
                admin.close()

        self.addCleanup(_drop)
        self.dsn = _dsn_with_database(DSN, self.database)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (
            ("CONTROL_PLANE_BACKEND", app_settings.CONTROL_PLANE_BACKEND),
            ("CONTROL_PLANE_DATABASE_URL", app_settings.CONTROL_PLANE_DATABASE_URL),
            ("AUTH_STORE_BACKEND", app_settings.AUTH_STORE_BACKEND),
            ("AUTH_STORE_DATABASE_URL", app_settings.AUTH_STORE_DATABASE_URL),
            ("STORAGE_DIR", app_settings.STORAGE_DIR),
            ("AUTH_DB_PATH", app_settings.AUTH_DB_PATH),
            ("AUTH_DEFAULT_ADMIN_PASSWORD", app_settings.AUTH_DEFAULT_ADMIN_PASSWORD),
            ("AUTH_DEFAULT_ADMIN_USERNAME", app_settings.AUTH_DEFAULT_ADMIN_USERNAME),
        ):
            self.addCleanup(setattr, app_settings, name, value)
        app_settings.STORAGE_DIR = self.tmp.name
        app_settings.AUTH_DB_PATH = os.path.join(self.tmp.name, "auth.db")
        app_settings.AUTH_DEFAULT_ADMIN_USERNAME = "admin"
        app_settings.AUTH_DEFAULT_ADMIN_PASSWORD = ADMIN_PASSWORD
        app_settings.AUTH_STORE_BACKEND = "postgres"
        app_settings.AUTH_STORE_DATABASE_URL = self.dsn

    def _pg_counts(self, table: str) -> int:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])

    # ------------------------------------------------------------------

    def test_created_administrator_can_manage_accounts_on_postgres(self) -> None:
        auth = AuthService()
        administrator = auth.get_user_by_username("admin")
        created = auth.create_user_as(administrator, "support-admin", ADMIN_PASSWORD, ROLE_SYSTEM_ADMIN)
        self.assertEqual(created.role, ROLE_SYSTEM_ADMIN)
        self.assertEqual(created.department_name, "system")
        self.assertEqual(auth.get_kb_permissions_for_user(created), {})
        session, reason = auth.authenticate("support-admin", ADMIN_PASSWORD)
        self.assertIsNotNone(session, reason)
        department = auth.create_department_as(created, "support-team")
        employee = auth.create_user_as(created, "support-employee", ADMIN_PASSWORD, ROLE_EMPLOYEE, department.id)
        self.assertEqual(employee.department_id, department.id)
        with self.assertRaisesRegex(ValueError, "用户名已存在"):
            auth.create_user_as(administrator, "support-admin", ADMIN_PASSWORD, ROLE_SYSTEM_ADMIN)
        auth.set_user_active_as(administrator, created.id, False)
        self.assertIsNone(auth.get_user_by_token(session.token))

    def test_auth_service_round_trip_on_postgres(self) -> None:
        auth = AuthService()
        self.assertEqual(auth._backend, "postgres")
        self.assertIsNone(auth.db_path)

        # 默认管理员由 bootstrap 建好，能读回来
        admin = auth.get_user_by_username("admin")
        self.assertIsNotNone(admin)

        department = auth.create_department_as(admin, "hardware")
        employee = auth.create_user_as(
            admin, "engineer", "password-123", ROLE_EMPLOYEE, department.id
        )
        self.assertEqual(employee.department_id, department.id)

        # 登录（token 写入 auth_sessions）
        session, reason = auth.authenticate("engineer", "password-123")
        self.assertIsNotNone(session, reason)
        self.assertEqual(auth.get_user_by_token(session.token).username, "engineer")

        # 停用后登录失败
        auth.set_user_active_as(admin, employee.id, False)
        blocked, _reason = auth.authenticate("engineer", "password-123")
        self.assertIsNone(blocked)

        # 知识库登记
        auth.register_knowledge_base("design", owner=employee)
        self.assertIsNotNone(
            auth.get_knowledge_base_id("design", department_id=department.id)
        )

    def test_auxiliary_sqlite_file_without_users_does_not_block_pg_auth(self) -> None:
        with sqlite3.connect(app_settings.AUTH_DB_PATH) as conn:
            conn.execute("CREATE TABLE auxiliary_log (message TEXT)")
            conn.execute("INSERT INTO auxiliary_log VALUES ('worker started')")
        auth = AuthService()
        self.assertIsNotNone(auth.get_user_by_username("admin"))

    def test_audit_events_round_trip_on_postgres(self) -> None:
        logs = AppLogService()
        self.assertEqual(logs._backend, "postgres")
        actor = AuthService().get_user_by_username("admin")
        logs.record_audit(
            "login_success", actor=actor, target_type="user", target_id="1"
        )
        self.assertGreaterEqual(logs.count_audit_events(actor), 1)
        events = logs.list_audit_events(actor, limit=5)
        self.assertTrue(any(event.action == "login_success" for event in events))

    def test_concurrent_audit_append_keeps_hash_chain_linear(self) -> None:
        """Concurrent appends must not fork the audit hash chain.

        ``record_audit`` reads the previous ``integrity_hash`` before inserting
        the next row.  Without a transaction-scoped lock both writers read the
        same predecessor and the chain forks silently.
        """
        logs = AppLogService()
        actor = AuthService().get_user_by_username("admin")
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def write(index: int) -> None:
            barrier.wait(timeout=10)
            try:
                logs.record_audit(
                    f"concurrent_audit_{index}",
                    actor=actor,
                    target_type="user",
                    target_id=str(index),
                )
            except BaseException as exc:  # noqa: BLE001 - asserted below
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=write, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        with closing(logs._connect()) as conn:
            rows = conn.execute(
                "SELECT id, previous_hash, integrity_hash FROM audit_events ORDER BY id"
            ).fetchall()
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["previous_hash"], "")
        for previous, current in zip(rows, rows[1:]):
            self.assertEqual(
                current["previous_hash"],
                previous["integrity_hash"],
                f"audit hash chain forked at id={current['id']}",
            )

    def test_mirror_is_noop_when_auth_domain_runs_on_postgres(self) -> None:
        from src.core import auth as auth_module

        auth = AuthService()
        admin = auth.get_user_by_username("admin")
        called: list[tuple] = []

        def _boom(*args, **kwargs):
            called.append(args)
            raise AssertionError("mirror must not run when auth lives in PostgreSQL")

        original = auth_module.__dict__.get("_mirror_probe")
        del original
        import src.storage.auth_mirror as mirror_module

        original_fn = mirror_module.mirror_auth_row
        mirror_module.mirror_auth_row = _boom
        try:
            auth.create_department_as(admin, "no-mirror")
        finally:
            mirror_module.mirror_auth_row = original_fn
        self.assertEqual(called, [])

    def test_shape_guard_rejects_legacy_semantic_tables(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS users CASCADE")
            conn.execute(
                """
                CREATE TABLE users (
                    id BIGINT PRIMARY KEY,
                    username TEXT,
                    password_hash TEXT,
                    role TEXT,
                    department_id BIGINT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    managed_by_env BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ
                )
                """
            )
        with self.assertRaises(RuntimeError) as caught:
            AuthService()
        message = str(caught.exception)
        self.assertIn("rebuild_auth_domain.py", message)
        self.assertIn("is_active", message)

    def test_rebuild_domain_group_restores_mechanical_shape(self) -> None:
        source = os.path.join(self.tmp.name, "legacy-auth.db")
        with closing(sqlite3.connect(source)) as conn:
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
                    role TEXT NOT NULL DEFAULT 'employee',
                    department_id INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    managed_by_env INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE auth_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE login_attempts (
                    key TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    client_ip TEXT NOT NULL DEFAULT '',
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    last_failure_at TEXT
                );
                CREATE TABLE knowledge_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    department_id INTEGER,
                    owner_user_id INTEGER,
                    is_shared INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL DEFAULT 1,
                    error_message TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE query_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE retrieved_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO departments (name, created_at) VALUES ('system', '2026-01-01T00:00:00+00:00');
                INSERT INTO users (username, password_hash, role, department_id, is_active, created_at, updated_at)
                    VALUES ('legacy', 'hash', 'employee', 1, 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO audit_events (action, created_at) VALUES ('legacy_event', '2026-01-01T00:00:00+00:00');
                """
            )
            conn.commit()

        # 先造"旧的语义形状"，模拟早期迁移留下的表
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS users CASCADE")
            conn.execute(
                "CREATE TABLE users (id BIGINT PRIMARY KEY, is_active BOOLEAN, created_at TIMESTAMPTZ)"
            )

        copied = domain_pg.rebuild_domain_group(
            self.dsn,
            domain_classes(),
            source_sqlite_path=source,
            tables=TABLES,
        )
        self.assertEqual(copied["users"], 1)
        self.assertEqual(copied["audit_events"], 1)

        # 形状已回到机械映射
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            types = dict(
                conn.execute(
                    """
                    SELECT column_name, data_type FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='users'
                    """
                ).fetchall()
            )
        self.assertEqual(types["is_active"], "bigint")
        self.assertEqual(types["created_at"], "text")

        # 数据可读，且 AuthService 能直接跑在这套表上
        auth = AuthService()
        self.assertEqual(auth.get_user_by_username("legacy").username, "legacy")
        self.assertEqual(self._pg_counts("audit_events"), 1)

    def test_governance_services_round_trip_on_postgres(self) -> None:
        """wiki / 通知等服务跟着认证域一起切到 PG（同一组表）。"""
        from src.core.notifications import NotificationService
        from src.core.wiki import WikiService

        wiki = WikiService()
        self.assertEqual(wiki._backend, "postgres")
        wiki.save_job_state(kb_id=1, department_id=1, state={"stage": "done", "stats": {}})
        stats = wiki.get_stats(kb_id=1, department_id=1)
        self.assertIsNotNone(stats)

        notifications = NotificationService()
        self.assertEqual(notifications._backend, "postgres")
        created = notifications.create(
            user_id=1, kind="system", title="测试通知", body="hello"
        )
        self.assertIsNotNone(created)
        self.assertEqual(notifications.unread_count(user_id=1), 1)

    def test_compatibility_store_round_trip_on_postgres(self) -> None:
        from src.document_authoring.compatibility import (
            DocumentAuthoringCompatibilityStore,
        )

        app_settings.COMPATIBILITY_STORE_BACKEND = "postgres"
        app_settings.COMPATIBILITY_STORE_DATABASE_URL = self.dsn
        self.addCleanup(
            setattr,
            app_settings,
            "COMPATIBILITY_STORE_BACKEND",
            app_settings.COMPATIBILITY_STORE_BACKEND,
        )
        app_settings.DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH = os.path.join(
            self.tmp.name, "compat.sqlite"
        )
        store = DocumentAuthoringCompatibilityStore()
        self.assertEqual(store._backend, "postgres")
        self.assertIsNone(store.db_path)


if __name__ == "__main__":
    unittest.main()
