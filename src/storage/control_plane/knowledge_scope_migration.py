"""Additive R3 scope migration shared by the production CLI and SQLite init."""

from __future__ import annotations

SQLITE_COLUMNS = {
    "chat_sessions": {"knowledge_selection_json": "TEXT NOT NULL DEFAULT '{}'", "knowledge_selection_hash": "TEXT NOT NULL DEFAULT ''"},
    "chat_turns": {"knowledge_scope_json": "TEXT NOT NULL DEFAULT '{}'", "knowledge_selection_hash": "TEXT NOT NULL DEFAULT ''", "knowledge_request_fingerprint": "TEXT NOT NULL DEFAULT ''"},
}


def inspect_sqlite(conn) -> dict:
    missing = []
    invalid = []
    for table, expected in SQLITE_COLUMNS.items():
        columns = {row[1]: row for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for name in expected:
            if name not in columns:
                missing.append(f"{table}.{name}")
            elif columns[name][2].upper() != "TEXT" or not columns[name][3]:
                invalid.append(f"{table}.{name}")
    return {"ready": not missing and not invalid, "missing": missing, "invalid": invalid}


def apply_sqlite(conn) -> dict:
    report = inspect_sqlite(conn)
    if report["invalid"]:
        raise RuntimeError("knowledge_scope_schema_type_mismatch")
    if report["ready"]:
        return {**report, "changed": False}
    for table, columns in SQLITE_COLUMNS.items():
        current = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if not current:
            raise RuntimeError("conversation_schema_missing")
        for name, declaration in columns.items():
            if name not in current:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')
    return {**inspect_sqlite(conn), "changed": True}


def inspect_postgres(conn) -> dict:
    rows = conn.execute("SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = current_schema() AND table_name IN ('chat_sessions','chat_turns')").fetchall()
    columns = {(row[0], row[1]): (row[2], row[3]) for row in rows}
    missing, invalid = [], []
    for table, expected in SQLITE_COLUMNS.items():
        for name in expected:
            key = (table, name)
            expected_type = "jsonb" if name.endswith("_json") else "text"
            if key not in columns:
                missing.append(f"{table}.{name}")
            elif columns[key] != (expected_type, "NO"):
                invalid.append(f"{table}.{name}")
    version_row = conn.execute("SELECT value FROM control_plane_schema WHERE key = 'schema_version'").fetchone()
    version = str(version_row[0]) if version_row else ""
    return {"ready": not missing and not invalid and version == "2", "missing": missing, "invalid": invalid, "schema_version": version}


def apply_postgres(conn) -> dict:
    from .schema_version import record_schema_version

    report = inspect_postgres(conn)
    if report["invalid"] or report["schema_version"] not in {"1", "2"}:
        raise RuntimeError("knowledge_scope_schema_incompatible")
    if report["ready"]:
        return {**report, "changed": False}
    for table, columns in SQLITE_COLUMNS.items():
        for name in columns:
            declaration = "JSONB NOT NULL DEFAULT '{}'::jsonb" if name.endswith("_json") else "TEXT NOT NULL DEFAULT ''"
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{name}" {declaration}')
    if report["schema_version"] != "2":
        record_schema_version(conn)
    return {**inspect_postgres(conn), "changed": True}
