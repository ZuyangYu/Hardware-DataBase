"""Control plane must not lose users that were created after the cutover.

The control plane (``CONTROL_PLANE_BACKEND=postgres``) reads
``users.is_active``/``users.department_id`` and enforces the
``fk_chat_sessions_user`` foreign key against the *PostgreSQL* copy of
``users``, while the authoritative auth store stays in SQLite.  Before the
``AuthService -> auth_mirror`` bridge existed, a user created *after* the
migration could not even create a session (FK violation) and their turns
were invisible to the worker claim query (INNER JOIN on ``users``).

These tests pin the fixed contract on the project-managed PostgreSQL
instance (``HDB_TEST_POSTGRES_DSN``) using a throwaway database per run, so
they never touch real data.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings as app_settings
from src.core.auth import ROLE_EMPLOYEE, AuthService
from src.core.conversation import ConversationService
from src.storage.control_plane import apply_schema, load_sqlite_snapshot
from src.storage.control_plane.factory import (
    build_conversation_service,
    close_conversation_services,
)

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = name
    return make_conninfo(**values)


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class PostgresAuthMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_auth_mirror_{uuid.uuid4().hex[:8]}"
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{cls.database}"')
        finally:
            admin.close()
        cls.dsn = _dsn_with_database(DSN, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        close_conversation_services()
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{cls.database}" WITH (FORCE)')
        finally:
            admin.close()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth_db = os.path.join(self.tmp.name, "auth.db")
        for name, value in (
            ("CONTROL_PLANE_BACKEND", app_settings.CONTROL_PLANE_BACKEND),
            ("CONTROL_PLANE_DATABASE_URL", app_settings.CONTROL_PLANE_DATABASE_URL),
            ("AUTH_DB_PATH", app_settings.AUTH_DB_PATH),
        ):
            self.addCleanup(setattr, app_settings, name, value)
        app_settings.AUTH_DB_PATH = self.auth_db
        # The mirror is disabled while the SQLite bootstrap + snapshot are
        # built (below), then enabled for the actual test body.

        # 1) SQLite auth snapshot: default admin + one pre-migration user,
        #    plus a pending turn for that user.
        self.auth = AuthService(db_path=self.auth_db)
        admin = self.auth.get_user_by_username(
            app_settings.AUTH_DEFAULT_ADMIN_USERNAME or "admin"
        )
        self.assertIsNotNone(admin)
        self.pre_department = self.auth.create_department_as(admin, "pre-migration")
        self.pre_user = self.auth.create_user_as(
            admin, "pre-migration", "password-123", ROLE_EMPLOYEE, self.pre_department.id
        )

        ledger_sqlite = app_settings.CONTROL_PLANE_BACKEND
        app_settings.CONTROL_PLANE_BACKEND = "sqlite"
        ledger = _sqlite_ledger(self.auth_db)
        self.pre_session = ledger.create_session(
            self.pre_user.id,
            "__general__",
            department_id=self.pre_department.id,
        )
        self.pre_turn = ledger.create_turn(
            self.pre_user.id, self.pre_session.id, "pre question"
        )
        app_settings.CONTROL_PLANE_BACKEND = ledger_sqlite

        # 2) Fresh PG control plane + snapshot import (pre-migration rows land).
        with psycopg.connect(self.dsn, autocommit=True, options="-c TimeZone=UTC") as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            apply_schema(conn)
            report = load_sqlite_snapshot(self.auth_db, conn, batch_size=100)
            self.assertTrue(report.ok, report.failures)

        # 3) From here on the deployment runs the PostgreSQL control plane, so
        #    subsequent auth mutations must be mirrored.
        app_settings.CONTROL_PLANE_BACKEND = "postgres"
        app_settings.CONTROL_PLANE_DATABASE_URL = self.dsn

    def _pg_service(self):
        return build_conversation_service()

    def _admin(self):
        return self.auth.get_user_by_username(
            app_settings.AUTH_DEFAULT_ADMIN_USERNAME or "admin"
        )

    def test_post_migration_user_turn_is_claimable(self) -> None:
        admin = self._admin()
        department = self.auth.create_department_as(admin, "post-migration-turns")
        user = self.auth.create_user_as(
            admin, "post-migration", "password-123", ROLE_EMPLOYEE, department.id
        )

        store = self._pg_service()
        session = store.create_session(user.id, "__general__")
        turn = store.create_turn(user.id, session.id, "post question")

        pending = store.list_pending_turn_work(limit=8)
        picked_ids = {turn.id for turn, _user_id in pending}
        picked_users = {int(user_id) for _turn, user_id in pending}

        self.assertIn(self.pre_turn.id, picked_ids)
        self.assertIn(turn.id, picked_ids)
        self.assertIn(user.id, picked_users)

    def test_post_migration_user_session_keeps_department(self) -> None:
        admin = self._admin()
        department = self.auth.create_department_as(admin, "post-migration-dept")
        user = self.auth.create_user_as(
            admin, "dept-user", "password-123", ROLE_EMPLOYEE, department.id
        )
        self.assertEqual(user.department_id, department.id)

        store = self._pg_service()
        session = store.create_session(user.id, "__general__")

        self.assertEqual(
            session.department_id,
            department.id,
            "the department fallback must see the mirrored users row",
        )

    def test_deactivated_post_migration_user_turn_is_not_claimed(self) -> None:
        admin = self._admin()
        department = self.auth.create_department_as(admin, "post-migration-disable")
        user = self.auth.create_user_as(
            admin, "to-disable", "password-123", ROLE_EMPLOYEE, department.id
        )

        store = self._pg_service()
        session = store.create_session(user.id, "__general__")
        store.create_turn(user.id, session.id, "will be disabled")

        self.auth.set_user_active_as(admin, user.id, False)

        pending = store.list_pending_turn_work(limit=8)
        picked_users = {int(user_id) for _turn, user_id in pending}
        self.assertNotIn(user.id, picked_users)


def _sqlite_ledger(db_path: str):

    return ConversationService(db_path)


if __name__ == "__main__":
    unittest.main()