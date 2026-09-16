"""Copy SQLite authority groups to PostgreSQL with validation (P1-002/P1-003).

Groups map to source databases (see the migration runbook):

- ``authority``:   storage/auth.db (identity, permissions, conversations,
                    authoring and memory authority ledger, export jobs)
- ``projection``:  storage/memory.db (rebuildable search projection)
- ``attachments``: storage/chat_attachments.db
- ``pipeline``:    storage/pipeline_documents.db
- ``compatibility``: storage/document_authoring_compatibility.sqlite

Safety: run with writes stopped for the selected group.  The script creates
missing tables, copies rows idempotently, then compares row counts and a
content hash per table.  It never deletes target rows and never writes to the
source.  Use ``--dry-run`` first.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import sys
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE = REPO_ROOT / "storage"

GROUP_DATABASES: dict[str, str] = {
    "authority": "auth.db",
    "projection": "memory.db",
    "attachments": "chat_attachments.db",
    "pipeline": "pipeline_documents.db",
    "compatibility": "document_authoring_compatibility.sqlite",
}

_INTEGER_TYPES = ("INT", "BIGINT", "SMALLINT", "TINYINT", "BOOLEAN")
_REAL_TYPES = ("REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL")
# FTS/trigram shadow tables are derived indexes, rebuilt on the target.
_DERIVED_TABLE_PATTERNS = ("_fts", "_trigram")
# Heartbeat/registry tables are rewritten continuously while services run;
# they are transient state (state-inventory §2) and must not block migration.
_TRANSIENT_TABLES = {"worker_instances"}


def _is_derived_table(table: str) -> bool:
    name = str(table).lower()
    return any(pattern in name for pattern in _DERIVED_TABLE_PATTERNS)


def _is_transient_table(table: str) -> bool:
    return str(table).lower() in _TRANSIENT_TABLES


def _pg_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _map_type(declared: str) -> str:
    value = str(declared or "").upper()
    if any(token in value for token in _INTEGER_TYPES):
        return "BIGINT"
    if any(token in value for token in _REAL_TYPES):
        return "DOUBLE PRECISION"
    if "BLOB" in value:
        return "BYTEA"
    return "TEXT"


def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f"PRAGMA table_info({_pg_ident(table)})"))


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _normalize_value(value: Any) -> Any:
    """Representation-normalize a cell so SQLite/PG hashes are comparable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, bool):
            return int(value)
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _content_hash(rows, columns: list[str]) -> str:
    """Order-insensitive multiset hash with normalized cell values."""
    row_digests = []
    for row in rows:
        payload = json.dumps(
            [_normalize_value(row[column]) for column in columns],
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        row_digests.append(
            hashlib.sha256(payload.encode("utf-8", "surrogateescape")).hexdigest()
        )
    row_digests.sort()
    return hashlib.sha256("\n".join(row_digests).encode("ascii")).hexdigest()


def _sqlite_rows(conn: sqlite3.Connection, table: str, columns: list[str]):
    column_sql = ", ".join(_pg_ident(column) for column in columns)
    return list(conn.execute(f"SELECT {column_sql} FROM {_pg_ident(table)}"))


def _coerce_for_target(value: Any, target_type: str) -> Any:
    """Fit a SQLite cell to the mapped PostgreSQL column type.

    SQLite is dynamically typed: a column declared TEXT may physically hold
    BLOB bytes (e.g. the memory ``store.value`` JSON).  PostgreSQL enforces
    the declared type, so bytes must be decoded for TEXT targets instead of
    being adapted to ``bytea``.
    """
    if isinstance(value, bool):
        return int(value) if target_type != "TEXT" else ("true" if value else "false")
    if isinstance(value, (bytes, bytearray, memoryview)) and target_type != "BYTEA":
        return bytes(value).decode("utf-8", errors="replace")
    return value


def _ensure_pg_table(pg, table: str, columns: list[sqlite3.Row]) -> None:
    definitions = []
    primary_keys: list[tuple[int, str]] = []
    for column in columns:
        name = str(column["name"])
        definitions.append(f"{_pg_ident(name)} {_map_type(column['type'])}")
        if int(column["pk"] or 0) > 0:
            primary_keys.append((int(column["pk"]), name))
    primary_keys.sort()
    if primary_keys:
        definitions.append("PRIMARY KEY (" + ", ".join(_pg_ident(name) for _, name in primary_keys) + ")")
    pg.execute(f"CREATE TABLE IF NOT EXISTS {_pg_ident(table)} ({', '.join(definitions)})")


def _copy_table(pg, source: sqlite3.Connection, table: str, batch_size: int) -> dict[str, object]:
    columns_info = _table_columns(source, table)
    columns = [str(column["name"]) for column in columns_info]
    if not columns:
        return {"table": table, "status": "skipped", "reason": "no columns"}
    _ensure_pg_table(pg, table, columns_info)
    source_count = int(source.execute(f"SELECT COUNT(*) FROM {_pg_ident(table)}").fetchone()[0])
    rows = _sqlite_rows(source, table, columns)
    column_sql = ", ".join(_pg_ident(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    statement = (
        f"INSERT INTO {_pg_ident(table)} ({column_sql}) VALUES ({placeholders})"
        " ON CONFLICT DO NOTHING"
    )
    target_types = [_map_type(column["type"]) for column in columns_info]
    copied = 0
    for start in range(0, len(rows), max(1, batch_size)):
        batch = [
            tuple(
                _coerce_for_target(row[column], target_types[index])
                for index, column in enumerate(columns)
            )
            for row in rows[start : start + batch_size]
        ]
        if not batch:
            continue
        with pg.cursor() as cursor:
            cursor.executemany(statement, batch)
            copied += int(cursor.rowcount or 0)
    with pg.cursor() as cursor:
        cursor.execute(f"SELECT {column_sql} FROM {_pg_ident(table)}")
        target_rows = cursor.fetchall()
    target_count = len(target_rows)
    def _typed_rows(raw_rows):
        return [
            {
                column: _coerce_for_target(row[column], target_types[index])
                for index, column in enumerate(columns)
            }
            for row in raw_rows
        ]

    source_hash = _content_hash(_typed_rows(rows), columns)
    target_hash = _content_hash(_typed_rows(target_rows), columns)
    if source_count != target_count:
        status = "count_mismatch"
    elif source_hash != target_hash:
        status = "hash_mismatch"
    else:
        status = "ok"
    return {
        "table": table,
        "status": status,
        "source_rows": source_count,
        "target_rows": target_count,
        "copied": copied,
        "source_hash": source_hash,
        "target_hash": target_hash,
        "columns": columns,
    }


def _resolve_source(group: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return STORAGE / GROUP_DATABASES[group]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=sorted(GROUP_DATABASES), default="authority")
    parser.add_argument("--source", default="", help="override the source SQLite path")
    parser.add_argument("--dsn", required=True, help="target PostgreSQL connection URL")
    parser.add_argument("--dry-run", action="store_true", help="report source tables without copying")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    source_path = _resolve_source(args.group, args.source)
    report: dict[str, object] = {
        "group": args.group,
        "source": str(source_path),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "tables": [],
    }
    if not source_path.is_file():
        print(f"source database not found: {source_path}", file=sys.stderr)
        return 2

    import psycopg

    results: list[dict[str, object]] = []
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        tables = _table_names(source)
        if args.dry_run:
            for table in tables:
                if _is_derived_table(table):
                    results.append({"table": table, "status": "derived_skipped"})
                    continue
                if _is_transient_table(table):
                    results.append({"table": table, "status": "transient_skipped"})
                    continue
                count = int(source.execute(f"SELECT COUNT(*) FROM {_pg_ident(table)}").fetchone()[0])
                results.append({"table": table, "source_rows": count, "status": "dry_run"})
        else:
            from psycopg.rows import dict_row

            with psycopg.connect(args.dsn, autocommit=True, row_factory=dict_row) as pg:
                for table in tables:
                    if _is_derived_table(table):
                        results.append(
                            {
                                "table": table,
                                "status": "derived_skipped",
                                "reason": "FTS/trigram shadow index; rebuild on target",
                            }
                        )
                        continue
                    if _is_transient_table(table):
                        results.append(
                            {
                                "table": table,
                                "status": "transient_skipped",
                                "reason": "heartbeat registry; rewritten after cutover",
                            }
                        )
                        continue
                    try:
                        results.append(_copy_table(pg, source, table, args.batch_size))
                    except Exception as exc:
                        results.append({"table": table, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        source.close()

    report["tables"] = results
    report["ok"] = all(
        item.get("status") in {"ok", "dry_run", "derived_skipped", "transient_skipped"}
        for item in results
    )
    report["hash_mismatches"] = [
        str(item["table"]) for item in results if item.get("status") == "hash_mismatch"
    ]
    report["count_mismatches"] = [
        str(item["table"]) for item in results if item.get("status") == "count_mismatch"
    ]
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
