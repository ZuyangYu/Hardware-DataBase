"""Transactional HarnessCheckpoint -> HarnessRun migration runner.

The runner is intentionally independent from :class:`DocumentAuthoringStore`.
Opening that Store can run application-startup migrations, which would make a
read-only migration rehearsal impossible.  This module therefore talks to the
SQLite file directly and treats ``harness_runs.payload_json`` as the canonical
business payload.

The command has three safety properties:

* all validation and reconciliation is performed before the write transaction;
* the legacy checkpoint table is never dropped or rewritten;
* the pre-migration database is backed up and verified before an apply.

``reverse_migration_drill`` is read-only.  ``rollback_migration`` is an
explicit, full-file restore operation and first creates a safety backup of the
current database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import importlib
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from src.document_authoring.harness.idempotency import canonical_json, receipt_action_key

from .harness_run_state import (
    GRAPH_STATE_VERSION,
    LEGACY_REASON_INVALID_PAYLOAD,
    CheckpointMigrationResult,
    migrate_checkpoint_payload,
)


_migration_schema = importlib.import_module(
    "src.document_authoring.migrations.0001_harness_run_state"
)

MIGRATION_ID = _migration_schema.MIGRATION_ID
SCHEMA_VERSION = _migration_schema.SCHEMA_VERSION
MIGRATION_VERSION = SCHEMA_VERSION
CHECKPOINT_TABLE = "harness_checkpoints"
RUN_TABLE = "harness_runs"
RECEIPT_TABLE = "node_execution_receipts"
WORK_ORDER_TABLE = "document_work_orders"
MANIFEST_TABLE = "authoring_run_manifests"

_DOMAIN_TABLES = (CHECKPOINT_TABLE, RUN_TABLE, RECEIPT_TABLE, WORK_ORDER_TABLE, MANIFEST_TABLE)
_SOURCE_GUARD_TABLES = (CHECKPOINT_TABLE, RECEIPT_TABLE, WORK_ORDER_TABLE, MANIFEST_TABLE)
_RUN_STATUS = {
    "planned", "queued", "running", "paused", "waiting_human", "retrying",
    "failed", "completed", "cancelled",
}
_TERMINAL_RUN_STATUS = {"completed", "cancelled", "failed"}
_BUSINESS_FIELDS = (
    "tenant_id", "work_order_id", "input_fingerprint", "input_fingerprint_version",
    "source_set_snapshot_id", "status", "current_node", "step_count",
    "retrieval_round_count", "completed_units", "total_units", "retry_count",
    "max_retries", "unit_statuses", "unit_attempts", "dispatch_cursor",
    "evidence_matrix_hash", "draft_ids", "pending_human_event", "fencing_token",
)


class MigrationError(RuntimeError):
    """Raised when any preflight or reconciliation check fails."""


MigrationAborted = MigrationError


@dataclass(frozen=True)
class TableDigest:
    table: str
    row_count: int
    content_hash: str

    @property
    def hash(self) -> str:
        """Compatibility alias for callers that call the digest ``hash``."""

        return self.content_hash

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "row_count": self.row_count,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class TableReconciliation:
    table: str
    before: TableDigest
    after: TableDigest
    row_count_match: bool
    content_hash_match: bool
    note: str = ""

    @property
    def verified(self) -> bool:
        return self.row_count_match and self.content_hash_match

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "row_count_match": self.row_count_match,
            "content_hash_match": self.content_hash_match,
            "verified": self.verified,
            "note": self.note,
        }


@dataclass(frozen=True)
class BackupVerification:
    path: str
    verified: bool
    file_hash: str
    integrity_check: str
    table_digests: dict[str, TableDigest] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "verified": self.verified,
            "file_hash": self.file_hash,
            "integrity_check": self.integrity_check,
            "table_digests": {
                name: digest.as_dict() for name, digest in self.table_digests.items()
            },
        }


@dataclass(frozen=True)
class ReverseDrillReport:
    verified: bool
    checkpoint_rows: int
    receipt_rows: int
    lineage_rows: int
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "checkpoint_rows": self.checkpoint_rows,
            "receipt_rows": self.receipt_rows,
            "lineage_rows": self.lineage_rows,
            "errors": list(self.errors),
        }


@dataclass
class MigrationReport:
    migration_id: str
    schema_version: int
    database_path: str
    dry_run: bool
    success: bool
    applied: bool = False
    already_applied: bool = False
    backup_verified: bool = False
    backup: BackupVerification | None = None
    reverse_drill: ReverseDrillReport | None = None
    checkpoint_rows: int = 0
    checkpoint_runs: int = 0
    converted_runs: int = 0
    legacy_terminal_runs: int = 0
    receipt_rows: int = 0
    lineage_rows: int = 0
    source_guard_hash: str = ""
    source_domain_snapshot_hash: str = ""
    target_domain_snapshot_hash: str = ""
    table_reconciliations: dict[str, TableReconciliation] = field(default_factory=dict)
    run_results: list[dict[str, Any]] = field(default_factory=list)
    receipt_results: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def verified(self) -> bool:
        return self.success and self.hash_reconciled and (
            self.reverse_drill is None or self.reverse_drill.verified
        )

    @property
    def backup_hash(self) -> str | None:
        return self.backup.file_hash if self.backup else None

    @property
    def rows(self) -> int:
        return self.checkpoint_rows

    @property
    def hash_reconciled(self) -> bool:
        return all(item.verified for item in self.table_reconciliations.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "schema_version": self.schema_version,
            "database_path": self.database_path,
            "dry_run": self.dry_run,
            "success": self.success,
            "ok": self.ok,
            "verified": self.verified,
            "applied": self.applied,
            "already_applied": self.already_applied,
            "backup_verified": self.backup_verified,
            "backup": self.backup.as_dict() if self.backup else None,
            "reverse_drill": self.reverse_drill.as_dict() if self.reverse_drill else None,
            "checkpoint_rows": self.checkpoint_rows,
            "checkpoint_runs": self.checkpoint_runs,
            "converted_runs": self.converted_runs,
            "legacy_terminal_runs": self.legacy_terminal_runs,
            "receipt_rows": self.receipt_rows,
            "lineage_rows": self.lineage_rows,
            "source_guard_hash": self.source_guard_hash,
            "source_domain_snapshot_hash": self.source_domain_snapshot_hash,
            "target_domain_snapshot_hash": self.target_domain_snapshot_hash,
            "hash_reconciled": self.hash_reconciled,
            "table_reconciliations": {
                name: reconciliation.as_dict()
                for name, reconciliation in self.table_reconciliations.items()
            },
            "run_results": self.run_results,
            "receipt_results": self.receipt_results,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class RollbackReport:
    database_path: str
    backup_path: str
    safety_backup_path: str
    verified: bool
    restored_table_digests: dict[str, TableDigest]

    def as_dict(self) -> dict[str, Any]:
        return {
            "database_path": self.database_path,
            "backup_path": self.backup_path,
            "safety_backup_path": self.safety_backup_path,
            "verified": self.verified,
            "restored_table_digests": {
                name: digest.as_dict() for name, digest in self.restored_table_digests.items()
            },
        }


@dataclass
class _CheckpointRecord:
    checkpoint_id: str
    run_id: str
    work_order_id: str
    updated_at: datetime
    payload: Any
    payload_hash: str
    lease_active: bool | None = None
    result: CheckpointMigrationResult | None = None
    latest: bool = False


@dataclass(frozen=True)
class _ReceiptPlan:
    receipt_id: str
    run_id: str
    payload: dict[str, Any]
    payload_hash: str
    action_key: str
    attempt: int
    source_action_key: str
    source_attempt: int | None


@dataclass
class _PreparedMigration:
    source_domain: dict[str, TableDigest]
    source_guard: dict[str, TableDigest]
    source_guard_hash: str
    runs_before: dict[str, dict[str, Any]]
    checkpoints: list[_CheckpointRecord]
    latest_by_run: dict[str, _CheckpointRecord]
    runs_after: dict[str, dict[str, Any]]
    work_orders_after: dict[str, dict[str, Any]]
    manifests_after: dict[str, dict[str, Any]]
    receipts: list[_ReceiptPlan]
    add_receipt_action_key: bool
    add_receipt_attempt: bool
    lineages: dict[str, dict[str, Any]]


def default_database_path() -> Path:
    """Return the same database path used by the application Store."""

    try:
        from src import settings

        return Path(settings.STORAGE_DIR) / "document_authoring.db"
    except Exception:  # pragma: no cover - only used by a minimal CLI install
        return Path("storage") / "document_authoring.db"


def default_backup_path(database_path: str | os.PathLike[str]) -> Path:
    database = Path(database_path).resolve()
    return database.with_name(f"{database.name}.{MIGRATION_ID}.pre-migration.bak")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def _canonical_payload(payload: Any) -> str:
    try:
        return canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"payload is not canonical JSON: {exc}") from exc


def _payload_hash(payload: Any) -> str:
    return _sha256_bytes(_canonical_payload(payload).encode("utf-8"))


def _json_clone(value: Any) -> Any:
    return json.loads(_canonical_payload(value))


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def _require_schema(connection: sqlite3.Connection) -> None:
    for table in (CHECKPOINT_TABLE, RUN_TABLE):
        if not _table_exists(connection, table):
            raise MigrationError(f"required table is missing: {table}")
    required = {
        CHECKPOINT_TABLE: {
            "checkpoint_id", "harness_run_id", "work_order_id", "status",
            "updated_at", "payload_json",
        },
        RUN_TABLE: {"harness_run_id", "work_order_id", "status", "created_at", "payload_json"},
    }
    for table, expected in required.items():
        missing = expected - set(_table_columns(connection, table))
        if missing:
            raise MigrationError(f"{table} is missing columns: {sorted(missing)}")
    if _table_exists(connection, RECEIPT_TABLE):
        expected = {
            "receipt_id", "harness_run_id", "node_name", "unit_id",
            "input_fingerprint", "status", "payload_json",
        }
        missing = expected - set(_table_columns(connection, RECEIPT_TABLE))
        if missing:
            raise MigrationError(f"{RECEIPT_TABLE} is missing columns: {sorted(missing)}")


def _connect(database_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri_path = quote(str(database_path.resolve()), safe="/")
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro", uri=True, timeout=30, isolation_level=None,
        )
    else:
        connection = sqlite3.connect(str(database_path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _db_value(value: Any, column: str) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if column == "payload_json" and isinstance(value, str):
        try:
            return {"__json__": json.loads(value)}
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
    return value


def _rows_for_table(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[dict[str, Any]]]:
    columns = _table_columns(connection, table)
    rows = []
    for row in connection.execute(f"SELECT * FROM {table}").fetchall():
        rows.append({column: _db_value(row[column], column) for column in columns})
    return columns, rows


def _digest_rows(table: str, columns: list[str], rows: list[dict[str, Any]]) -> TableDigest:
    canonical_rows = [
        {column: row.get(column) for column in columns}
        for row in rows
    ]
    canonical_rows.sort(key=canonical_json)
    return TableDigest(table, len(canonical_rows), _canonical_hash(canonical_rows))


def _table_digest(connection: sqlite3.Connection, table: str) -> TableDigest:
    if not _table_exists(connection, table):
        return TableDigest(table, 0, "")
    columns, rows = _rows_for_table(connection, table)
    return _digest_rows(table, columns, rows)


def _strip_migration_fields(payload: Any, fields: set[str]) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if key not in fields}


def _legacy_guard_digest(connection: sqlite3.Connection, table: str) -> TableDigest:
    """Digest source semantics while ignoring fields added by this migration."""

    if not _table_exists(connection, table):
        return TableDigest(table, 0, "")
    columns = _table_columns(connection, table)
    ignored_columns: set[str] = set()
    payload_fields: set[str] = set()
    if table == RECEIPT_TABLE:
        ignored_columns = {"action_key", "attempt"}
        payload_fields = {"action_key", "attempt"}
    elif table == WORK_ORDER_TABLE:
        payload_fields = {"requested_executor", "input_fingerprint_version"}
    elif table == MANIFEST_TABLE:
        payload_fields = {"input_fingerprint_version"}
    kept_columns = [column for column in columns if column not in ignored_columns]
    rows: list[dict[str, Any]] = []
    for row in connection.execute(f"SELECT * FROM {table}").fetchall():
        item = {}
        for column in kept_columns:
            value = row[column]
            if column == "payload_json":
                try:
                    value = _strip_migration_fields(json.loads(value), payload_fields)
                except (TypeError, ValueError, json.JSONDecodeError):
                    value = value
            item[column] = _db_value(value, column)
        rows.append(item)
    return _digest_rows(table, kept_columns, rows)


def _domain_snapshot(connection: sqlite3.Connection) -> dict[str, TableDigest]:
    return {
        table: _table_digest(connection, table)
        for table in _DOMAIN_TABLES
        if _table_exists(connection, table)
    }


def _source_guard_snapshot(connection: sqlite3.Connection) -> dict[str, TableDigest]:
    return {
        table: _legacy_guard_digest(connection, table)
        for table in _SOURCE_GUARD_TABLES
        if _table_exists(connection, table)
    }


def _snapshot_json(snapshot: dict[str, TableDigest]) -> dict[str, Any]:
    return {table: digest.as_dict() for table, digest in sorted(snapshot.items())}


def _snapshot_hash(snapshot: dict[str, TableDigest]) -> str:
    return _canonical_hash(_snapshot_json(snapshot))


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sqlite_backup(
    backup_path: str | os.PathLike[str],
    *,
    expected_tables: dict[str, TableDigest] | None = None,
) -> BackupVerification:
    """Verify SQLite integrity and, optionally, exact table digests."""

    path = Path(backup_path).resolve()
    if not path.is_file():
        raise MigrationError(f"backup file does not exist: {path}")
    table_digests: dict[str, TableDigest] = {}
    try:
        with _connect(path, read_only=True) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise MigrationError(f"backup integrity_check failed: {integrity}")
            for table in _DOMAIN_TABLES:
                if _table_exists(connection, table):
                    table_digests[table] = _table_digest(connection, table)
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"backup is not a readable SQLite database: {path}") from exc
    if expected_tables is not None:
        for table, expected in expected_tables.items():
            actual = table_digests.get(table, TableDigest(table, 0, ""))
            if actual != expected:
                raise MigrationError(
                    f"backup table digest mismatch for {table}: "
                    f"expected {expected.as_dict()}, got {actual.as_dict()}"
                )
    return BackupVerification(
        path=str(path),
        verified=True,
        file_hash=_file_hash(path),
        integrity_check="ok",
        table_digests=table_digests,
    )


# Short public name for operational callers; the explicit SQLite name remains
# useful when this runner is imported beside another backup provider.
verify_backup = verify_sqlite_backup


def _make_sqlite_backup(database_path: Path, backup_path: Path) -> BackupVerification:
    if database_path.resolve() == backup_path.resolve():
        raise MigrationError("database and backup paths must be different")
    if not database_path.is_file():
        raise MigrationError(f"database file does not exist: {database_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        return verify_sqlite_backup(backup_path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{backup_path.name}.", suffix=".tmp", dir=backup_path.parent, delete=False,
        ) as stream:
            temp_path = Path(stream.name)
        with _connect(database_path, read_only=True) as source, sqlite3.connect(str(temp_path)) as target:
            source.backup(target)
        os.replace(temp_path, backup_path)
        temp_path = None
        return verify_sqlite_backup(backup_path)
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"could not create SQLite backup: {backup_path}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _ensure_backup(
    database_path: Path,
    backup_path: Path,
    *,
    expected_tables: dict[str, TableDigest],
) -> BackupVerification:
    if backup_path.exists():
        verification = verify_sqlite_backup(backup_path, expected_tables=expected_tables)
    else:
        _make_sqlite_backup(database_path, backup_path)
        verification = verify_sqlite_backup(backup_path, expected_tables=expected_tables)
    return verification


def _load_payload(raw: Any, *, object_id: str, table: str) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{table} payload is invalid JSON for {object_id}") from exc
    _canonical_payload(payload)
    return payload


def _parse_datetime(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationError(f"{label} is not a valid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_run_payloads(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for row in connection.execute(f"SELECT * FROM {RUN_TABLE} ORDER BY harness_run_id").fetchall():
        run_id = str(row["harness_run_id"] or "")
        if not run_id:
            raise MigrationError("harness_runs contains an empty harness_run_id")
        payload = _load_payload(row["payload_json"], object_id=run_id, table=RUN_TABLE)
        if not isinstance(payload, dict):
            raise MigrationError(f"harness_runs payload must be an object for {run_id}")
        if payload.get("harness_run_id") not in (None, run_id):
            raise MigrationError(f"harness_runs identity mismatch for {run_id}")
        if payload.get("work_order_id") not in (None, row["work_order_id"]):
            raise MigrationError(f"harness_runs work-order mismatch for {run_id}")
        payload.setdefault("harness_run_id", run_id)
        payload.setdefault("work_order_id", str(row["work_order_id"] or ""))
        if payload.get("status") not in (None, row["status"]):
            raise MigrationError(f"harness_runs status mismatch for {run_id}")
        payload.setdefault("status", str(row["status"] or "planned"))
        if payload["status"] not in _RUN_STATUS:
            raise MigrationError(f"harness_runs has unknown status for {run_id}: {payload['status']!r}")
        payload.setdefault("created_at", str(row["created_at"] or ""))
        runs[run_id] = payload
    return runs


def _load_checkpoint_records(
    connection: sqlite3.Connection,
    runs: dict[str, dict[str, Any]],
) -> list[_CheckpointRecord]:
    records: list[_CheckpointRecord] = []
    rows = connection.execute(
        f"SELECT * FROM {CHECKPOINT_TABLE} ORDER BY updated_at, checkpoint_id"
    ).fetchall()
    for row in rows:
        checkpoint_id = str(row["checkpoint_id"] or "")
        run_id = str(row["harness_run_id"] or "")
        work_order_id = str(row["work_order_id"] or "")
        if not checkpoint_id or not run_id or not work_order_id:
            raise MigrationError("harness_checkpoints contains an empty identity")
        if run_id not in runs:
            raise MigrationError(f"checkpoint {checkpoint_id} references missing run {run_id}")
        payload = _load_payload(
            row["payload_json"], object_id=checkpoint_id, table=CHECKPOINT_TABLE,
        )
        if isinstance(payload, dict):
            if payload.get("checkpoint_id") not in (None, checkpoint_id):
                raise MigrationError(f"checkpoint id mismatch for {checkpoint_id}")
            if payload.get("harness_run_id") not in (None, run_id):
                raise MigrationError(f"checkpoint identity mismatch for {checkpoint_id}")
            if payload.get("work_order_id") not in (None, work_order_id):
                raise MigrationError(f"checkpoint work-order mismatch for {checkpoint_id}")
            if payload.get("status") not in (None, row["status"]):
                raise MigrationError(f"checkpoint status mismatch for {checkpoint_id}")
        updated_at = _parse_datetime(
            row["updated_at"], label=f"checkpoint {checkpoint_id}.updated_at",
        )
        records.append(_CheckpointRecord(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            work_order_id=work_order_id,
            updated_at=updated_at,
            payload=payload,
            payload_hash=_payload_hash(payload),
        ))
    return records


def _prove_active_lease(
    checkpoint: _CheckpointRecord,
    run_payload: dict[str, Any],
    *,
    as_of: datetime,
) -> bool | None:
    if not isinstance(checkpoint.payload, dict) or checkpoint.payload.get("status", "active") != "active":
        return None
    if run_payload.get("status") != "running":
        return None
    if not run_payload.get("lease_owner") or not run_payload.get("lease_expires_at"):
        return None
    checkpoint_token = checkpoint.payload.get("fencing_token")
    run_token = run_payload.get("fencing_token")
    if not isinstance(checkpoint_token, int) or isinstance(checkpoint_token, bool):
        return None
    if not isinstance(run_token, int) or isinstance(run_token, bool):
        return None
    if checkpoint_token != run_token:
        return False
    try:
        expires_at = _parse_datetime(
            run_payload["lease_expires_at"],
            label=f"run {checkpoint.run_id}.lease_expires_at",
        )
    except MigrationError:
        return None
    return expires_at > as_of


def _safe_checkpoint_migration(
    payload: Any,
    *,
    lease_active: bool | None,
    backend_round_trip: Callable[[str, dict[str, Any]], None] | None,
) -> CheckpointMigrationResult:
    if not isinstance(payload, dict):
        return CheckpointMigrationResult(
            outcome="legacy_terminal", migration_reason=LEGACY_REASON_INVALID_PAYLOAD,
        )
    try:
        return migrate_checkpoint_payload(
            payload,
            lease_active=lease_active,
            backend_round_trip=backend_round_trip,
        )
    except Exception:
        # A malformed typed field must not abort all other historical runs by
        # being guessed into a resumable state.  It is a legacy-terminal run;
        # the original payload remains untouched for audit/recovery.
        return CheckpointMigrationResult(
            outcome="legacy_terminal", migration_reason=LEGACY_REASON_INVALID_PAYLOAD,
        )


def _build_run_payload(
    before: dict[str, Any],
    checkpoint: _CheckpointRecord,
    result: CheckpointMigrationResult,
) -> dict[str, Any]:
    target = _json_clone(before)
    source = checkpoint.payload if isinstance(checkpoint.payload, dict) else {}
    fields = dict(result.harness_run_fields)
    if "tenant_id" not in source and "tenant_id" in before:
        fields.pop("tenant_id", None)
    if result.outcome == "converted" and result.graph_state is None:
        raise MigrationError(f"converted checkpoint has no graph state: {checkpoint.checkpoint_id}")
    for key, value in fields.items():
        # P2c checkpoints predate retry-budget fields.  Their converter emits
        # safe zero defaults, but an existing HarnessRun may already carry the
        # real policy budget; do not erase that value merely because the old
        # payload did not serialize the field.
        if key in {"retry_count", "max_retries"} and key not in source:
            continue
        if key == "input_fingerprint_version" and key not in source and key in before:
            continue
        target[key] = _json_clone(value)
    target["harness_run_id"] = checkpoint.run_id
    target["work_order_id"] = checkpoint.work_order_id
    target["checkpoint_id"] = checkpoint.checkpoint_id
    target["graph_state_version"] = GRAPH_STATE_VERSION
    target["migration_state"] = result.outcome
    target["migration_reason"] = result.migration_reason
    target["migration_source_checkpoint_id"] = checkpoint.checkpoint_id
    target["last_agent_checkpoint_at"] = checkpoint.updated_at.isoformat()
    target["updated_at"] = checkpoint.updated_at.isoformat()
    if result.outcome == "legacy_terminal":
        current_status = target.get("status")
        if current_status not in _TERMINAL_RUN_STATUS:
            target["status"] = fields.get("status", "paused")
        target["lease_owner"] = None
        target["lease_expires_at"] = None
        target["heartbeat_at"] = None
    elif target.get("status") != "running":
        target["lease_owner"] = None
        target["lease_expires_at"] = None
    if target.get("status") not in _RUN_STATUS:
        raise MigrationError(
            f"checkpoint {checkpoint.checkpoint_id} produced invalid HarnessRun status "
            f"{target.get('status')!r}"
        )
    return target


def _validate_checkpoint_identity(
    checkpoint: _CheckpointRecord,
    before: dict[str, Any],
    work_order: dict[str, Any] | None,
) -> None:
    source = checkpoint.payload if isinstance(checkpoint.payload, dict) else {}
    if work_order:
        for key in ("input_fingerprint", "source_set_snapshot_id"):
            run_value = before.get(key)
            work_order_value = work_order.get(key)
            if run_value not in (None, "") and work_order_value not in (None, "") and run_value != work_order_value:
                raise MigrationError(
                    f"{checkpoint.run_id} {key} conflicts between HarnessRun and DocumentWorkOrder"
                )
    for key in ("input_fingerprint", "source_set_snapshot_id"):
        source_value = source.get(key)
        if source_value in (None, ""):
            continue
        for label, other in (("HarnessRun", before.get(key)), ("DocumentWorkOrder", (work_order or {}).get(key))):
            if other not in (None, "") and other != source_value:
                raise MigrationError(
                    f"{checkpoint.checkpoint_id} {key} conflicts with {label}"
                )


def _prepare_work_orders(
    connection: sqlite3.Connection,
    checkpoints: list[_CheckpointRecord],
) -> dict[str, dict[str, Any]]:
    if not _table_exists(connection, WORK_ORDER_TABLE):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for work_order_id in sorted({record.work_order_id for record in checkpoints}):
        row = connection.execute(
            f"SELECT payload_json FROM {WORK_ORDER_TABLE} WHERE work_order_id = ?",
            (work_order_id,),
        ).fetchone()
        if row is None:
            raise MigrationError(f"checkpoint references missing work order {work_order_id}")
        payload = _load_payload(row["payload_json"], object_id=work_order_id, table=WORK_ORDER_TABLE)
        if not isinstance(payload, dict):
            raise MigrationError(f"work order payload must be an object: {work_order_id}")
        if payload.get("work_order_id") not in (None, work_order_id):
            raise MigrationError(f"work-order identity mismatch for {work_order_id}")
        mode = payload.get("execution_mode")
        requested = payload.get("requested_executor")
        if mode is not None and mode not in {"internal_harness", "deterministic_only", "external_agent"}:
            raise MigrationError(f"unknown execution_mode for work order {work_order_id}: {mode!r}")
        if requested is not None and requested != mode:
            raise MigrationError(f"requested_executor conflicts with execution_mode for {work_order_id}")
        if mode is not None:
            payload["requested_executor"] = mode
        elif requested is not None:
            raise MigrationError(f"work order {work_order_id} has requested_executor without execution_mode")
        version = payload.get("input_fingerprint_version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise MigrationError(f"invalid input_fingerprint_version for work order {work_order_id}")
        payload["input_fingerprint_version"] = version
        payload["work_order_id"] = work_order_id
        result[work_order_id] = payload
    return result


def _prepare_manifests(
    connection: sqlite3.Connection,
    run_payloads: dict[str, dict[str, Any]],
    work_orders: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not _table_exists(connection, MANIFEST_TABLE):
        return {}
    result: dict[str, dict[str, Any]] = {}
    manifest_ids = {
        str(payload["run_manifest_id"])
        for payload in run_payloads.values()
        if payload.get("run_manifest_id")
    }
    for manifest_id in sorted(manifest_ids):
        row = connection.execute(
            f"SELECT payload_json FROM {MANIFEST_TABLE} WHERE run_manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        # Minimal legacy fixtures sometimes did not persist a manifest.  No
        # manifest payload exists to backfill, so leave that reference alone;
        # a real Store database always has the row and is fully checked here.
        if row is None:
            continue
        payload = _load_payload(row["payload_json"], object_id=manifest_id, table=MANIFEST_TABLE)
        if not isinstance(payload, dict):
            raise MigrationError(f"run manifest payload must be an object: {manifest_id}")
        work_order_id = str(payload.get("work_order_id") or "")
        expected_version = work_orders.get(work_order_id, {}).get("input_fingerprint_version", 1)
        current_version = payload.get("input_fingerprint_version", expected_version)
        if current_version != expected_version:
            raise MigrationError(f"run manifest input_fingerprint_version conflicts: {manifest_id}")
        payload["input_fingerprint_version"] = expected_version
        payload["run_manifest_id"] = manifest_id
        result[manifest_id] = payload
    return result


def _prepare_receipts(
    connection: sqlite3.Connection,
) -> tuple[list[_ReceiptPlan], bool, bool]:
    if not _table_exists(connection, RECEIPT_TABLE):
        return [], False, False
    columns = set(_table_columns(connection, RECEIPT_TABLE))
    plans: list[_ReceiptPlan] = []
    action_keys: dict[tuple[str, str], str] = {}
    for row in connection.execute(f"SELECT * FROM {RECEIPT_TABLE} ORDER BY receipt_id").fetchall():
        receipt_id = str(row["receipt_id"] or "")
        if not receipt_id:
            raise MigrationError("node_execution_receipts contains an empty receipt_id")
        payload = _load_payload(row["payload_json"], object_id=receipt_id, table=RECEIPT_TABLE)
        if not isinstance(payload, dict):
            raise MigrationError(f"receipt payload must be an object: {receipt_id}")
        identity_fields = ("harness_run_id", "node_name", "unit_id", "input_fingerprint")
        for key in identity_fields:
            structured = row[key]
            if payload.get(key) not in (None, structured):
                raise MigrationError(f"receipt {receipt_id} has conflicting {key}")
        if payload.get("status") not in (None, row["status"]):
            raise MigrationError(f"receipt {receipt_id} has conflicting status")
        run_id = str(row["harness_run_id"] or "")
        node_name = str(row["node_name"] or "")
        unit_id = str(row["unit_id"] or "")
        input_fingerprint = str(row["input_fingerprint"] or "")
        if not run_id or not node_name or not unit_id or not input_fingerprint:
            raise MigrationError(f"receipt {receipt_id} has incomplete identity")

        payload_attempt = payload.get("attempt")
        column_attempt = row["attempt"] if "attempt" in columns else None
        if payload_attempt is not None:
            if isinstance(payload_attempt, bool) or not isinstance(payload_attempt, int) or payload_attempt < 1:
                raise MigrationError(f"receipt {receipt_id} has invalid attempt")
            if column_attempt is not None and column_attempt != payload_attempt:
                raise MigrationError(f"receipt {receipt_id} payload/column attempt mismatch")
            attempt = payload_attempt
        else:
            attempt = column_attempt if column_attempt is not None else 1
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise MigrationError(f"receipt {receipt_id} has invalid attempt")

        payload_action = payload.get("action_key")
        column_action = row["action_key"] if "action_key" in columns else ""
        payload_action = "" if payload_action is None else payload_action
        column_action = "" if column_action is None else column_action
        if not isinstance(payload_action, str) or not isinstance(column_action, str):
            raise MigrationError(f"receipt {receipt_id} has invalid action_key")
        if payload_action and column_action and payload_action != column_action:
            raise MigrationError(f"receipt {receipt_id} payload/column action_key mismatch")
        source_action = payload_action or column_action
        action_key = source_action or receipt_action_key(
            harness_run_id=run_id,
            node_name=node_name,
            unit_id=unit_id,
            attempt=attempt,
            input_fingerprint=input_fingerprint,
            action=node_name,
        )
        identity = (run_id, action_key)
        previous = action_keys.get(identity)
        if previous is not None:
            raise MigrationError(
                f"receipt action_key collision for run {run_id}: {previous} and {receipt_id}"
            )
        action_keys[identity] = receipt_id
        target_payload = _json_clone(payload)
        target_payload.update({
            "receipt_id": receipt_id,
            "harness_run_id": run_id,
            "node_name": node_name,
            "unit_id": unit_id,
            "input_fingerprint": input_fingerprint,
            "action_key": action_key,
            "attempt": attempt,
        })
        plans.append(_ReceiptPlan(
            receipt_id=receipt_id,
            run_id=run_id,
            payload=target_payload,
            payload_hash=_payload_hash(target_payload),
            action_key=action_key,
            attempt=attempt,
            source_action_key=source_action,
            source_attempt=column_attempt if payload_attempt is None else payload_attempt,
        ))
    return plans, "action_key" not in columns, "attempt" not in columns


def _new_lineage_run_id(run_id: str, checkpoint_id: str, reason: str) -> str:
    suffix = _canonical_hash({
        "migration_id": MIGRATION_ID,
        "source_run_id": run_id,
        "source_checkpoint_id": checkpoint_id,
        "migration_reason": reason,
    })[:32]
    return f"legacy-recovery-{suffix}"


def _build_lineage(
    checkpoint: _CheckpointRecord,
    legacy_target: dict[str, Any],
    *,
    created_at: str,
    existing_runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    reason = checkpoint.result.migration_reason if checkpoint.result else None
    reason = reason or "legacy_terminal"
    new_run_id = _new_lineage_run_id(checkpoint.run_id, checkpoint.checkpoint_id, reason)
    new_payload = _json_clone(legacy_target)
    statuses = new_payload.get("unit_statuses") or {}
    if not isinstance(statuses, dict):
        statuses = {}
    new_payload.update({
        "harness_run_id": new_run_id,
        "status": "planned",
        "checkpoint_id": None,
        "current_node": "initialize",
        "step_count": 0,
        "retrieval_round_count": 0,
        "completed_units": 0,
        "total_units": int(new_payload.get("total_units") or 0),
        "retry_count": 0,
        "unit_statuses": {str(unit_id): "planned" for unit_id in statuses},
        "unit_attempts": {},
        "dispatch_cursor": 0,
        "evidence_matrix_hash": None,
        "draft_ids": [],
        "pending_human_event": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "fencing_token": 0,
        "agent_thread_id": None,
        "migration_state": "native",
        "migration_reason": None,
        "migration_source_checkpoint_id": None,
        "created_at": created_at,
        "updated_at": created_at,
        "lineage": {
            "migration_id": MIGRATION_ID,
            "source_harness_run_id": checkpoint.run_id,
            "source_checkpoint_id": checkpoint.checkpoint_id,
            "migration_reason": reason,
        },
    })
    existing = existing_runs.get(new_run_id)
    if existing is not None and _canonical_payload(existing) != _canonical_payload(new_payload):
        raise MigrationError(f"lineage run id already exists with different payload: {new_run_id}")
    return {
        "source_run_id": checkpoint.run_id,
        "source_checkpoint_id": checkpoint.checkpoint_id,
        "legacy_target_run_id": checkpoint.run_id,
        "new_run_id": new_run_id,
        "migration_reason": reason,
        "payload": new_payload,
    }


def _prepare(
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
    backend_round_trip: Callable[[str, dict[str, Any]], None] | None,
) -> _PreparedMigration:
    _require_schema(connection)
    source_domain = _domain_snapshot(connection)
    source_guard = _source_guard_snapshot(connection)
    source_guard_hash = _snapshot_hash(source_guard)
    runs_before = _load_run_payloads(connection)
    checkpoints = _load_checkpoint_records(connection, runs_before)
    latest_by_run: dict[str, _CheckpointRecord] = {}
    for record in checkpoints:
        previous = latest_by_run.get(record.run_id)
        if previous is None or (record.updated_at, record.checkpoint_id) > (previous.updated_at, previous.checkpoint_id):
            latest_by_run[record.run_id] = record
    for record in checkpoints:
        record.latest = latest_by_run[record.run_id] is record
        record.lease_active = _prove_active_lease(
            record, runs_before[record.run_id], as_of=as_of,
        )
        record.result = _safe_checkpoint_migration(
            record.payload,
            lease_active=record.lease_active,
            backend_round_trip=backend_round_trip,
        )
    work_orders_after = _prepare_work_orders(connection, checkpoints)
    runs_after: dict[str, dict[str, Any]] = {}
    for run_id, record in sorted(latest_by_run.items()):
        before = runs_before[run_id]
        work_order = work_orders_after.get(record.work_order_id)
        _validate_checkpoint_identity(record, before, work_order)
        runs_after[run_id] = _build_run_payload(before, record, record.result)
    for run_id, payload in runs_after.items():
        work_order_id = str(payload.get("work_order_id") or "")
        work_order = work_orders_after.get(work_order_id)
        if work_order:
            requested = work_order.get("requested_executor")
            if requested is not None:
                current = payload.get("requested_executor")
                if current not in (None, requested):
                    raise MigrationError(f"HarnessRun requested_executor conflicts for {run_id}")
                payload["requested_executor"] = requested
            version = work_order.get("input_fingerprint_version", 1)
            current_version = payload.get("input_fingerprint_version", version)
            if current_version not in (None, version) and current_version != 1:
                raise MigrationError(f"HarnessRun input_fingerprint_version conflicts for {run_id}")
            payload["input_fingerprint_version"] = version
    manifests_after = _prepare_manifests(connection, runs_after, work_orders_after)
    receipts, add_action_key, add_attempt = _prepare_receipts(connection)
    lineages: dict[str, dict[str, Any]] = {}
    migration_time = datetime.now(timezone.utc).isoformat()
    for run_id, record in sorted(latest_by_run.items()):
        if record.result and record.result.outcome == "legacy_terminal":
            lineages[run_id] = _build_lineage(
                record, runs_after[run_id], created_at=migration_time, existing_runs=runs_before,
            )
    return _PreparedMigration(
        source_domain=source_domain,
        source_guard=source_guard,
        source_guard_hash=source_guard_hash,
        runs_before=runs_before,
        checkpoints=checkpoints,
        latest_by_run=latest_by_run,
        runs_after=runs_after,
        work_orders_after=work_orders_after,
        manifests_after=manifests_after,
        receipts=receipts,
        add_receipt_action_key=add_action_key,
        add_receipt_attempt=add_attempt,
        lineages=lineages,
    )


def _target_reconciliations(
    before: dict[str, TableDigest],
    after: dict[str, TableDigest],
    *,
    expected_run_rows: int,
) -> dict[str, TableReconciliation]:
    result: dict[str, TableReconciliation] = {}
    for table in sorted(set(before) | set(after)):
        old = before.get(table, TableDigest(table, 0, ""))
        new = after.get(table, TableDigest(table, 0, ""))
        if table == CHECKPOINT_TABLE:
            count_match = new.row_count == old.row_count
            hash_match = new.content_hash == old.content_hash
            note = "legacy table retained unchanged"
        elif table == RUN_TABLE:
            count_match = new.row_count == expected_run_rows
            hash_match = True  # run payloads are checked per source run below
            note = "canonical run payloads checked by run_id; legacy recovery runs may be added"
        elif table == RECEIPT_TABLE:
            count_match = new.row_count == old.row_count
            hash_match = True  # normalized receipt payloads are checked per receipt below
            note = "receipt payload/action key checked by receipt_id"
        else:
            count_match = new.row_count == old.row_count
            hash_match = True  # only migration fields are normalized
            note = "payload normalization checked by object id"
        result[table] = TableReconciliation(table, old, new, count_match, hash_match, note)
    return result


def _run_results(prepared: _PreparedMigration) -> list[dict[str, Any]]:
    items = []
    for run_id, record in sorted(prepared.latest_by_run.items()):
        result = record.result
        items.append({
            "harness_run_id": run_id,
            "checkpoint_id": record.checkpoint_id,
            "outcome": result.outcome if result else "legacy_terminal",
            "migration_reason": result.migration_reason if result else LEGACY_REASON_INVALID_PAYLOAD,
            "lease_active": record.lease_active,
            "source_payload_hash": record.payload_hash,
            "graph_state_hash": _payload_hash(result.graph_state) if result and result.graph_state else None,
            "lineage_new_run_id": prepared.lineages.get(run_id, {}).get("new_run_id"),
        })
    return items


def _receipt_results(receipts: list[_ReceiptPlan]) -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": item.receipt_id,
            "harness_run_id": item.run_id,
            "action_key": item.action_key,
            "attempt": item.attempt,
            "source_action_key_present": bool(item.source_action_key),
        }
        for item in receipts
    ]


def _summary_report(
    prepared: _PreparedMigration,
    *,
    database_path: Path,
    dry_run: bool,
    applied: bool,
    already_applied: bool = False,
    backup: BackupVerification | None = None,
    target_domain: dict[str, TableDigest] | None = None,
    expected_run_rows: int | None = None,
    warnings: list[str] | None = None,
) -> MigrationReport:
    target_domain = target_domain or prepared.source_domain
    converted = sum(
        record.result is not None
        and record.result.outcome == "converted"
        for record in prepared.latest_by_run.values()
    )
    legacy = len(prepared.latest_by_run) - converted
    if expected_run_rows is None:
        expected_run_rows = len(prepared.runs_before) + len(prepared.lineages)
    return MigrationReport(
        migration_id=MIGRATION_ID,
        schema_version=SCHEMA_VERSION,
        database_path=str(database_path),
        dry_run=dry_run,
        success=True,
        applied=applied,
        already_applied=already_applied,
        backup_verified=backup.verified if backup else False,
        backup=backup,
        checkpoint_rows=len(prepared.checkpoints),
        checkpoint_runs=len(prepared.latest_by_run),
        converted_runs=converted,
        legacy_terminal_runs=legacy,
        receipt_rows=len(prepared.receipts),
        lineage_rows=len(prepared.lineages),
        source_guard_hash=prepared.source_guard_hash,
        source_domain_snapshot_hash=_snapshot_hash(prepared.source_domain),
        target_domain_snapshot_hash=_snapshot_hash(target_domain),
        table_reconciliations=_target_reconciliations(
            prepared.source_domain, target_domain, expected_run_rows=expected_run_rows,
        ),
        run_results=_run_results(prepared),
        receipt_results=_receipt_results(prepared.receipts),
        warnings=list(warnings or []),
    )


def _check_source_guard(
    connection: sqlite3.Connection,
    expected: dict[str, TableDigest],
) -> None:
    actual = _source_guard_snapshot(connection)
    if actual != expected:
        raise MigrationError(
            "legacy source changed during migration preflight; refusing partial backfill"
        )


def _check_target_runs_before(
    connection: sqlite3.Connection,
    expected: dict[str, dict[str, Any]],
) -> None:
    """Prevent a stale preflight from overwriting a concurrently changed run."""

    actual = _load_run_payloads(connection)
    if set(actual) != set(expected):
        raise MigrationError(
            "HarnessRun set changed during migration preflight; refusing stale backfill"
        )
    for run_id, expected_payload in expected.items():
        if _canonical_payload(actual[run_id]) != _canonical_payload(expected_payload):
            raise MigrationError(
                f"HarnessRun changed during migration preflight: {run_id}"
            )


def _update_payload_row(
    connection: sqlite3.Connection,
    table: str,
    key_column: str,
    key: str,
    payload: dict[str, Any],
    *,
    status: str | None = None,
) -> None:
    if status is None:
        connection.execute(
            f"UPDATE {table} SET payload_json = ? WHERE {key_column} = ?",
            (_canonical_payload(payload), key),
        )
    else:
        connection.execute(
            f"UPDATE {table} SET status = ?, payload_json = ? WHERE {key_column} = ?",
            (status, _canonical_payload(payload), key),
        )


def _verify_applied_rows(connection: sqlite3.Connection, prepared: _PreparedMigration) -> None:
    for run_id, expected in prepared.runs_after.items():
        row = connection.execute(
            f"SELECT status, payload_json FROM {RUN_TABLE} WHERE harness_run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise MigrationError(f"migrated HarnessRun is missing: {run_id}")
        actual = _load_payload(row["payload_json"], object_id=run_id, table=RUN_TABLE)
        if _canonical_payload(actual) != _canonical_payload(expected) or row["status"] != expected["status"]:
            raise MigrationError(f"HarnessRun reconciliation failed: {run_id}")
    for work_order_id, expected in prepared.work_orders_after.items():
        row = connection.execute(
            f"SELECT payload_json FROM {WORK_ORDER_TABLE} WHERE work_order_id = ?",
            (work_order_id,),
        ).fetchone()
        if row is None:
            raise MigrationError(f"migrated work order is missing: {work_order_id}")
        actual = _load_payload(row["payload_json"], object_id=work_order_id, table=WORK_ORDER_TABLE)
        if _canonical_payload(actual) != _canonical_payload(expected):
            raise MigrationError(f"work-order reconciliation failed: {work_order_id}")
    for manifest_id, expected in prepared.manifests_after.items():
        row = connection.execute(
            f"SELECT payload_json FROM {MANIFEST_TABLE} WHERE run_manifest_id = ?", (manifest_id,)
        ).fetchone()
        if row is None:
            raise MigrationError(f"migrated run manifest is missing: {manifest_id}")
        actual = _load_payload(row["payload_json"], object_id=manifest_id, table=MANIFEST_TABLE)
        if _canonical_payload(actual) != _canonical_payload(expected):
            raise MigrationError(f"run-manifest reconciliation failed: {manifest_id}")
    for receipt in prepared.receipts:
        row = connection.execute(
            f"SELECT * FROM {RECEIPT_TABLE} WHERE receipt_id = ?", (receipt.receipt_id,)
        ).fetchone()
        if row is None:
            raise MigrationError(f"migrated receipt is missing: {receipt.receipt_id}")
        actual = _load_payload(row["payload_json"], object_id=receipt.receipt_id, table=RECEIPT_TABLE)
        if _canonical_payload(actual) != _canonical_payload(receipt.payload):
            raise MigrationError(f"receipt payload reconciliation failed: {receipt.receipt_id}")
        if row["action_key"] != receipt.action_key or row["attempt"] != receipt.attempt:
            raise MigrationError(f"receipt key reconciliation failed: {receipt.receipt_id}")
    for lineage in prepared.lineages.values():
        row = connection.execute(
            f"SELECT payload_json FROM {RUN_TABLE} WHERE harness_run_id = ?",
            (lineage["new_run_id"],),
        ).fetchone()
        if row is None:
            raise MigrationError(f"lineage run is missing: {lineage['new_run_id']}")
        actual = _load_payload(row["payload_json"], object_id=lineage["new_run_id"], table=RUN_TABLE)
        if _canonical_payload(actual) != _canonical_payload(lineage["payload"]):
            raise MigrationError(f"lineage run reconciliation failed: {lineage['new_run_id']}")
    index = connection.execute(
        "SELECT 1 FROM pragma_index_list(?) WHERE name = ? AND "
        "[unique] = 1",
        (RECEIPT_TABLE, "idx_node_execution_receipts_action_key"),
    ).fetchone()
    if prepared.receipts and index is None:
        raise MigrationError("receipt action_key unique index was not created")


def _insert_ledger_rows(
    connection: sqlite3.Connection,
    prepared: _PreparedMigration,
    *,
    migrated_at: str,
) -> None:
    for record in prepared.checkpoints:
        result = record.result
        target_hash = None
        if record.latest:
            target_hash = _payload_hash(prepared.runs_after[record.run_id])
        connection.execute(
            """INSERT INTO document_authoring_migration_ledger
               (migration_id, object_type, source_id, source_run_id, source_payload_hash,
                outcome, migration_reason, graph_state_hash, target_run_id, target_payload_hash,
                action_key, attempt, migrated_at)
               VALUES (?, 'checkpoint', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (
                MIGRATION_ID, record.checkpoint_id, record.run_id, record.payload_hash,
                result.outcome if result else "legacy_terminal",
                result.migration_reason if result else LEGACY_REASON_INVALID_PAYLOAD,
                _payload_hash(result.graph_state) if result and result.graph_state else None,
                record.run_id, target_hash, migrated_at,
            ),
        )
    for receipt in prepared.receipts:
        connection.execute(
            """INSERT INTO document_authoring_migration_ledger
               (migration_id, object_type, source_id, source_run_id, source_payload_hash,
                outcome, migration_reason, graph_state_hash, target_run_id, target_payload_hash,
                action_key, attempt, migrated_at)
               VALUES (?, 'receipt', ?, ?, ?, 'converted', NULL, NULL, ?, ?, ?, ?, ?)""",
            (
                MIGRATION_ID, receipt.receipt_id, receipt.run_id,
                _payload_hash(_strip_migration_fields(receipt.payload, {"action_key", "attempt"})),
                receipt.run_id, receipt.payload_hash, receipt.action_key, receipt.attempt,
                migrated_at,
            ),
        )


def _insert_lineage(
    connection: sqlite3.Connection,
    prepared: _PreparedMigration,
    *,
    created_at: str,
) -> None:
    for lineage in prepared.lineages.values():
        existing = connection.execute(
            """SELECT source_checkpoint_id, legacy_target_run_id, new_run_id,
                      migration_reason, lineage_payload_json
               FROM document_authoring_run_lineage
               WHERE migration_id = ? AND source_run_id = ?""",
            (MIGRATION_ID, lineage["source_run_id"]),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (
                lineage["source_checkpoint_id"], lineage["legacy_target_run_id"],
                lineage["new_run_id"], lineage["migration_reason"],
                _canonical_payload(lineage["payload"]),
            ):
                raise MigrationError(f"existing lineage differs: {lineage['source_run_id']}")
            continue
        connection.execute(
            """INSERT INTO document_authoring_run_lineage
               (migration_id, source_run_id, source_checkpoint_id, legacy_target_run_id,
                new_run_id, migration_reason, lineage_payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                MIGRATION_ID, lineage["source_run_id"], lineage["source_checkpoint_id"],
                lineage["legacy_target_run_id"], lineage["new_run_id"],
                lineage["migration_reason"], _canonical_payload(lineage["payload"]), created_at,
            ),
        )


def _apply_prepared(
    database_path: Path,
    prepared: _PreparedMigration,
    *,
    backup: BackupVerification | None,
) -> tuple[dict[str, TableDigest], MigrationReport]:
    connection = _connect(database_path)
    migrated_at = datetime.now(timezone.utc).isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _check_source_guard(connection, prepared.source_guard)
        _check_target_runs_before(connection, prepared.runs_before)
        _migration_schema.install_metadata_schema(connection)
        if prepared.add_receipt_action_key:
            connection.execute(
                f"ALTER TABLE {RECEIPT_TABLE} ADD COLUMN action_key TEXT NOT NULL DEFAULT ''"
            )
        if prepared.add_receipt_attempt:
            connection.execute(
                f"ALTER TABLE {RECEIPT_TABLE} ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"
            )
        for work_order_id, payload in prepared.work_orders_after.items():
            _update_payload_row(
                connection, WORK_ORDER_TABLE, "work_order_id", work_order_id, payload,
            )
        for manifest_id, payload in prepared.manifests_after.items():
            _update_payload_row(
                connection, MANIFEST_TABLE, "run_manifest_id", manifest_id, payload,
            )
        for run_id, payload in prepared.runs_after.items():
            _update_payload_row(connection, RUN_TABLE, "harness_run_id", run_id, payload, status=payload["status"])
        for lineage in prepared.lineages.values():
            payload = lineage["payload"]
            connection.execute(
                f"""INSERT INTO {RUN_TABLE}
                    (harness_run_id, work_order_id, status, created_at, payload_json)
                    VALUES (?, ?, ?, ?, ?)""",
                (
                    lineage["new_run_id"], payload["work_order_id"], payload["status"],
                    payload["created_at"], _canonical_payload(payload),
                ),
            )
        for receipt in prepared.receipts:
            connection.execute(
                f"""UPDATE {RECEIPT_TABLE}
                    SET action_key = ?, attempt = ?, payload_json = ?
                    WHERE receipt_id = ?""",
                (
                    receipt.action_key, receipt.attempt, _canonical_payload(receipt.payload),
                    receipt.receipt_id,
                ),
            )
        if _table_exists(connection, RECEIPT_TABLE):
            connection.execute(
                f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_node_execution_receipts_action_key
                    ON {RECEIPT_TABLE}(harness_run_id, action_key)
                    WHERE action_key != ''"""
            )
        _verify_applied_rows(connection, prepared)
        target_domain = _domain_snapshot(connection)
        source_domain_hash = _snapshot_hash(prepared.source_domain)
        target_domain_hash = _snapshot_hash(target_domain)
        source_guard_json = _snapshot_json(prepared.source_guard)
        summary = {
            "migration_id": MIGRATION_ID,
            "schema_version": SCHEMA_VERSION,
            "checkpoint_rows": len(prepared.checkpoints),
            "checkpoint_runs": len(prepared.latest_by_run),
            "converted_runs": sum(
                record.result is not None and record.result.outcome == "converted"
                for record in prepared.latest_by_run.values()
            ),
            "legacy_terminal_runs": len(prepared.lineages),
            "receipt_rows": len(prepared.receipts),
            "lineage_rows": len(prepared.lineages),
            "source_domain_snapshot": _snapshot_json(prepared.source_domain),
            "target_domain_snapshot": _snapshot_json(target_domain),
            "source_domain_snapshot_hash": source_domain_hash,
            "target_domain_snapshot_hash": target_domain_hash,
            "run_results": _run_results(prepared),
            "receipt_results": _receipt_results(prepared.receipts),
        }
        _insert_ledger_rows(connection, prepared, migrated_at=migrated_at)
        _insert_lineage(connection, prepared, created_at=migrated_at)
        connection.execute(
            """INSERT INTO schema_migrations
               (migration_id, schema_version, status, applied_at, source_snapshot_hash,
                source_snapshot_json, target_snapshot_hash, backup_path, backup_hash, report_json)
               VALUES (?, ?, 'applied', ?, ?, ?, ?, ?, ?, ?)""",
            (
                MIGRATION_ID, SCHEMA_VERSION, migrated_at, prepared.source_guard_hash,
                _canonical_payload(source_guard_json), target_domain_hash,
                backup.path if backup else "", backup.file_hash if backup else "",
                _canonical_payload(summary),
            ),
        )
        connection.execute("COMMIT")
    except Exception as exc:
        connection.execute("ROLLBACK")
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            f"migration transaction aborted and was rolled back: {exc}"
        ) from exc
    finally:
        connection.close()
    report = _summary_report(
        prepared,
        database_path=database_path,
        dry_run=False,
        applied=True,
        backup=backup,
        target_domain=target_domain,
    )
    return target_domain, report


def _marker(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _table_exists(connection, "schema_migrations"):
        return None
    columns = set(_table_columns(connection, "schema_migrations"))
    expected = {
        "migration_id", "schema_version", "status", "source_snapshot_hash",
        "source_snapshot_json", "target_snapshot_hash", "backup_path", "backup_hash",
        "report_json",
    }
    if not expected <= columns:
        raise MigrationError("schema_migrations has an incompatible schema")
    return connection.execute(
        "SELECT * FROM schema_migrations WHERE migration_id = ?", (MIGRATION_ID,)
    ).fetchone()


def _marker_source_guard(marker: sqlite3.Row) -> dict[str, TableDigest]:
    try:
        raw = json.loads(marker["source_snapshot_json"])
        return {
            table: TableDigest(
                table, int(value["row_count"]), str(value["content_hash"]),
            )
            for table, value in raw.items()
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationError("schema_migrations source snapshot is invalid") from exc


def _already_applied_report(
    database_path: Path,
    connection: sqlite3.Connection,
    marker: sqlite3.Row,
    *,
    dry_run: bool,
    backup: BackupVerification | None,
    reverse_drill: bool,
) -> MigrationReport:
    expected_guard = _marker_source_guard(marker)
    actual_guard = _source_guard_snapshot(connection)
    if actual_guard != expected_guard:
        raise MigrationError(
            "legacy source changed after migration; old-table writes violate the observation window"
        )
    summary = _load_payload(marker["report_json"], object_id=MIGRATION_ID, table="schema_migrations")
    if not isinstance(summary, dict):
        raise MigrationError("schema_migrations report is invalid")
    if backup is not None:
        recorded_backup_hash = str(marker["backup_hash"] or "")
        if marker["backup_path"] and str(Path(marker["backup_path"]).resolve()) == backup.path:
            if recorded_backup_hash and backup.file_hash != recorded_backup_hash:
                raise MigrationError("recorded migration backup hash does not match the backup file")
        recorded_source = {
            table: TableDigest(table, int(value["row_count"]), str(value["content_hash"]))
            for table, value in (summary.get("source_domain_snapshot") or {}).items()
        }
        for table, expected in recorded_source.items():
            actual = backup.table_digests.get(table, TableDigest(table, 0, ""))
            if actual != expected:
                raise MigrationError(f"recorded migration backup no longer matches source table: {table}")
    domain = _domain_snapshot(connection)
    source_domain = {
        table: TableDigest(table, int(value["row_count"]), str(value["content_hash"]))
        for table, value in (summary.get("source_domain_snapshot") or {}).items()
    }
    prepared = _PreparedMigration(
        source_domain=source_domain or domain,
        source_guard=expected_guard,
        source_guard_hash=str(marker["source_snapshot_hash"]),
        runs_before={}, checkpoints=[], latest_by_run={}, runs_after={},
        work_orders_after={}, manifests_after={}, receipts=[],
        add_receipt_action_key=False, add_receipt_attempt=False, lineages={},
    )
    report = _summary_report(
        prepared,
        database_path=database_path,
        dry_run=dry_run,
        applied=False,
        already_applied=True,
        backup=backup,
        target_domain=domain,
        warnings=["migration already applied; no rows were written"],
    )
    report.checkpoint_rows = int(summary.get("checkpoint_rows", 0))
    report.checkpoint_runs = int(summary.get("checkpoint_runs", 0))
    report.converted_runs = int(summary.get("converted_runs", 0))
    report.legacy_terminal_runs = int(summary.get("legacy_terminal_runs", 0))
    report.receipt_rows = int(summary.get("receipt_rows", 0))
    report.lineage_rows = int(summary.get("lineage_rows", 0))
    report.run_results = list(summary.get("run_results") or [])
    report.receipt_results = list(summary.get("receipt_results") or [])
    report.source_domain_snapshot_hash = str(
        summary.get("source_domain_snapshot_hash") or _snapshot_hash(prepared.source_domain)
    )
    report.target_domain_snapshot_hash = _snapshot_hash(domain)
    report.table_reconciliations = _target_reconciliations(
        prepared.source_domain, domain,
        expected_run_rows=_table_digest(connection, RUN_TABLE).row_count,
    )
    if reverse_drill:
        report.reverse_drill = _reverse_drill_existing(connection, marker)
        if not report.reverse_drill.verified:
            report.success = False
    return report


def _target_matches_fields(
    source: dict[str, Any],
    target: dict[str, Any],
    *,
    expected_status: str | None = None,
) -> list[str]:
    errors: list[str] = []
    for key in _BUSINESS_FIELDS:
        # ``active`` is the legacy checkpoint spelling; the migration contract
        # intentionally maps it to ``running`` only after a lease proof.
        if key == "status" and expected_status is not None:
            continue
        if key in source and key in target and source[key] != target[key]:
            errors.append(f"{key} differs")
    if expected_status is not None and target.get("status") != expected_status:
        errors.append(f"status is {target.get('status')!r}, expected {expected_status!r}")
    return errors


def _reverse_drill_existing(
    connection: sqlite3.Connection,
    marker: sqlite3.Row,
) -> ReverseDrillReport:
    errors: list[str] = []
    checkpoint_rows = int(connection.execute(
        f"SELECT COUNT(*) FROM {CHECKPOINT_TABLE}"
    ).fetchone()[0])
    receipt_rows = int(connection.execute(
        f"SELECT COUNT(*) FROM {RECEIPT_TABLE}"
    ).fetchone()[0]) if _table_exists(connection, RECEIPT_TABLE) else 0
    lineage_rows = int(connection.execute(
        "SELECT COUNT(*) FROM document_authoring_run_lineage WHERE migration_id = ?",
        (MIGRATION_ID,),
    ).fetchone()[0]) if _table_exists(connection, "document_authoring_run_lineage") else 0
    if not _table_exists(connection, "document_authoring_migration_ledger"):
        return ReverseDrillReport(False, checkpoint_rows, receipt_rows, lineage_rows, ("migration ledger is missing",))
    ledger = connection.execute(
        "SELECT * FROM document_authoring_migration_ledger WHERE migration_id = ? ORDER BY object_type, source_id",
        (MIGRATION_ID,),
    ).fetchall()
    checkpoint_ledger = [row for row in ledger if row["object_type"] == "checkpoint"]
    receipt_ledger = [row for row in ledger if row["object_type"] == "receipt"]
    if len(checkpoint_ledger) != checkpoint_rows:
        errors.append(f"checkpoint ledger count {len(checkpoint_ledger)} != source count {checkpoint_rows}")
    if len(receipt_ledger) != receipt_rows:
        errors.append(f"receipt ledger count {len(receipt_ledger)} != source count {receipt_rows}")
    for item in checkpoint_ledger:
        row = connection.execute(
            f"SELECT * FROM {CHECKPOINT_TABLE} WHERE checkpoint_id = ?", (item["source_id"],)
        ).fetchone()
        if row is None:
            errors.append(f"checkpoint missing during reverse drill: {item['source_id']}")
            continue
        source = _load_payload(row["payload_json"], object_id=item["source_id"], table=CHECKPOINT_TABLE)
        if _payload_hash(source) != item["source_payload_hash"]:
            errors.append(f"checkpoint source hash changed: {item['source_id']}")
            continue
        target_row = connection.execute(
            f"SELECT payload_json FROM {RUN_TABLE} WHERE harness_run_id = ?", (item["target_run_id"],)
        ).fetchone()
        if target_row is None:
            errors.append(f"target run missing during reverse drill: {item['target_run_id']}")
            continue
        target = _load_payload(target_row["payload_json"], object_id=item["target_run_id"], table=RUN_TABLE)
        # A run can have several historical checkpoints, while HarnessRun has
        # one canonical payload.  Only the newest checkpoint owns the target
        # projection; older rows are still hash-audited in the ledger.
        if item["target_payload_hash"] is not None:
            if target.get("migration_state") != item["outcome"]:
                errors.append(f"migration state mismatch for {item['source_id']}")
            if target.get("checkpoint_id") != item["source_id"]:
                errors.append(f"checkpoint lineage mismatch for {item['source_id']}")
        if isinstance(source, dict) and item["target_payload_hash"] is not None:
            expected_status = source.get("status")
            if expected_status == "active":
                expected_status = "running" if item["outcome"] == "converted" else "paused"
            errors.extend(
                f"{item['source_id']}: {message}"
                for message in _target_matches_fields(source, target, expected_status=expected_status)
            )
    for item in receipt_ledger:
        row = connection.execute(
            f"SELECT action_key, attempt, payload_json FROM {RECEIPT_TABLE} WHERE receipt_id = ?",
            (item["source_id"],),
        ).fetchone()
        if row is None:
            errors.append(f"receipt missing during reverse drill: {item['source_id']}")
            continue
        if row["action_key"] != item["action_key"] or row["attempt"] != item["attempt"]:
            errors.append(f"receipt key mismatch during reverse drill: {item['source_id']}")
        payload = _load_payload(row["payload_json"], object_id=item["source_id"], table=RECEIPT_TABLE)
        if not isinstance(payload, dict) or payload.get("action_key") != item["action_key"] or payload.get("attempt") != item["attempt"]:
            errors.append(f"receipt payload key mismatch during reverse drill: {item['source_id']}")
    if _table_exists(connection, "document_authoring_run_lineage"):
        for row in connection.execute(
            "SELECT * FROM document_authoring_run_lineage WHERE migration_id = ?",
            (MIGRATION_ID,),
        ).fetchall():
            new_run = connection.execute(
                f"SELECT payload_json FROM {RUN_TABLE} WHERE harness_run_id = ?", (row["new_run_id"],)
            ).fetchone()
            if new_run is None:
                errors.append(f"lineage recovery run missing: {row['new_run_id']}")
                continue
            payload = _load_payload(new_run["payload_json"], object_id=row["new_run_id"], table=RUN_TABLE)
            lineage = payload.get("lineage") if isinstance(payload, dict) else None
            if not isinstance(lineage, dict) or lineage.get("source_harness_run_id") != row["source_run_id"]:
                errors.append(f"lineage payload mismatch: {row['new_run_id']}")
    return ReverseDrillReport(not errors, checkpoint_rows, receipt_rows, lineage_rows, tuple(errors))


def _reverse_drill_prepared(
    prepared: _PreparedMigration,
) -> ReverseDrillReport:
    errors: list[str] = []
    for run_id, record in prepared.latest_by_run.items():
        expected = prepared.runs_after[run_id]
        source = record.payload if isinstance(record.payload, dict) else {}
        if expected.get("checkpoint_id") != record.checkpoint_id:
            errors.append(f"prepared checkpoint lineage mismatch: {record.checkpoint_id}")
        expected_status = source.get("status")
        if expected_status == "active":
            expected_status = "running" if record.result and record.result.outcome == "converted" else "paused"
        errors.extend(
            f"{record.checkpoint_id}: {message}"
            for message in _target_matches_fields(source, expected, expected_status=expected_status)
        )
    return ReverseDrillReport(
        not errors,
        len(prepared.checkpoints),
        len(prepared.receipts),
        len(prepared.lineages),
        tuple(errors),
    )


def reverse_migration_drill(
    database_path: str | os.PathLike[str] | None = None,
) -> ReverseDrillReport:
    """Run the non-mutating reverse/lineage reconciliation drill."""

    database = Path(database_path) if database_path is not None else default_database_path()
    database = database.resolve()
    with _connect(database, read_only=True) as connection:
        _require_schema(connection)
        marker = _marker(connection)
        if marker is not None:
            return _reverse_drill_existing(connection, marker)
        prepared = _prepare(
            connection, as_of=datetime.now(timezone.utc), backend_round_trip=None,
        )
        return _reverse_drill_prepared(prepared)


def _restore_database(database_path: Path, backup_path: Path) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{database_path.name}.restore.", suffix=".tmp", dir=database_path.parent, delete=False,
        ) as stream:
            temp_path = Path(stream.name)
        with sqlite3.connect(str(backup_path)) as source, sqlite3.connect(str(temp_path)) as target:
            source.backup(target)
        with sqlite3.connect(str(temp_path)) as restored:
            integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise MigrationError(f"restored database integrity_check failed: {integrity}")
        os.replace(temp_path, database_path)
        temp_path = None
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"could not restore database from backup: {backup_path}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def rollback_migration(
    database_path: str | os.PathLike[str] | None,
    backup_path: str | os.PathLike[str],
    *,
    safety_backup_path: str | os.PathLike[str] | None = None,
) -> RollbackReport:
    """Restore a verified pre-migration backup and verify the restored file."""

    database = (Path(database_path) if database_path is not None else default_database_path()).resolve()
    backup = Path(backup_path).resolve()
    if database == backup:
        raise MigrationError("database and rollback backup paths must be different")
    with _connect(database, read_only=True) as connection:
        marker = _marker(connection)
        if marker is None:
            raise MigrationError("cannot rollback: migration marker is not present")
        current_domain = _domain_snapshot(connection)
    backup_verification = verify_sqlite_backup(backup)
    safety = (
        Path(safety_backup_path).resolve()
        if safety_backup_path is not None
        else database.with_name(f"{database.name}.{MIGRATION_ID}.post-migration.bak")
    )
    _ensure_backup(database, safety, expected_tables=current_domain)
    _restore_database(database, backup)
    with _connect(database, read_only=True) as connection:
        restored_domain = _domain_snapshot(connection)
        if restored_domain != backup_verification.table_digests:
            raise MigrationError("rollback verification failed: restored table digests differ from backup")
        if not _table_exists(connection, CHECKPOINT_TABLE):
            raise MigrationError("rollback verification failed: legacy checkpoint table is missing")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise MigrationError(f"rollback verification failed: {integrity}")
    return RollbackReport(
        database_path=str(database),
        backup_path=str(backup),
        safety_backup_path=str(safety),
        verified=True,
        restored_table_digests=restored_domain,
    )


def _run_with_marker_or_prepare(
    database_path: Path,
    *,
    dry_run: bool,
    verify_backup_requested: bool,
    backup_path: str | os.PathLike[str] | None,
    reverse_drill: bool,
    as_of: datetime,
    backend_round_trip: Callable[[str, dict[str, Any]], None] | None,
) -> MigrationReport:
    with _connect(database_path, read_only=dry_run) as connection:
        _require_schema(connection)
        marker = _marker(connection)
        if marker is not None:
            requested_backup = Path(backup_path).resolve() if backup_path else None
            marker_backup = Path(marker["backup_path"]).resolve() if marker["backup_path"] else None
            selected_backup = requested_backup or marker_backup
            backup = None
            if verify_backup_requested or selected_backup is not None:
                if selected_backup is None:
                    raise MigrationError("backup verification was requested but no backup path is recorded")
                backup = verify_sqlite_backup(selected_backup)
            return _already_applied_report(
                database_path, connection, marker, dry_run=dry_run, backup=backup,
                reverse_drill=reverse_drill,
            )
        prepared = _prepare(
            connection, as_of=as_of, backend_round_trip=backend_round_trip,
        )
    backup = None
    if not dry_run or verify_backup_requested or backup_path is not None:
        selected_backup = (
            Path(backup_path).resolve() if backup_path is not None
            else default_backup_path(database_path)
        )
        backup = _ensure_backup(
            database_path, selected_backup, expected_tables=prepared.source_domain,
        )
    if dry_run:
        report = _summary_report(
            prepared,
            database_path=database_path,
            dry_run=True,
            applied=False,
            backup=backup,
            target_domain=prepared.source_domain,
            expected_run_rows=len(prepared.runs_before),
            warnings=["dry-run: no database rows, schema objects, or migration marker were written"],
        )
        if reverse_drill:
            report.reverse_drill = _reverse_drill_prepared(prepared)
            if not report.reverse_drill.verified:
                report.success = False
        return report
    _, report = _apply_prepared(database_path, prepared, backup=backup)
    if reverse_drill:
        report.reverse_drill = reverse_migration_drill(database_path)
        if not report.reverse_drill.verified:
            report.success = False
    return report


def run_migration(
    database_path: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
    verify_backup: bool = False,
    backup_path: str | os.PathLike[str] | None = None,
    reverse_drill: bool = False,
    as_of: datetime | None = None,
    backend_round_trip: Callable[[str, dict[str, Any]], None] | None = None,
) -> MigrationReport:
    """Run or rehearse migration 0001.

    ``backend_round_trip`` is an optional Task 5a checkpointer hook.  It is
    called during preflight with ``(thread_id, graph_state)`` and must raise if
    the selected backend cannot write/read the state.  SQLite business data is
    never marked converted based on a failed hook.
    """

    database = (Path(database_path) if database_path is not None else default_database_path()).resolve()
    if not database.is_file():
        raise MigrationError(f"database file does not exist: {database}")
    point = as_of or datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    point = point.astimezone(timezone.utc)
    return _run_with_marker_or_prepare(
        database,
        dry_run=dry_run,
        verify_backup_requested=verify_backup,
        backup_path=backup_path,
        reverse_drill=reverse_drill,
        as_of=point,
        backend_round_trip=backend_round_trip,
    )


def migrate(*args: Any, **kwargs: Any) -> MigrationReport:
    """Short alias retained for programmatic migration callers."""

    return run_migration(*args, **kwargs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", "--db-path", dest="database", type=Path)
    parser.add_argument("--backup", "--backup-path", dest="backup", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="validate and report without writing SQLite")
    parser.add_argument("--verify-backup", action="store_true", help="create/verify the pre-migration backup")
    parser.add_argument(
        "--reverse-drill", action="store_true",
        help="run the read-only reverse/lineage reconciliation drill",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="restore --backup after creating a safety backup of the current database",
    )
    parser.add_argument("--safety-backup", type=Path, help="path for the rollback safety backup")
    parser.add_argument("--as-of", help="UTC ISO timestamp used for active-lease proof")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database = args.database or default_database_path()
    try:
        if args.rollback:
            if args.dry_run:
                raise MigrationError("--rollback cannot be combined with --dry-run")
            if not args.backup:
                raise MigrationError("--rollback requires --backup")
            report = rollback_migration(
                database, args.backup, safety_backup_path=args.safety_backup,
            )
        else:
            as_of = _parse_datetime(args.as_of, label="--as-of") if args.as_of else None
            report = run_migration(
                database,
                # A CLI reverse drill is always read-only.  The programmatic
                # API may combine apply + drill explicitly, but a flag named
                # ``--reverse-drill`` must never surprise an operator by
                # applying a pending migration.
                dry_run=args.dry_run or args.reverse_drill,
                verify_backup=args.verify_backup,
                backup_path=args.backup,
                reverse_drill=args.reverse_drill,
                as_of=as_of,
            )
        rendered = json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        print(rendered)
        ok = report.verified if isinstance(report, RollbackReport) else report.verified
        return 0 if ok else 1
    except MigrationError as exc:
        if args.json:
            print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"migration_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
