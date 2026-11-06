"""Idempotent removal of the retired task/tenant storage layer.

The workspace-first agent no longer has a task workbench or a standalone
tenant table: a user is the isolation boundary.  Older databases still carry
``tasks``/``agent_tasks*`` tables, a ``tenants`` table and ``tenant_id``
columns that no active code reads.  Bootstrap calls :func:`cleanup_configured`
so upgrades converge on the reduced schema.

Tenant-shaped columns in attachments, result exports and document/project
tables are intentionally left alone: those components still use the user id as
a compatibility tenant key.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from typing import Any


# Both historical naming schemes: SQLite used unprefixed names while the
# PostgreSQL task store used ``agent_`` prefixes.
LEGACY_TASK_TABLES: tuple[str, ...] = (
    "tasks",
    "task_turns",
    "task_artifacts",
    "agent_tasks",
    "agent_task_turns",
    "agent_task_artifacts",
)

LEGACY_TENANT_TABLES: tuple[str, ...] = ("tenants",)

# Only columns introduced by the deleted tenant layer.  The same table may
# exist in the auth domain and in the control-plane auth mirror.
LEGACY_TENANT_COLUMNS: dict[str, str] = {
    "users": "tenant_id",
    "departments": "tenant_id",
    "knowledge_bases": "tenant_id",
    "agent_workspaces": "tenant_id",
    "agent_workspace_artifacts": "tenant_id",
}


def _sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def cleanup_sqlite(path: str | None, *, dry_run: bool = False) -> dict[str, Any]:
    """Drop legacy task/tenant objects from one SQLite file.

    Returns a report; a missing file is a no-op.  Individual ``DROP COLUMN``
    failures are reported instead of raised so an unusual index/view cannot
    block application startup.
    """

    report: dict[str, Any] = {
        "path": path or "",
        "dropped_tables": [],
        "dropped_columns": [],
        "skipped": [],
        "changed": False,
    }
    if not path or not os.path.exists(path):
        report["skipped"].append("missing")
        return report

    with closing(sqlite3.connect(path)) as conn:
        tables = _sqlite_tables(conn)
        for table in (*LEGACY_TASK_TABLES, *LEGACY_TENANT_TABLES):
            if table not in tables:
                continue
            report["dropped_tables"].append(table)
            if not dry_run:
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            tables.discard(table)
        for table, column in LEGACY_TENANT_COLUMNS.items():
            if table not in tables or column not in _sqlite_columns(conn, table):
                continue
            report["dropped_columns"].append(f"{table}.{column}")
            if dry_run:
                continue
            try:
                conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
            except sqlite3.OperationalError as exc:
                report["dropped_columns"].remove(f"{table}.{column}")
                report["skipped"].append(f"{table}.{column}: {exc}")
        if not dry_run:
            conn.commit()

    report["changed"] = bool(report["dropped_tables"] or report["dropped_columns"])
    return report


def cleanup_postgres(conn, *, dry_run: bool = False) -> dict[str, Any]:
    """Drop legacy task/tenant objects from one PostgreSQL connection.

    ``conn`` is expected to be autocommit (bootstrap uses autocommit
    connections) so each statement is durable on its own.
    """

    report: dict[str, Any] = {
        "dropped_tables": [],
        "dropped_columns": [],
        "changed": False,
    }
    existing_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    for table in (*LEGACY_TASK_TABLES, *LEGACY_TENANT_TABLES):
        if table not in existing_tables:
            continue
        report["dropped_tables"].append(table)
        if not dry_run:
            conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        existing_tables.discard(table)
    existing_columns = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public'"
        )
    }
    for table, column in LEGACY_TENANT_COLUMNS.items():
        if (table, column) not in existing_columns:
            continue
        report["dropped_columns"].append(f"{table}.{column}")
        if not dry_run:
            conn.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{column}"')
    report["changed"] = bool(report["dropped_tables"] or report["dropped_columns"])
    return report


def _normalized_backend(value: object, *, fallback: str = "sqlite") -> str:
    return str(value or "").strip().lower() or fallback


def cleanup_configured(*, dry_run: bool = False) -> dict[str, Any]:
    """Clean every SQLite/PostgreSQL database the current config can touch."""

    import src.settings as settings
    from src.storage import domain_pg

    sqlite_report = []
    paths = [
        str(getattr(settings, "WORKSPACE_DB_PATH", "") or ""),
        str(getattr(settings, "AUTH_DB_PATH", "") or ""),
        # The pre-workspace task database is never opened by current code.
        os.path.join(str(getattr(settings, "STORAGE_DIR", "") or ""), "tasks.db"),
    ]
    seen_paths: set[str] = set()
    for path in paths:
        absolute = os.path.abspath(path) if path else ""
        if not absolute or absolute in seen_paths:
            continue
        seen_paths.add(absolute)
        sqlite_report.append(cleanup_sqlite(absolute, dry_run=dry_run))

    pg_report = []
    dsns: set[str] = set()
    control_backend = _normalized_backend(
        getattr(settings, "CONTROL_PLANE_BACKEND", "sqlite")
    )
    if control_backend == "postgres":
        dsn = str(getattr(settings, "CONTROL_PLANE_DATABASE_URL", "") or "").strip()
        if dsn:
            dsns.add(dsn)
    try:
        auth_backend, auth_dsn = domain_pg.resolve_domain_backend("AUTH_STORE")
    except RuntimeError:
        auth_backend, auth_dsn = "sqlite", ""
    if _normalized_backend(auth_backend) == "postgres" and str(auth_dsn or "").strip():
        dsns.add(str(auth_dsn).strip())
    workspace_backend = _normalized_backend(
        getattr(settings, "WORKSPACE_STORE_BACKEND", ""),
        fallback=control_backend,
    )
    if workspace_backend == "postgres":
        workspace_dsn = str(
            getattr(settings, "WORKSPACE_DB_URL", "") or ""
        ).strip() or str(
            getattr(settings, "CONTROL_PLANE_DATABASE_URL", "") or ""
        ).strip()
        if workspace_dsn:
            dsns.add(workspace_dsn)
    if dsns:
        import psycopg

        for dsn in sorted(dsns):
            with psycopg.connect(
                dsn, autocommit=True, options="-c TimeZone=UTC"
            ) as conn:
                pg_report.append(cleanup_postgres(conn, dry_run=dry_run))

    return {
        "sqlite": sqlite_report,
        "postgres": pg_report,
        "changed": any(item["changed"] for item in (*sqlite_report, *pg_report)),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(cleanup_configured(dry_run="--dry-run" in os.sys.argv), indent=2))
