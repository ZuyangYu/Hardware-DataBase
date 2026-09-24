"""Schema version bookkeeping for the control plane (v3 §5.2).

Startup and readiness paths call :func:`assert_schema_compatible`; the
migration loader calls :func:`record_schema_version` after applying the
versioned schema.  Applications never run implicit DDL for these tables.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .schema import SCHEMA_TABLE, SCHEMA_VERSION


def _row_value(row) -> str:
    if row is None:
        raise RuntimeError(
            "control plane schema is not initialized; run the schema migration first"
        )
    if isinstance(row, dict):
        return str(row.get("value") or "")
    return str(row[0] or "")


def record_schema_version(conn, *, source_fingerprint: str = "") -> None:
    """Upsert the schema version row (and optional source fingerprint)."""
    now = datetime.now(timezone.utc)
    entries = [("schema_version", SCHEMA_VERSION), ("applied_at", now.isoformat())]
    if source_fingerprint:
        entries.append(("source_fingerprint", str(source_fingerprint)))
    for key, value in entries:
        conn.execute(
            f"""
            INSERT INTO {SCHEMA_TABLE} (key, value, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            (key, value, now),
        )


def assert_schema_compatible(conn) -> str:
    """Fail closed unless the target schema version matches this code."""
    try:
        row = conn.execute(
            f"SELECT value FROM {SCHEMA_TABLE} WHERE key = 'schema_version'"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - normalized as a readiness failure
        raise RuntimeError(f"control plane schema is not initialized: {exc}") from exc
    stored = _row_value(row)
    if stored != SCHEMA_VERSION:
        raise RuntimeError(
            "control plane schema version mismatch: "
            f"stored={stored!r} expected={SCHEMA_VERSION!r}"
        )
    return stored
