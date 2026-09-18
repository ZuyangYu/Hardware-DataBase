"""v3 §15: control-plane readiness checks fail closed."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from types import SimpleNamespace

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.storage.control_plane import apply_schema, record_schema_version
from src.storage.control_plane.health import check_control_plane

DSN = os.environ.get("HDB_TEST_POSTGRES_DSN", "")


def _dsn_with_database(dsn: str, name: str) -> str:
    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    return make_conninfo(**params)


class ControlPlaneHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_sqlite_backend_reports_up(self):
        db_path = os.path.join(self.tmp.name, "auth.db")
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
            conn.commit()
        result = check_control_plane(
            SimpleNamespace(CONTROL_PLANE_BACKEND="sqlite", AUTH_DB_PATH=db_path)
        )
        self.assertEqual(result["status"], "up")
        self.assertEqual(result["backend"], "sqlite")

    def test_unknown_backend_and_missing_dsn_fail_closed(self):
        unsupported = check_control_plane(
            SimpleNamespace(CONTROL_PLANE_BACKEND="mysql")
        )
        self.assertEqual(unsupported["status"], "down")
        missing = check_control_plane(
            SimpleNamespace(CONTROL_PLANE_BACKEND="postgres", CONTROL_PLANE_DATABASE_URL="")
        )
        self.assertEqual(missing["status"], "down")
        self.assertIn("CONTROL_PLANE_DATABASE_URL", missing["error"])


class ReadinessMetadataTests(unittest.TestCase):
    """v3 §11.5: readiness exposes the live config revision and restart list."""

    def test_readiness_reports_config_revision_and_restart_marker(self):
        import src.settings
        from src.observability.health import check_ready

        result = check_ready()
        self.assertEqual(result["config_revision"], src.settings.CONFIG_REVISION)
        self.assertIn("CONTROL_PLANE_BACKEND", result["restart_required_settings"])
        self.assertEqual(result["restart_required_settings"], sorted(result["restart_required_settings"]))


@unittest.skipUnless(DSN, "HDB_TEST_POSTGRES_DSN is not configured")
class ControlPlaneHealthPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = f"hdb_health_{uuid.uuid4().hex[:8]}"
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{cls.database}"')
        finally:
            admin.close()
        cls.dsn = _dsn_with_database(DSN, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        admin = psycopg.connect(_dsn_with_database(DSN, "postgres"), autocommit=True)
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{cls.database}" WITH (FORCE)')
        finally:
            admin.close()

    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            CONTROL_PLANE_BACKEND="postgres", CONTROL_PLANE_DATABASE_URL=self.dsn
        )

    def test_missing_schema_is_down_and_initialized_schema_is_up(self):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        self.assertEqual(check_control_plane(self._settings())["status"], "down")

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            apply_schema(conn)
            record_schema_version(conn)
        result = check_control_plane(self._settings())
        self.assertEqual(result["status"], "up")
        self.assertEqual(result["schema_version"], "1")

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE control_plane_schema SET value = '99'"
                " WHERE key = 'schema_version'"
            )
        self.assertEqual(check_control_plane(self._settings())["status"], "down")


if __name__ == "__main__":
    unittest.main()
