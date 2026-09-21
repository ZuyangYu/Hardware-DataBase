"""SQLite→PostgreSQL loader and verifier for the control plane (v3 §5.4).

The target schema is created by :mod:`schema` (execution core) and by this
module's generated DDL (remaining tables, using the same explicit type
rules).  Secondary indexes and foreign keys are created *after* the data load
so import order cannot fail on cycles and violations surface against intact
data.  Every copied table is verified with an order-insensitive content hash,
and the authority group gets execution-protocol checks (status set,
``turn_sequence`` coverage, idempotency keys, event continuity,
``conversation_execution_state`` coverage).

The loader requires an empty target: this is a stop-write maintenance-window
tool, not an online catch-up replicator.  A failed run is recovered by
dropping the target schema and re-running from the frozen snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import (
    CORE_FOREIGN_KEYS,
    CORE_IDENTITY_COLUMNS,
    PARTIAL_INDEX_OVERRIDES,
    SCHEMA_VERSION,
    TURN_STATUSES,
    create_core_indexes,
    generated_identity_column,
    generated_table_ddl,
    is_core_table,
    pg_ident,
    pg_type_for,
)

try:  # psycopg is an optional runtime dependency for the loader only
    from psycopg.errors import ForeignKeyViolation
    from psycopg.rows import tuple_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - exercised by callers without psycopg
    tuple_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]
    ForeignKeyViolation = None  # type: ignore[assignment]


_DERIVED_TABLE_PATTERNS = ("_fts", "_trigram")
_TRANSIENT_TABLES = {"worker_instances"}


@dataclass
class TableReport:
    table: str
    status: str = ""
    source_rows: int = 0
    target_rows: int = 0
    copied: int = 0
    source_hash: str = ""
    target_hash: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LoadReport:
    source: str
    source_sha256: str
    schema_version: str
    dry_run: bool
    tables: list[TableReport] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tables"] = [table.to_dict() for table in self.tables]
        return payload


def _is_derived_table(table: str) -> bool:
    name = str(table).lower()
    return any(pattern in name for pattern in _DERIVED_TABLE_PATTERNS)


def _is_transient_table(table: str) -> bool:
    return str(table).lower() in _TRANSIENT_TABLES


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_table_names(source: sqlite3.Connection) -> list[str]:
    rows = source.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _sqlite_count(source: sqlite3.Connection, table: str) -> int:
    return int(source.execute(f"SELECT COUNT(*) FROM {pg_ident(table)}").fetchone()[0])


def _sqlite_columns(source: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        {
            "name": str(row["name"]),
            "type": str(row["type"] or ""),
            "notnull": int(row["notnull"] or 0),
            "pk": int(row["pk"] or 0),
            "dflt_value": row["dflt_value"],
        }
        for row in source.execute(f"PRAGMA table_info({pg_ident(table)})")
    ]


def _parse_timestamp(value: Any, *, table: str, column: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{table}.{column}: empty timestamp")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{table}.{column}: invalid timestamp {value!r}") from exc
    else:
        raise ValueError(f"{table}.{column}: unsupported timestamp {type(value).__name__}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_bool(value: Any, *, table: str, column: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes"}:
        return True
    if text in {"0", "false", "f", "no", ""}:
        return False
    raise ValueError(f"{table}.{column}: invalid boolean {value!r}")


def canonical_value(target_type: str, value: Any, *, table: str = "", column: str = "") -> Any:
    """Normalize a cell for cross-database hash comparison."""
    if value is None:
        return None
    if target_type == "JSONB":
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            parsed = json.loads(text)
        else:
            parsed = value
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False, default=str)
    if target_type == "BOOLEAN":
        return _as_bool(value, table=table, column=column)
    if target_type == "TIMESTAMPTZ":
        if isinstance(value, str) and not value.strip():
            return None  # SQLite '' sentinel == "not set"
        parsed = _parse_timestamp(value, table=table, column=column)
        return parsed.isoformat(timespec="microseconds")
    if target_type == "BIGINT":
        return int(value)
    if target_type == "BYTEA":
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def adapt_value(target_type: str, value: Any, *, table: str, column: str) -> Any:
    """Fit a SQLite cell to the target PostgreSQL column type."""
    if value is None:
        return None
    if target_type == "JSONB":
        if isinstance(value, str) and not value.strip():
            return None
        parsed = json.loads(value) if isinstance(value, str) else value
        return Jsonb(parsed)
    if target_type == "BOOLEAN":
        return _as_bool(value, table=table, column=column)
    if target_type == "TIMESTAMPTZ":
        if isinstance(value, str) and not value.strip():
            return None  # SQLite '' sentinel == "not set"
        return _parse_timestamp(value, table=table, column=column)
    if target_type == "BIGINT":
        return int(value)
    if target_type == "BYTEA":
        return bytes(value)
    return str(value)


def _content_hash(rows: list[tuple], types: list[str], table: str, columns: list[str]) -> str:
    digests = []
    for row in rows:
        payload = json.dumps(
            [
                canonical_value(target_type, value, table=table, column=column)
                for target_type, value, column in zip(types, row, columns)
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digests.append(hashlib.sha256(payload.encode("utf-8", "surrogateescape")).hexdigest())
    digests.sort()
    return hashlib.sha256("\n".join(digests).encode("ascii")).hexdigest()


def _target_types(table: str, columns: list[dict[str, Any]]) -> list[str]:
    return [pg_type_for(table, str(column["name"]), str(column["type"])) for column in columns]


def _target_has_rows(conn, table: str) -> bool:
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {pg_ident(table)} LIMIT 1)")
        return bool(cursor.fetchone()[0])


def _fetch_pg_rows(conn, table: str, columns: list[str]) -> list[tuple]:
    column_sql = ", ".join(pg_ident(column) for column in columns)
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(f"SELECT {column_sql} FROM {pg_ident(table)}")
        return cursor.fetchall()


def _copy_table(
    source: sqlite3.Connection,
    conn,
    table: str,
    columns: list[str],
    types: list[str],
    batch_size: int,
) -> int:
    column_sql = ", ".join(pg_ident(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    statement = f"INSERT INTO {pg_ident(table)} ({column_sql}) VALUES ({placeholders})"
    rows = source.execute(f"SELECT {column_sql} FROM {pg_ident(table)}").fetchall()
    copied = 0
    with conn.cursor(row_factory=tuple_row) as cursor:
        for start in range(0, len(rows), max(1, batch_size)):
            batch = [
                tuple(
                    adapt_value(target_type, row[column], table=table, column=column)
                    for target_type, column in zip(types, columns)
                )
                for row in rows[start : start + batch_size]
            ]
            if not batch:
                continue
            cursor.executemany(statement, batch)
            copied += len(batch)
    return copied


def _generated_index_ddl(source: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    statements: list[str] = []
    notes: list[str] = []
    for row in source.execute(f"PRAGMA index_list({pg_ident(table)})"):
        name = str(row["name"])
        origin = str(row["origin"] or "c")
        partial = bool(row["partial"])
        unique = bool(row["unique"])
        if origin == "pk":
            continue
        if partial:
            override = PARTIAL_INDEX_OVERRIDES.get(name)
            if override:
                statements.append(override)
            else:
                notes.append(f"index {name} skipped (partial; no override registered)")
            continue
        columns = [
            str(info["name"])
            for info in source.execute(f"PRAGMA index_info({pg_ident(name)})")
        ]
        if any(not column for column in columns):
            notes.append(f"index {name} skipped (expression)")
            continue
        if origin == "u":
            name = "ux_" + table.lower() + "_" + "_".join(column.lower() for column in columns)
        prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
        statements.append(
            f"{prefix} IF NOT EXISTS {pg_ident(name)} ON {pg_ident(table)} "
            f"({', '.join(pg_ident(column) for column in columns)})"
        )
    return statements, notes


def _generated_foreign_key_ddl(
    source: sqlite3.Connection, table: str, available: set[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    constraints: list[tuple[str, str]] = []
    notes: list[str] = []
    for index, row in enumerate(source.execute(f"PRAGMA foreign_key_list({pg_ident(table)})")):
        reference = str(row["table"])
        column = str(row["from"])
        target_column = str(row["to"] or "id")
        if reference not in available:
            notes.append(f"foreign key {column}->{reference} skipped (table not loaded)")
            continue
        if reference == table:
            notes.append(f"foreign key {column}->{reference} skipped (self reference)")
            continue
        name = f"fk_{table}_{column}_{index}"
        ddl = f"FOREIGN KEY ({pg_ident(column)}) REFERENCES {pg_ident(reference)} ({pg_ident(target_column)})"
        on_delete = str(row["on_delete"] or "NO ACTION").upper()
        on_update = str(row["on_update"] or "NO ACTION").upper()
        if on_delete != "NO ACTION":
            ddl += f" ON DELETE {on_delete}"
        if on_update != "NO ACTION":
            ddl += f" ON UPDATE {on_update}"
        constraints.append((name, ddl))
    return constraints, notes


def _add_constraint_if_missing(
    conn,
    table: str,
    name: str,
    constraint_sql: str,
    *,
    notes: list[str] | None = None,
) -> bool:
    """Add a constraint unless it already exists.

    SQLite does not enforce foreign keys by default, so the source can contain
    orphan rows.  A failing FK is skipped and reported (the migration tool
    never deletes or rewrites source data), instead of aborting the whole load.
    """
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = to_regclass(%s)",
            (name, table),
        )
        if cursor.fetchone() is not None:
            return False
        try:
            cursor.execute(
                f"ALTER TABLE {pg_ident(table)} ADD CONSTRAINT {pg_ident(name)} {constraint_sql}"
            )
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is dirty source data
            if ForeignKeyViolation is None or not isinstance(exc, ForeignKeyViolation):
                raise
            if notes is not None:
                notes.append(
                    f"foreign key {name} skipped (source rows violate the constraint)"
                )
            return False
    return True


def _table_notes(report: LoadReport, table: str) -> list[str]:
    for entry in report.tables:
        if entry.table == table:
            return entry.notes
    return []


def _advance_identity(conn, table: str, column: str) -> int | None:
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(f"SELECT MAX({pg_ident(column)}) FROM {pg_ident(table)}")
        maximum = cursor.fetchone()[0]
        if maximum is None:
            return None
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence(%s, %s), %s, true)",
            (table, column, int(maximum)),
        )
    return int(maximum)


def _backfill_execution_state(conn) -> int:
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            """
            INSERT INTO conversation_execution_state (session_id, updated_at)
            SELECT s.id, s.updated_at FROM chat_sessions s
            WHERE NOT EXISTS (
                SELECT 1 FROM conversation_execution_state e WHERE e.session_id = s.id
            )
            """
        )
        return int(cursor.rowcount or 0)


def _scalar(conn, sql: str, params: tuple = ()) -> int:
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(sql, params)
        return int(cursor.fetchone()[0] or 0)


def _authority_checks(conn, tables: set[str]) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {}
    failures: list[str] = []
    if "chat_turns" in tables:
        with conn.cursor(row_factory=tuple_row) as cursor:
            cursor.execute("SELECT status, COUNT(*) FROM chat_turns GROUP BY status")
            status_counts = {str(status): int(count) for status, count in cursor.fetchall()}
        checks["turn_statuses"] = status_counts
        unknown = sorted(set(status_counts) - set(TURN_STATUSES))
        if unknown:
            failures.append(f"chat_turns has unknown statuses: {unknown}")
        null_sequences = _scalar(
            conn, "SELECT COUNT(*) FROM chat_turns WHERE turn_sequence IS NULL"
        )
        checks["turn_sequence_nulls"] = null_sequences
        if null_sequences:
            failures.append(f"chat_turns has {null_sequences} NULL turn_sequence rows")
        duplicates = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT session_id, client_request_id FROM chat_turns
                WHERE client_request_id IS NOT NULL
                GROUP BY session_id, client_request_id HAVING COUNT(*) > 1
            ) duplicates
            """,
        )
        checks["duplicate_idempotency_keys"] = duplicates
        if duplicates:
            failures.append(f"chat_turns has {duplicates} duplicate idempotency keys")
    if "chat_turn_events" in tables and "chat_turns" in tables:
        mismatched = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM chat_turns t
            WHERE t.last_event_seq <> COALESCE(
                (SELECT COUNT(*) FROM chat_turn_events e WHERE e.turn_id = t.id), 0
            )
            """,
        )
        checks["event_continuity_violations"] = mismatched
        if mismatched:
            failures.append(f"chat_turn_events continuity broken for {mismatched} turns")
    if "conversation_execution_state" in tables and "chat_sessions" in tables:
        missing = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM chat_sessions s
            WHERE NOT EXISTS (
                SELECT 1 FROM conversation_execution_state e WHERE e.session_id = s.id
            )
            """,
        )
        checks["execution_state_missing_sessions"] = missing
        if missing:
            failures.append(f"conversation_execution_state missing for {missing} sessions")
        orphan = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM conversation_execution_state e
            WHERE e.active_turn_id <> '' AND NOT EXISTS (
                SELECT 1 FROM chat_turns t
                WHERE t.id = e.active_turn_id AND t.session_id = e.session_id
            )
            """,
        )
        checks["execution_state_orphan_active_turns"] = orphan
        if orphan:
            failures.append(f"conversation_execution_state has {orphan} orphan active turns")
    return checks, failures


def load_sqlite_snapshot(
    source_path: str | Path,
    conn,
    *,
    tables: Iterable[str] | None = None,
    batch_size: int = 500,
    backfill_execution_state: bool = True,
    dry_run: bool = False,
    apply_target_schema: bool = True,
    add_foreign_keys: bool = True,
) -> LoadReport:
    """Copy a frozen SQLite snapshot into an empty PostgreSQL schema and verify it."""
    path = Path(source_path)
    report = LoadReport(
        source=str(path),
        source_sha256=_sha256_file(path),
        schema_version=SCHEMA_VERSION,
        dry_run=bool(dry_run),
    )
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        available = _sqlite_table_names(source)
        requested = set(str(table) for table in (tables or ()))
        selected = [
            table
            for table in available
            if not _is_derived_table(table)
            and not _is_transient_table(table)
            and (not requested or table in requested)
        ]
        if requested:
            missing = sorted(requested - set(selected))
            for table in missing:
                report.tables.append(
                    TableReport(table=table, status="skipped", notes=["not present in source"])
                )
        columns_by_table = {table: _sqlite_columns(source, table) for table in selected}

        if dry_run:
            for table in selected:
                report.tables.append(
                    TableReport(
                        table=table,
                        status="dry_run",
                        source_rows=_sqlite_count(source, table),
                    )
                )
            report.checks = {"mode": "dry_run", "table_count": len(selected)}
            report.ok = True
            return report

        if apply_target_schema:
            from .schema import apply_schema
            from .schema_version import record_schema_version

            apply_schema(conn)
            record_schema_version(conn, source_fingerprint=report.source_sha256)
            for table in selected:
                if is_core_table(table) or not columns_by_table[table]:
                    continue
                conn.execute(generated_table_ddl(table, columns_by_table[table]))

        loaded: set[str] = set()
        for table in selected:
            columns = [str(column["name"]) for column in columns_by_table[table]]
            entry = TableReport(table=table)
            if not columns:
                entry.status = "skipped"
                entry.notes.append("no columns")
                report.tables.append(entry)
                continue
            types = _target_types(table, columns_by_table[table])
            if _target_has_rows(conn, table):
                entry.status = "error"
                entry.error = "target table is not empty (stop-write window required)"
                report.failures.append(f"{table}: {entry.error}")
                report.tables.append(entry)
                continue
            entry.source_rows = _sqlite_count(source, table)
            try:
                entry.copied = _copy_table(source, conn, table, columns, types, batch_size)
            except Exception as exc:  # noqa: BLE001 - reported per table
                entry.status = "error"
                entry.error = f"{type(exc).__name__}: {exc}"
                report.failures.append(f"{table}: copy failed: {entry.error}")
                report.tables.append(entry)
                continue
            try:
                source_rows = source.execute(
                    f"SELECT {', '.join(pg_ident(column) for column in columns)} FROM {pg_ident(table)}"
                ).fetchall()
                target_rows = _fetch_pg_rows(conn, table, columns)
                entry.target_rows = len(target_rows)
                entry.source_hash = _content_hash(source_rows, types, table, columns)
                entry.target_hash = _content_hash(target_rows, types, table, columns)
                if entry.source_rows != entry.target_rows:
                    entry.status = "count_mismatch"
                elif entry.source_hash != entry.target_hash:
                    entry.status = "hash_mismatch"
                else:
                    entry.status = "ok"
                    loaded.add(table)
            except Exception as exc:  # noqa: BLE001 - reported per table
                entry.status = "verify_error"
                entry.error = f"{type(exc).__name__}: {exc}"
            if entry.status != "ok":
                report.failures.append(f"{table}: {entry.status}")
            report.tables.append(entry)

        if "chat_sessions" in loaded and "conversation_execution_state" in loaded:
            if backfill_execution_state:
                report.checks["execution_state_backfilled"] = _backfill_execution_state(conn)

        if loaded:
            for table in selected:
                if table not in loaded or is_core_table(table):
                    continue
                statements, notes = _generated_index_ddl(source, table)
                for statement in statements:
                    conn.execute(statement)
                if notes:
                    for entry in report.tables:
                        if entry.table == table:
                            entry.notes.extend(notes)
                            break
            create_core_indexes(conn)

        if add_foreign_keys:
            for name, table, constraint_sql in CORE_FOREIGN_KEYS:
                if table not in loaded:
                    continue
                reference = constraint_sql.split("REFERENCES")[1].split("(")[0].strip().strip('"')
                if reference not in loaded:
                    _table_notes(report, table).append(
                        f"foreign key {name} skipped ({reference} not loaded)"
                    )
                    continue
                _add_constraint_if_missing(
                    conn, table, name, constraint_sql, notes=_table_notes(report, table)
                )
            for table in selected:
                if table not in loaded or is_core_table(table):
                    continue
                constraints, notes = _generated_foreign_key_ddl(source, table, loaded)
                for name, ddl in constraints:
                    _add_constraint_if_missing(
                        conn, table, name, ddl, notes=_table_notes(report, table)
                    )
                _table_notes(report, table).extend(notes)

        identity_columns = [
            (table, column)
            for table, column in CORE_IDENTITY_COLUMNS
            if table in loaded
        ]
        for table in selected:
            if table not in loaded or is_core_table(table):
                continue
            identity = generated_identity_column(table, columns_by_table[table])
            if identity is not None:
                identity_columns.append(identity)
        for table, column in identity_columns:
            maximum = _advance_identity(conn, table, column)
            report.checks[f"identity:{table}.{column}"] = maximum

        checks, failures = _authority_checks(conn, loaded)
        report.checks.update(checks)
        report.failures.extend(failures)
    finally:
        source.close()

    report.ok = not report.failures and all(
        entry.status in {"ok", "skipped", "dry_run"} for entry in report.tables
    )
    return report
