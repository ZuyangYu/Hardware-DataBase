"""Legacy task/tenant storage cleanup is idempotent and narrowly scoped."""

from __future__ import annotations

import sqlite3

from src.storage.legacy_cleanup import cleanup_sqlite


def _tables(path: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _columns(path: str, table: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _legacy_db(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (id TEXT PRIMARY KEY);
            CREATE TABLE task_turns (task_id TEXT NOT NULL);
            CREATE TABLE task_artifacts (id TEXT PRIMARY KEY);
            CREATE TABLE tenants (id TEXT PRIMARY KEY);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default'
            );
            CREATE TABLE agent_workspaces (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default'
            );
            CREATE TABLE chat_attachments (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default'
            );
            """
        )


def test_cleanup_drops_only_retired_task_and_tenant_objects(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)

    report = cleanup_sqlite(path)
    assert report["changed"] is True
    assert set(report["dropped_tables"]) == {
        "tasks",
        "task_turns",
        "task_artifacts",
        "tenants",
    }
    assert set(report["dropped_columns"]) == {
        "users.tenant_id",
        "agent_workspaces.tenant_id",
    }
    assert _tables(path) == {"users", "agent_workspaces", "chat_attachments"}
    assert "tenant_id" not in _columns(path, "users")
    assert "tenant_id" not in _columns(path, "agent_workspaces")
    # Attachments still use the user id as a compatibility tenant key.
    assert "tenant_id" in _columns(path, "chat_attachments")

    again = cleanup_sqlite(path)
    assert again["changed"] is False
    assert again["dropped_tables"] == []
    assert again["dropped_columns"] == []


def test_cleanup_dry_run_reports_without_mutating(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)

    report = cleanup_sqlite(path, dry_run=True)
    assert report["changed"] is True
    assert "tasks" in _tables(path)
    assert "tenant_id" in _columns(path, "users")


def test_cleanup_ignores_missing_database(tmp_path):
    report = cleanup_sqlite(str(tmp_path / "missing.db"))
    assert report["changed"] is False
    assert report["skipped"] == ["missing"]
