"""Focused tests for the Task 5b SQLite migration runner."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.document_authoring.harness.idempotency import receipt_action_key
from src.document_authoring.migrations.runner import (
    MigrationError,
    reverse_migration_drill,
    rollback_migration,
    run_migration,
    verify_sqlite_backup,
)


AS_OF = datetime(2026, 8, 31, tzinfo=timezone.utc)
NOW = "2026-08-31T00:00:00+00:00"


def _payload(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _make_database(tmp_path: Path, *, receipt_columns: bool = False) -> Path:
    database = tmp_path / "document_authoring.db"
    receipt_extra = ", action_key TEXT NOT NULL DEFAULT '', attempt INTEGER NOT NULL DEFAULT 1" if receipt_columns else ""
    connection = sqlite3.connect(database)
    connection.executescript(
        f"""
        CREATE TABLE document_work_orders (
            work_order_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            status TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE authoring_run_manifests (
            run_manifest_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE harness_runs (
            harness_run_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE harness_checkpoints (
            checkpoint_id TEXT PRIMARY KEY, harness_run_id TEXT NOT NULL,
            work_order_id TEXT NOT NULL, status TEXT NOT NULL,
            updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE node_execution_receipts (
            receipt_id TEXT PRIMARY KEY, harness_run_id TEXT NOT NULL,
            node_name TEXT NOT NULL, unit_id TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
            payload_json TEXT NOT NULL{receipt_extra},
            UNIQUE(harness_run_id, node_name, unit_id, input_fingerprint)
        );
        """
    )
    if receipt_columns:
        connection.execute(
            """CREATE UNIQUE INDEX idx_node_execution_receipts_action_key
               ON node_execution_receipts(harness_run_id, action_key)
               WHERE action_key != ''"""
        )
    connection.commit()
    connection.close()
    return database


def _seed_run(
    database: Path,
    number: int,
    *,
    checkpoint_status: str = "paused",
    run_status: str = "queued",
    lease_expires_at: str | None = None,
    fencing_token: int = 0,
    receipt_action: str | None = None,
    receipt_attempt: int | None = None,
) -> tuple[str, str, str]:
    run_id = f"run-{number}"
    work_order_id = f"work-order-{number}"
    manifest_id = f"manifest-{number}"
    fingerprint = f"fingerprint-{number}"
    snapshot_id = f"snapshot-{number}"
    run_payload = {
        "harness_run_id": run_id,
        "work_order_id": work_order_id,
        "run_manifest_id": manifest_id,
        "status": run_status,
        "tenant_id": "tenant-a",
        "input_fingerprint": fingerprint,
        "input_fingerprint_version": 1,
        "source_set_snapshot_id": snapshot_id,
        "current_node": "old-node",
        "step_count": 1,
        "retrieval_round_count": 1,
        "completed_units": 0,
        "total_units": 1,
        "retry_count": 0,
        "max_retries": 1,
        "unit_statuses": {"field-1": "planned"},
        "unit_attempts": {},
        "dispatch_cursor": 0,
        "fencing_token": fencing_token,
        "lease_owner": "worker-1" if lease_expires_at else None,
        "lease_expires_at": lease_expires_at,
        "created_at": NOW,
        "updated_at": NOW,
    }
    work_order_payload = {
        "work_order_id": work_order_id,
        "tenant_id": "tenant-a",
        "execution_mode": "internal_harness",
        "input_fingerprint": fingerprint,
        "source_set_snapshot_id": snapshot_id,
    }
    manifest_payload = {
        "run_manifest_id": manifest_id,
        "work_order_id": work_order_id,
        "input_fingerprint": fingerprint,
    }
    checkpoint_payload = {
        "checkpoint_id": f"checkpoint-{number}",
        "harness_run_id": run_id,
        "work_order_id": work_order_id,
        "input_fingerprint": fingerprint,
        "source_set_snapshot_id": snapshot_id,
        "fencing_token": fencing_token,
        "status": checkpoint_status,
        "current_node": "retrieve_evidence",
        "step_count": 4,
        "retrieval_round_count": 2,
        "completed_units": 1 if checkpoint_status == "completed" else 0,
        "total_units": 1,
        "unit_statuses": {"field-1": "completed" if checkpoint_status == "completed" else "planned"},
        "evidence_matrix_hash": "evidence-hash",
        "draft_ids": ["draft-1"],
        "pending_human_event": {"event_id": "human-1"} if checkpoint_status == "waiting_human" else None,
    }
    receipt_id = f"receipt-{number}"
    receipt_payload = {
        "receipt_id": receipt_id,
        "harness_run_id": run_id,
        "node_name": "draft_ready_unit",
        "unit_id": "field-1",
        "input_fingerprint": f"receipt-input-{number}",
        "status": "started",
    }
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO document_work_orders VALUES (?, ?, ?, ?)",
        (work_order_id, "tenant-a", "planned", _payload(work_order_payload)),
    )
    connection.execute(
        "INSERT INTO authoring_run_manifests VALUES (?, ?, ?)",
        (manifest_id, work_order_id, _payload(manifest_payload)),
    )
    connection.execute(
        "INSERT INTO harness_runs VALUES (?, ?, ?, ?, ?)",
        (run_id, work_order_id, run_status, NOW, _payload(run_payload)),
    )
    connection.execute(
        "INSERT INTO harness_checkpoints VALUES (?, ?, ?, ?, ?, ?)",
        (
            checkpoint_payload["checkpoint_id"], run_id, work_order_id,
            checkpoint_status, NOW, _payload(checkpoint_payload),
        ),
    )
    if receipt_action is None:
        connection.execute(
            """INSERT INTO node_execution_receipts
               (receipt_id, harness_run_id, node_name, unit_id, input_fingerprint, status, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, run_id, "draft_ready_unit", "field-1",
                f"receipt-input-{number}", "started", _payload(receipt_payload),
            ),
        )
    else:
        receipt_payload["action_key"] = receipt_action
        receipt_payload["attempt"] = receipt_attempt or 1
        connection.execute(
            """INSERT INTO node_execution_receipts
               (receipt_id, harness_run_id, node_name, unit_id, input_fingerprint, status,
                payload_json, action_key, attempt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, run_id, "draft_ready_unit", "field-1",
                f"receipt-input-{number}", "started", _payload(receipt_payload),
                receipt_action, receipt_attempt or 1,
            ),
        )
    connection.commit()
    connection.close()
    return run_id, work_order_id, checkpoint_payload["checkpoint_id"]


def _read(database: Path, query: str, params: tuple = ()):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    row = connection.execute(query, params).fetchall()
    connection.close()
    return row


def test_dry_run_is_read_only_and_verifies_backup(tmp_path: Path):
    database = _make_database(tmp_path)
    _seed_run(database, 1, checkpoint_status="paused")
    _seed_run(
        database, 2, checkpoint_status="active", run_status="running",
        lease_expires_at="2026-08-30T23:59:00+00:00", fencing_token=4,
    )
    before_checkpoint = _read(database, "SELECT * FROM harness_checkpoints")
    before_run = _read(database, "SELECT payload_json FROM harness_runs ORDER BY harness_run_id")
    backup = tmp_path / "pre-migration.db"

    report = run_migration(
        database, dry_run=True, verify_backup=True, backup_path=backup, as_of=AS_OF,
    )

    assert report.success and not report.applied
    assert report.backup_verified
    assert report.checkpoint_rows == 2
    assert report.converted_runs == 1
    assert report.legacy_terminal_runs == 1
    assert report.hash_reconciled
    assert backup.exists()
    assert _read(database, "SELECT * FROM harness_checkpoints") == before_checkpoint
    assert _read(database, "SELECT payload_json FROM harness_runs ORDER BY harness_run_id") == before_run
    assert _read(database, "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'") == []
    assert "action_key" not in {row[1] for row in _read(database, "PRAGMA table_info(node_execution_receipts)")}
    assert verify_sqlite_backup(backup).verified


def test_apply_backfills_state_receipts_and_keeps_legacy_table(tmp_path: Path):
    database = _make_database(tmp_path)
    good_run, _, _ = _seed_run(
        database, 1, checkpoint_status="active", run_status="running",
        lease_expires_at="2026-09-01T00:00:00+00:00", fencing_token=3,
    )
    expired_run, _, _ = _seed_run(
        database, 2, checkpoint_status="active", run_status="running",
        lease_expires_at="2026-08-30T23:59:00+00:00", fencing_token=4,
    )
    backup = tmp_path / "pre-migration.db"

    report = run_migration(
        database, verify_backup=True, backup_path=backup, as_of=AS_OF,
    )

    assert report.success and report.applied
    assert report.converted_runs == 1
    assert report.legacy_terminal_runs == 1
    assert report.lineage_rows == 1
    good = json.loads(_read(database, "SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (good_run,))[0][0])
    expired = json.loads(_read(database, "SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (expired_run,))[0][0])
    assert good["status"] == "running"
    assert good["migration_state"] == "converted"
    assert good["unit_attempts"] == {"field-1": 1}
    assert good["max_retries"] == 1
    assert expired["status"] == "paused"
    assert expired["migration_state"] == "legacy_terminal"
    assert expired["migration_reason"] == "legacy_active_lease_expired"
    assert expired["lease_owner"] is None

    receipt = _read(
        database,
        "SELECT harness_run_id, node_name, unit_id, input_fingerprint, action_key, attempt, payload_json FROM node_execution_receipts ORDER BY receipt_id",
    )[0]
    expected_key = receipt_action_key(
        harness_run_id=receipt["harness_run_id"], node_name=receipt["node_name"],
        unit_id=receipt["unit_id"], attempt=1, input_fingerprint=receipt["input_fingerprint"],
        action=receipt["node_name"],
    )
    assert receipt["action_key"] == expected_key
    assert receipt["attempt"] == 1
    assert json.loads(receipt["payload_json"])["action_key"] == expected_key
    assert _read(database, "SELECT name FROM sqlite_master WHERE name = 'harness_checkpoints'")
    assert _read(database, "SELECT COUNT(*) AS count FROM harness_checkpoints")[0]["count"] == 2
    work_order = json.loads(_read(database, "SELECT payload_json FROM document_work_orders WHERE work_order_id = 'work-order-1'")[0][0])
    manifest = json.loads(_read(database, "SELECT payload_json FROM authoring_run_manifests WHERE run_manifest_id = 'manifest-1'")[0][0])
    assert work_order["requested_executor"] == "internal_harness"
    assert manifest["input_fingerprint_version"] == 1
    lineage = _read(database, "SELECT new_run_id FROM document_authoring_run_lineage")[0][0]
    recovery = json.loads(_read(database, "SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (lineage,))[0][0])
    assert recovery["status"] == "planned"
    assert recovery["lineage"]["source_harness_run_id"] == expired_run
    assert reverse_migration_drill(database).verified


def test_current_store_receipt_schema_is_compatible(tmp_path: Path):
    database = _make_database(tmp_path, receipt_columns=True)
    _seed_run(database, 1, checkpoint_status="completed", receipt_action="existing-key", receipt_attempt=2)
    report = run_migration(database, backup_path=tmp_path / "pre.db", as_of=AS_OF)

    assert report.success
    receipt = _read(database, "SELECT action_key, attempt FROM node_execution_receipts")[0]
    assert (receipt["action_key"], receipt["attempt"]) == ("existing-key", 2)


def test_multiple_checkpoints_keep_latest_run_projection_and_audit_every_row(tmp_path: Path):
    database = _make_database(tmp_path)
    _seed_run(database, 1, checkpoint_status="paused")
    connection = sqlite3.connect(database)
    latest = {
        "checkpoint_id": "checkpoint-latest",
        "harness_run_id": "run-1",
        "work_order_id": "work-order-1",
        "input_fingerprint": "fingerprint-1",
        "source_set_snapshot_id": "snapshot-1",
        "fencing_token": 0,
        "status": "completed",
        "current_node": "complete",
        "step_count": 9,
        "completed_units": 1,
        "total_units": 1,
        "unit_statuses": {"field-1": "completed"},
    }
    connection.execute(
        "INSERT INTO harness_checkpoints VALUES (?, ?, ?, ?, ?, ?)",
        ("checkpoint-latest", "run-1", "work-order-1", "completed", "2026-08-31T00:01:00+00:00", _payload(latest)),
    )
    connection.commit()
    connection.close()

    report = run_migration(database, backup_path=tmp_path / "pre.db", as_of=AS_OF)

    assert report.success and report.checkpoint_rows == 2
    assert report.checkpoint_runs == 1
    run = json.loads(_read(database, "SELECT payload_json FROM harness_runs WHERE harness_run_id = 'run-1'")[0][0])
    assert run["checkpoint_id"] == "checkpoint-latest"
    assert run["status"] == "completed"
    assert _read(database, "SELECT COUNT(*) AS count FROM document_authoring_migration_ledger WHERE object_type = 'checkpoint'")[0]["count"] == 2
    assert reverse_migration_drill(database).verified


def test_failure_on_receipt_key_collision_aborts_without_partial_writes(tmp_path: Path):
    database = _make_database(tmp_path)
    _seed_run(database, 1)
    _seed_run(database, 2)
    connection = sqlite3.connect(database)
    # Distinct legacy receipts may carry an already populated key in their
    # payload even when the old table had no action_key column.
    for receipt_id in ("receipt-1", "receipt-2"):
        connection.execute(
            "UPDATE node_execution_receipts SET payload_json = json_set(payload_json, '$.action_key', ?) WHERE receipt_id = ?",
            ("same-action-key", receipt_id),
        )
    connection.execute(
        """UPDATE node_execution_receipts
           SET harness_run_id = 'run-1', unit_id = 'field-2',
               input_fingerprint = 'receipt-input-2',
               payload_json = json_set(
                   json_set(json_set(payload_json, '$.harness_run_id', 'run-1'),
                            '$.unit_id', 'field-2'),
                   '$.input_fingerprint', 'receipt-input-2')
           WHERE receipt_id = 'receipt-2'"""
    )
    connection.commit()
    connection.close()
    before_runs = _read(database, "SELECT payload_json FROM harness_runs ORDER BY harness_run_id")

    with pytest.raises(MigrationError, match="action_key collision"):
        run_migration(database, backup_path=tmp_path / "pre.db", as_of=AS_OF)

    assert _read(database, "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'") == []
    assert "action_key" not in {row[1] for row in _read(database, "PRAGMA table_info(node_execution_receipts)")}
    assert _read(database, "SELECT payload_json FROM harness_runs ORDER BY harness_run_id") == before_runs


def test_sqlite_write_failure_rolls_back_schema_and_rows(tmp_path: Path):
    database = _make_database(tmp_path)
    _seed_run(database, 1)
    before_run = _read(database, "SELECT payload_json FROM harness_runs WHERE harness_run_id = 'run-1'")[0][0]
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TRIGGER fail_harness_run_migration
           BEFORE UPDATE OF payload_json ON harness_runs
           BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"""
    )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationError, match="rolled back"):
        run_migration(database, backup_path=tmp_path / "pre.db", as_of=AS_OF)

    assert _read(database, "SELECT payload_json FROM harness_runs WHERE harness_run_id = 'run-1'")[0][0] == before_run
    assert _read(database, "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'") == []
    assert "action_key" not in {row[1] for row in _read(database, "PRAGMA table_info(node_execution_receipts)")}


def test_already_applied_is_idempotent_and_new_checkpoint_write_is_rejected(tmp_path: Path):
    database = _make_database(tmp_path)
    _seed_run(database, 1)
    backup = tmp_path / "pre.db"
    first = run_migration(database, backup_path=backup, as_of=AS_OF)
    second = run_migration(database, verify_backup=True, backup_path=backup, as_of=AS_OF)
    assert first.applied
    assert second.already_applied and not second.applied

    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO harness_checkpoints
           (checkpoint_id, harness_run_id, work_order_id, status, updated_at, payload_json)
           SELECT 'checkpoint-new', harness_run_id, work_order_id, 'paused',
                  '2026-08-31T00:01:00+00:00', payload_json
           FROM harness_checkpoints LIMIT 1"""
    )
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="legacy source changed"):
        run_migration(database, backup_path=backup, as_of=AS_OF)


def test_rollback_restores_pre_migration_file_and_reverse_drill_is_verifiable(tmp_path: Path):
    database = _make_database(tmp_path)
    _seed_run(database, 1, checkpoint_status="waiting_human")
    backup = tmp_path / "pre.db"
    run_migration(database, backup_path=backup, as_of=AS_OF)
    assert reverse_migration_drill(database).verified

    rollback = rollback_migration(
        database, backup, safety_backup_path=tmp_path / "post.db",
    )

    assert rollback.verified
    assert (tmp_path / "post.db").exists()
    assert _read(database, "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'") == []
    assert _read(database, "SELECT status FROM harness_runs WHERE harness_run_id = 'run-1'")[0][0] == "queued"
    assert _read(database, "SELECT COUNT(*) AS count FROM harness_checkpoints")[0]["count"] == 1
    assert "action_key" not in {row[1] for row in _read(database, "PRAGMA table_info(node_execution_receipts)")}
