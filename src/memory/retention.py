"""Retention and privacy cleanup for the memory control plane.

Retention is intentionally conservative: configured retention can expire all
user memories, while project memories are automatically processed only after
they have already entered a non-active lifecycle state.  The operation uses
the same redaction/fence path as consent revocation, so an old Store object is
not left searchable while the database mutation is committed.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from src.memory.catalog import ensure_memory_schema, utc_now
from src.memory.jobs import _invalidate_memory_in_transaction


def _retention_days(value: str | int | float | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


def expire_memory_records(
    db_path: str,
    *,
    retention_days: str | int | float | None = None,
    now: datetime | None = None,
    limit: int = 500,
) -> int:
    """Redact/fence records whose configured retention window has elapsed."""

    days = _retention_days(retention_days)
    if days is None:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    cutoff_text = cutoff.isoformat()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_memory_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """SELECT memory_id, scope, status FROM memory_records
                   WHERE updated_at < ?
                     AND (scope = 'user' OR status IN ('deleted', 'rejected', 'superseded', 'provenance_missing'))
                   ORDER BY updated_at, memory_id
                   LIMIT ?""",
                (cutoff_text, max(1, min(int(limit), 5_000))),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE memory_sources SET source_valid = 0, invalidated_at = COALESCE(invalidated_at, ?) WHERE memory_id = ?",
                    (utc_now(), row["memory_id"]),
                )
                _invalidate_memory_in_transaction(
                    conn,
                    row["memory_id"],
                    reason="retention_expired",
                )
                conn.execute(
                    """INSERT INTO memory_audit_events
                       (audit_event_id, memory_id, event_type, metadata_json, created_at)
                       VALUES (lower(hex(randomblob(16))), ?, 'retention_expired', ?, ?)""",
                    (row["memory_id"], '{"reason":"retention_expired"}', utc_now()),
                )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise


__all__ = ["expire_memory_records"]
