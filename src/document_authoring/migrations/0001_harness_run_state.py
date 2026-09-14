"""SQLite schema objects used by the HarnessRun data migration.

This migration deliberately does not alter or remove ``harness_checkpoints``.
The application remains responsible for the observation-window read path; this
module only provides durable bookkeeping for the one-time backfill performed by
``migrations.runner``.
"""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "0001_harness_run_state"
SCHEMA_VERSION = 1

# The migration ledger contains identifiers, hashes and stable reasons only.
# It must not become a second copy of evidence, prompts or file contents.
METADATA_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status = 'applied'),
    applied_at TEXT NOT NULL,
    source_snapshot_hash TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    target_snapshot_hash TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    backup_hash TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_authoring_migration_ledger (
    migration_id TEXT NOT NULL,
    object_type TEXT NOT NULL CHECK (object_type IN ('checkpoint', 'receipt')),
    source_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    source_payload_hash TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('converted', 'legacy_terminal')),
    migration_reason TEXT,
    graph_state_hash TEXT,
    target_run_id TEXT,
    target_payload_hash TEXT,
    action_key TEXT,
    attempt INTEGER,
    migrated_at TEXT NOT NULL,
    PRIMARY KEY (migration_id, object_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_document_authoring_migration_ledger_run
    ON document_authoring_migration_ledger(migration_id, source_run_id, object_type);

CREATE TABLE IF NOT EXISTS document_authoring_run_lineage (
    migration_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    source_checkpoint_id TEXT NOT NULL,
    legacy_target_run_id TEXT NOT NULL,
    new_run_id TEXT NOT NULL,
    migration_reason TEXT NOT NULL,
    lineage_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (migration_id, source_run_id),
    UNIQUE (migration_id, new_run_id)
);

CREATE INDEX IF NOT EXISTS idx_document_authoring_run_lineage_new_run
    ON document_authoring_run_lineage(migration_id, new_run_id);
"""


def install_metadata_schema(connection: sqlite3.Connection) -> None:
    """Install the migration ledger schema inside the caller's transaction."""

    # ``sqlite3.Connection.executescript`` issues an implicit COMMIT before
    # running the script.  Execute the statements individually so callers can
    # keep schema installation and the data backfill in one transaction.
    for statement in METADATA_DDL.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)
