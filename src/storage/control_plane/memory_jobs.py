"""PostgreSQL memory-ledger primitives used inside conversation transactions (v3 §5).

The memory catalog stays in the AUTH_DB atomic group: ``complete_turn`` writes
the project-reflection outbox row in the same transaction as the answer, and
message/session invalidation must fence derived memory before new text becomes
visible.  This module ports the transaction-safe entry points that
``ConversationService`` calls; the worker-side claim/complete port and the
service layer follow in the same workstream.
"""

from __future__ import annotations

import uuid
from typing import Any


def _message_order(turn_id: str | None, message_id: int | None) -> tuple[int, str]:
    """Same watermark ordering as the SQLite enqueue (numeric id, then turn id)."""
    return (int(message_id or 0), str(turn_id or ""))


def _insert_project_job(
    conn,
    *,
    session_id: int,
    scope_fingerprint: str,
    target_turn_id: str,
    target_message_id: int,
    source_start_turn_id: str | None,
    source_start_message_id: int | None,
    requested_at: str,
    available_at: str,
) -> str:
    job_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_jobs
            (job_id, session_id, job_kind, scope_fingerprint, target_turn_id,
             target_message_id, source_start_turn_id, source_start_message_id,
             generation, status, requested_at, available_at)
        VALUES (%s, %s, 'project_reflection', %s, %s, %s, %s, %s, 1, 'pending',
                %s::timestamptz, %s::timestamptz)
        """,
        (
            job_id,
            int(session_id),
            str(scope_fingerprint),
            str(target_turn_id),
            int(target_message_id),
            source_start_turn_id,
            source_start_message_id,
            str(requested_at),
            str(available_at),
        ),
    )
    return job_id


def enqueue_project_reflection(
    conn,
    *,
    session_id: int,
    scope_fingerprint: str,
    target_turn_id: str,
    target_message_id: int,
    available_at: str,
    source_start_turn_id: str | None = None,
    source_start_message_id: int | None = None,
    force: bool = False,
    requested_at: str | None = None,
) -> str:
    """Transaction-safe PostgreSQL twin of the SQLite enqueue.

    Semantics are identical: the newest message wins, ``force`` may move the
    watermark backwards, and every mutation fences in-flight runs by bumping
    ``generation``.
    """
    from src.memory.jobs import utc_now

    now = str(requested_at or utc_now())
    existing = conn.execute(
        "SELECT * FROM memory_jobs WHERE session_id = %s AND job_kind = 'project_reflection'",
        (int(session_id),),
    ).fetchone()
    if existing is None:
        return _insert_project_job(
            conn,
            session_id=session_id,
            scope_fingerprint=scope_fingerprint,
            target_turn_id=target_turn_id,
            target_message_id=target_message_id,
            source_start_turn_id=source_start_turn_id,
            source_start_message_id=source_start_message_id,
            requested_at=now,
            available_at=available_at,
        )
    if _message_order(target_turn_id, target_message_id) <= _message_order(
        existing["target_turn_id"], existing["target_message_id"]
    ):
        if force:
            conn.execute(
                """
                UPDATE memory_jobs
                SET scope_fingerprint = %s, target_turn_id = %s, target_message_id = %s,
                    source_start_turn_id = COALESCE(source_start_turn_id, %s),
                    source_start_message_id = COALESCE(source_start_message_id, %s),
                    generation = generation + 1, status = 'pending',
                    available_at = %s::timestamptz, requested_at = %s::timestamptz,
                    completed_at = NULL, last_error = '',
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE job_id = %s
                """,
                (
                    str(scope_fingerprint),
                    str(target_turn_id),
                    int(target_message_id),
                    source_start_turn_id,
                    source_start_message_id,
                    str(available_at),
                    now,
                    str(existing["job_id"]),
                ),
            )
        return str(existing["job_id"])
    conn.execute(
        """
        UPDATE memory_jobs
        SET scope_fingerprint = %s, target_turn_id = %s, target_message_id = %s,
            source_start_turn_id = COALESCE(source_start_turn_id, %s),
            source_start_message_id = COALESCE(source_start_message_id, %s),
            generation = generation + 1, status = 'pending',
            available_at = %s::timestamptz, requested_at = %s::timestamptz,
            completed_at = NULL, last_error = ''
        WHERE job_id = %s
        """,
        (
            str(scope_fingerprint),
            str(target_turn_id),
            int(target_message_id),
            source_start_turn_id,
            source_start_message_id,
            str(available_at),
            now,
            str(existing["job_id"]),
        ),
    )
    return str(existing["job_id"])


def fetch_project_job(conn, session_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM memory_jobs WHERE session_id = %s AND job_kind = 'project_reflection'",
        (int(session_id),),
    ).fetchone()
    return dict(row) if row is not None else None
