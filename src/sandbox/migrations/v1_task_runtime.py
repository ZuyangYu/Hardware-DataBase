"""v1 migration for runtime-event lineage columns."""

from __future__ import annotations

from typing import Any


def migrate_runtime_events(conn: Any, event_columns: set[str]) -> None:
    """Add the event lineage columns introduced by Runtime Contract v1."""

    migrations = {
        "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        "skill_content_digest": "TEXT NOT NULL DEFAULT ''",
        "runtime_contract_version": "TEXT NOT NULL DEFAULT 'v1'",
    }
    for column, ddl in migrations.items():
        if column not in event_columns:
            conn.execute(f"ALTER TABLE sandbox_task_runtime_events ADD COLUMN {column} {ddl}")
