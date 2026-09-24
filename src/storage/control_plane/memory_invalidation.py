"""PostgreSQL twin of the memory invalidation primitives (v3 §5).

``edit_message`` and ``clear_session`` must fence derived memory before new
text becomes visible, in the same transaction that rewrites the raw chat row.
This module mirrors ``src/memory/jobs.py`` classification and outbox semantics
so the conversation facade can call either backend.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from psycopg.types.json import Jsonb


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _namespace(payload: Any) -> Any:
    """Return the stored namespace value (JSONB decodes to list, TEXT to JSON)."""
    if isinstance(payload, (dict, list)):
        return payload
    try:
        parsed = json.loads(payload or "[]")
    except (TypeError, ValueError):
        return []
    return parsed


def _source_delete_metric(value: int, *, kind: str) -> None:
    try:
        from src.observability.metrics import counter

        counter("hdb.memory.source_delete", value=value, attributes={"kind": kind})
    except Exception:
        pass


def apply_source_invalidation(
    conn, memory_id: str, *, ref_id: str, reason: str
) -> None:
    """Fence one memory record after its backing sources became invalid."""
    from src.memory.jobs import utc_now
    from src.memory.schemas import MEMORY_SCHEMA_VERSION, content_hash

    now = str(utc_now())
    record = conn.execute(
        "SELECT * FROM memory_records WHERE memory_id = %s", (str(memory_id),)
    ).fetchone()
    if record is None:
        return
    valid_count = conn.execute(
        "SELECT COUNT(*) AS count FROM memory_sources"
        " WHERE memory_id = %s AND source_valid",
        (str(memory_id),),
    ).fetchone()["count"]
    if str(record["scope"]) == "user":
        next_status = "deleted"
    elif str(record["status"]) == "verified":
        next_status = "provenance_missing"
    else:
        next_status = "needs_rebuild" if int(valid_count or 0) else "deleted"
    conn.execute(
        """
        UPDATE memory_records
        SET status = %s,
            deleted_at = CASE WHEN %s = 'deleted' THEN %s::timestamptz
                              ELSE deleted_at END,
            updated_at = %s::timestamptz
        WHERE memory_id = %s
        """,
        (next_status, next_status, now, now, str(memory_id)),
    )
    if str(record["scope"]) == "user":
        redacted_hash = content_hash(
            {}, schema_version=record["schema_version"] or MEMORY_SCHEMA_VERSION
        )
        conn.execute(
            """
            UPDATE memory_records
            SET content_json = '{}'::jsonb, content_hash = %s, title = '已删除记忆',
                subject = NULL, memory_type = 'context'
            WHERE memory_id = %s
            """,
            (redacted_hash, str(memory_id)),
        )
        conn.execute(
            "UPDATE memory_revisions SET before_content_json = NULL,"
            " after_content_json = NULL WHERE memory_id = %s",
            (str(memory_id),),
        )
        conn.execute(
            """
            UPDATE memory_reflection_runs
            SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL,
                status = 'failed'
            WHERE run_id IN (SELECT run_id FROM memory_run_items WHERE memory_id = %s)
            """,
            (str(memory_id),),
        )
    projections = conn.execute(
        "SELECT * FROM memory_projections WHERE memory_id = %s AND retired_at IS NULL",
        (str(memory_id),),
    ).fetchall()
    for projection in projections:
        next_fence = int(projection["fence_version"]) + 1
        conn.execute(
            """
            UPDATE memory_projections
            SET active = FALSE, manager_writable = FALSE, fence_version = %s,
                retired_at = %s::timestamptz
            WHERE projection_id = %s AND retired_at IS NULL AND fence_version = %s
            """,
            (
                next_fence,
                now,
                projection["projection_id"],
                int(projection["fence_version"]),
            ),
        )
        payload = {
            "projection_id": projection["projection_id"],
            "namespace": _namespace(projection["namespace_json"]),
            "store_key": projection["store_key"],
            "reason": reason,
        }
        conn.execute(
            """
            INSERT INTO memory_deletion_outbox
                (operation_id, memory_id, source_or_consent_id, operation,
                 expected_revision, expected_fence, idempotency_key, payload_json,
                 created_at)
            VALUES (%s, %s, %s, 'delete_projection', %s, %s, %s, %s, %s::timestamptz)
            ON CONFLICT DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                str(memory_id),
                ref_id,
                int(record["current_revision"]),
                next_fence,
                f"delete:{memory_id}:{projection['projection_id']}:{next_fence}",
                Jsonb(payload),
                now,
            ),
        )
    conn.execute(
        """
        INSERT INTO memory_audit_events
            (audit_event_id, memory_id, event_type, metadata_json, created_at)
        VALUES (%s, %s, 'source_invalidated', %s, %s::timestamptz)
        """,
        (
            str(uuid.uuid4()),
            str(memory_id),
            Jsonb(
                {
                    "ref": ref_id,
                    "reason": reason,
                    "remaining_valid_sources": int(valid_count or 0),
                }
            ),
            now,
        ),
    )


def invalidate_session_memory(conn, session_id: int, *, reason: str) -> int:
    """Invalidate memory derived from a session before raw chat deletion."""
    from src.memory.jobs import utc_now

    now = str(utc_now())
    consent_rows = conn.execute(
        """
        SELECT consent_event_id FROM memory_consent_events
        WHERE session_id = %s AND revoked_at IS NULL
        """,
        (int(session_id),),
    ).fetchall()
    consent_ids = [str(row["consent_event_id"]) for row in consent_rows]
    conn.execute(
        """
        UPDATE memory_reflection_runs
        SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL,
            status = 'failed'
        WHERE job_id IN (SELECT job_id FROM memory_jobs WHERE session_id = %s)
        """,
        (int(session_id),),
    )
    if consent_ids:
        conn.execute(
            """
            UPDATE memory_consent_events SET revoked_at = %s::timestamptz
            WHERE consent_event_id = ANY(%s) AND revoked_at IS NULL
            """,
            (now, consent_ids),
        )
        for consent_id in consent_ids:
            conn.execute(
                """
                INSERT INTO memory_audit_events
                    (audit_event_id, operation_id, event_type, metadata_json, created_at)
                VALUES (%s, %s, 'consent_revoked', %s, %s::timestamptz)
                """,
                (
                    str(uuid.uuid4()),
                    consent_id,
                    Jsonb({"reason": reason, "session_id": int(session_id)}),
                    now,
                ),
            )
        conn.execute(
            """
            UPDATE memory_reflection_runs
            SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL,
                status = 'failed'
            WHERE consent_event_id = ANY(%s)
            """,
            (consent_ids,),
        )
    conn.execute(
        """
        UPDATE memory_jobs
        SET status = 'cancelled', generation = generation + 1, last_error = %s,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
        WHERE session_id = %s AND status IN ('pending', 'retrying', 'running')
        """,
        (reason, int(session_id)),
    )
    source_rows = conn.execute(
        """
        SELECT DISTINCT memory_id FROM memory_sources
        WHERE session_id = %s AND source_valid
        """,
        (int(session_id),),
    ).fetchall()
    if not source_rows:
        return 0
    conn.execute(
        """
        UPDATE memory_sources SET source_valid = FALSE,
            invalidated_at = %s::timestamptz
        WHERE session_id = %s AND source_valid
        """,
        (now, int(session_id)),
    )
    affected = 0
    for row in source_rows:
        apply_source_invalidation(
            conn,
            str(row["memory_id"]),
            ref_id=f"session:{int(session_id)}",
            reason=reason,
        )
        affected += 1
    _source_delete_metric(affected, kind="session")
    return affected


def invalidate_message_memory(
    conn, session_id: int, message_id: int, *, reason: str
) -> int:
    """Invalidate memory sourced from one edited/redacted message."""
    from src.memory.jobs import utc_now

    ref_id = f"message:{int(session_id)}:{int(message_id)}"
    now = str(utc_now())
    conn.execute(
        """
        UPDATE memory_jobs
        SET status = 'cancelled', generation = generation + 1, last_error = %s,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
        WHERE session_id = %s AND status IN ('pending', 'retrying', 'running')
        """,
        (reason, int(session_id)),
    )
    conn.execute(
        """
        UPDATE memory_reflection_runs
        SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL,
            status = 'failed'
        WHERE status IN ('prepared', 'output_persisted')
          AND job_id IN (SELECT job_id FROM memory_jobs WHERE session_id = %s)
        """,
        (int(session_id),),
    )
    source_rows = conn.execute(
        """
        SELECT DISTINCT memory_id FROM memory_sources
        WHERE session_id = %s AND message_id = %s AND source_valid
        """,
        (int(session_id), int(message_id)),
    ).fetchall()
    if not source_rows:
        return 0
    conn.execute(
        """
        UPDATE memory_sources SET source_valid = FALSE, invalidated_at = %s::timestamptz
        WHERE session_id = %s AND message_id = %s AND source_valid
        """,
        (now, int(session_id), int(message_id)),
    )
    affected = 0
    for row in source_rows:
        apply_source_invalidation(
            conn, str(row["memory_id"]), ref_id=ref_id, reason=reason
        )
        affected += 1
    _source_delete_metric(affected, kind="message")
    return affected


def expire_memory_records_postgres(
    conn,
    *,
    retention_days: int,
    now=None,
    limit: int = 500,
) -> int:
    """PostgreSQL twin of the SQLite retention sweep (v3 §5.1).

    Selects records past the configured retention window, invalidates their
    sources and queues projection deletions using the same classification and
    fence paths as consent revocation.  The caller owns the transaction.
    """
    from datetime import datetime, timedelta, timezone

    from src.memory.jobs import utc_now

    days = int(retention_days)
    if days <= 0:
        return 0
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT memory_id, scope, status FROM memory_records
        WHERE updated_at < %s
          AND (scope = 'user'
               OR status IN ('deleted', 'rejected', 'superseded', 'provenance_missing'))
        ORDER BY updated_at, memory_id
        LIMIT %s
        """,
        (cutoff, max(1, min(int(limit), 5_000))),
    ).fetchall()
    now_text = str(utc_now())
    for row in rows:
        conn.execute(
            """
            UPDATE memory_sources SET source_valid = FALSE,
                invalidated_at = COALESCE(invalidated_at, %s::timestamptz)
            WHERE memory_id = %s
            """,
            (now_text, str(row["memory_id"])),
        )
        apply_source_invalidation(
            conn, str(row["memory_id"]), ref_id="retention", reason="retention_expired"
        )
        conn.execute(
            """
            INSERT INTO memory_audit_events
                (audit_event_id, memory_id, event_type, metadata_json, created_at)
            VALUES (%s, %s, 'retention_expired', %s, %s::timestamptz)
            """,
            (
                str(uuid.uuid4()),
                str(row["memory_id"]),
                Jsonb({"reason": "retention_expired"}),
                now_text,
            ),
        )
    return len(rows)
