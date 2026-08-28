"""Durable reflection jobs, consent ledger, leases and replay records."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.memory.catalog import (
    MemoryCatalogRepository,
    MemoryJob,
    ensure_memory_schema,
    get_job,
    json_dumps,
    row_to_job,
    utc_now,
)
from src.memory.schemas import MEMORY_SCHEMA_VERSION, MemoryConsentManifest, MemoryConsentSourceItem, content_hash, manifest_hash
from src.observability.metrics import counter

import logging

_logger = logging.getLogger("RAG")


def _dead_letter_alert(event: dict) -> None:
    """Emit a structured ERROR log so log-based alerting can pick it up.

    Payload is limited to identifier fields (job id/kind/attempts/error class)
    and never carries user text; metrics remain the numeric source of truth.
    """
    try:
        _logger.error("hdb.memory.dead_letter %s", json.dumps(event, ensure_ascii=False, default=str))
    except Exception:
        pass


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))).isoformat()


def _message_order(turn_id: str | None, message_id: int | None) -> tuple[int, str]:
    # SQLite message ids are the stable per-session high-watermark.  UUID turn
    # ids are only a deterministic tie-breaker.  Keep the first component
    # numeric: lexicographically comparing strings would incorrectly place
    # message 10 before message 2 and could move a debounce watermark back.
    return (int(message_id or 0), str(turn_id or ""))


@dataclass(frozen=True)
class UserMemorySettings:
    user_id: str
    opt_in: bool
    policy_version: str
    revoke_generation: int
    updated_at: str


@dataclass(frozen=True)
class ConsentSnapshot:
    consent_event_id: str
    user_id: str
    session_id: int
    turn_id: str
    message_id: int
    consent_kind: str
    policy_version: str
    consent_revoke_generation: int
    granted_at: str
    revoked_at: str | None
    authorized_start_turn_id: str
    authorized_start_message_id: int
    authorized_end_turn_id: str
    authorized_end_message_id: int
    authorized_source_hash: str
    manifest: tuple[dict[str, Any], ...]


class MemoryJobRepository:
    """Repository for the memory control plane.

    Methods accepting ``conn`` are used by Conversation's existing
    ``BEGIN IMMEDIATE`` transaction.  Standalone methods open their own
    connection and preserve the same SQLite WAL/busy-timeout settings.
    """

    def __init__(self, db_path: str | None = None):
        if not db_path:
            import config.settings as settings

            db_path = settings.AUTH_DB_PATH
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            ensure_memory_schema(conn)

    def catalog(self) -> MemoryCatalogRepository:
        return MemoryCatalogRepository(self.db_path)

    def get(self, job_id: str, *, conn: sqlite3.Connection | None = None) -> MemoryJob | None:
        if conn is not None:
            return get_job(conn, job_id)
        with closing(self._connect()) as owned:
            return get_job(owned, job_id)

    def enqueue_project_reflection(
        self,
        *,
        session_id: int,
        scope_fingerprint: str,
        target_turn_id: str,
        target_message_id: int,
        available_at: str | None = None,
        source_start_turn_id: str | None = None,
        source_start_message_id: int | None = None,
        force: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Create or debounce the single project job for one Session."""
        own = conn is None
        if own:
            conn = self._connect()
        assert conn is not None
        try:
            if own:
                conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            available = available_at or now
            existing = conn.execute(
                "SELECT * FROM memory_jobs WHERE session_id = ? AND job_kind = 'project_reflection'",
                (int(session_id),),
            ).fetchone()
            if existing is None:
                result = _insert_project_job(
                    conn,
                    session_id=session_id,
                    scope_fingerprint=scope_fingerprint,
                    target_turn_id=target_turn_id,
                    target_message_id=target_message_id,
                    source_start_turn_id=source_start_turn_id,
                    source_start_message_id=source_start_message_id,
                    requested_at=now,
                    available_at=available,
                )
                if own:
                    conn.commit()
                return result

            job_id = existing["job_id"]
            old_target = _message_order(existing["target_turn_id"], existing["target_message_id"])
            new_target = _message_order(target_turn_id, target_message_id)
            if new_target <= old_target:
                # A retry or duplicate complete callback must not move a
                # high-watermark backwards or create another generation.
                if force:
                    conn.execute(
                        """UPDATE memory_jobs SET scope_fingerprint = ?, target_turn_id = ?, target_message_id = ?,
                            source_start_turn_id = COALESCE(source_start_turn_id, ?),
                            source_start_message_id = COALESCE(source_start_message_id, ?),
                            generation = generation + 1, status = 'pending', available_at = ?,
                            requested_at = ?, completed_at = NULL, last_error = '',
                            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                        WHERE job_id = ?""",
                        (scope_fingerprint, str(target_turn_id), int(target_message_id), source_start_turn_id,
                         source_start_message_id, available, now, job_id),
                    )
                    if own:
                        conn.commit()
                elif own:
                    conn.commit()
                return job_id
            # A high-watermark update invalidates the old lease. Requeue it
            # with a fresh claim so an in-flight Worker cannot commit an
            # obsolete conversation window.
            next_status = "pending"
            conn.execute(
                """UPDATE memory_jobs
                SET scope_fingerprint = ?, target_turn_id = ?, target_message_id = ?,
                    source_start_turn_id = COALESCE(source_start_turn_id, ?),
                    source_start_message_id = COALESCE(source_start_message_id, ?),
                    generation = generation + 1, status = ?, available_at = ?,
                    requested_at = ?, completed_at = NULL, last_error = '',
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE job_id = ?""",
                (scope_fingerprint, str(target_turn_id), int(target_message_id), source_start_turn_id,
                 source_start_message_id, next_status, available, now, job_id),
            )
            if own:
                conn.commit()
            return job_id
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    def enqueue_user_reflection(
        self,
        *,
        session_id: int,
        scope_fingerprint: str,
        target_turn_id: str,
        target_message_id: int,
        consent: ConsentSnapshot,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Create exactly one immutable User Job for a consent event."""
        own = conn is None
        if own:
            conn = self._connect()
        assert conn is not None
        try:
            if own:
                conn.execute("BEGIN IMMEDIATE")
            job_id = str(uuid.uuid4())
            now = utc_now()
            conn.execute(
                """INSERT INTO memory_jobs
                (job_id, session_id, job_kind, scope_fingerprint, consent_event_id,
                 consent_policy_version, consent_revoke_generation, target_turn_id, target_message_id,
                 authorized_start_turn_id, authorized_start_message_id, authorized_end_turn_id,
                 authorized_end_message_id, authorized_source_hash, generation, status, requested_at, available_at)
                VALUES (?, ?, 'user_reflection', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?)""",
                (job_id, int(session_id), scope_fingerprint, consent.consent_event_id,
                 consent.policy_version, consent.consent_revoke_generation, target_turn_id, int(target_message_id),
                 consent.authorized_start_turn_id, consent.authorized_start_message_id,
                 consent.authorized_end_turn_id, consent.authorized_end_message_id,
                 consent.authorized_source_hash, now, now),
            )
            if own:
                conn.commit()
            return job_id
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 180,
        max_retries: int = 5,
        now: str | None = None,
    ) -> MemoryJob | None:
        """Atomically claim the oldest available job with a fencing token."""
        now = now or utc_now()
        token = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT job_id FROM memory_jobs
                    WHERE (status IN ('pending', 'retrying') AND available_at <= ?
                           AND (next_retry_at IS NULL OR next_retry_at <= ?))
                       OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                    ORDER BY CASE WHEN job_kind = 'user_reflection' THEN 0 ELSE 1 END, requested_at, job_id
                    LIMIT 1""",
                    (now, now, now),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                current = conn.execute("SELECT * FROM memory_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
                max_attempts = max(1, int(max_retries))
                current_attempts = int(current["retry_count"] or 0)
                # ``retry_count`` is the number of claims/attempts already
                # made.  A job may be claimed exactly max_attempts times; a
                # lease-expired job at that boundary is dead-lettered without
                # manufacturing an extra attempt number.
                if current_attempts >= max_attempts:
                    conn.execute(
                        "UPDATE memory_jobs SET status = 'dead_letter', retry_count = ?, last_error = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL WHERE job_id = ?",
                        (current_attempts, "maximum memory job attempts exceeded", row["job_id"]),
                    )
                    counter("hdb.memory.job_dead_letter", attributes={"job_kind": current["job_kind"]})
                    _dead_letter_alert(
                        {
                            "queue": "job",
                            "job_id": str(row["job_id"]),
                            "job_kind": str(current["job_kind"]),
                            "attempts": current_attempts,
                            "reason": "max_attempts_exceeded_on_claim",
                        }
                    )
                    conn.commit()
                    return None
                retry_count = current_attempts + 1
                conn.execute(
                    """UPDATE memory_jobs
                    SET status = 'running', lease_owner = ?, lease_token = ?,
                        lease_expires_at = ?, claimed_at = ?, retry_count = ?, next_retry_at = NULL
                    WHERE job_id = ?""",
                    (worker_id, token, _iso_after(lease_seconds), now, retry_count, row["job_id"]),
                )
                claimed = conn.execute("SELECT * FROM memory_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
                conn.commit()
                counter("hdb.memory.jobs_total", attributes={"job_kind": claimed["job_kind"]})
                if current["status"] == "running":
                    counter("hdb.memory.job_lease_expired", attributes={"job_kind": current["job_kind"]})
                if current_attempts > 0:
                    # A claim after at least one prior attempt is a replay of
                    # the same durable Job (lease expiry / retry / requeue).
                    counter("hdb.memory.job_replayed", attributes={"job_kind": claimed["job_kind"]})
                return row_to_job(claimed)
            except Exception:
                conn.rollback()
                raise

    def heartbeat(self, job_id: str, lease_token: str, generation: int, *, lease_seconds: int = 180) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """UPDATE memory_jobs SET lease_expires_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_token = ? AND generation = ?""",
                (_iso_after(lease_seconds), job_id, lease_token, int(generation)),
            )
            return bool(cur.rowcount)

    def mark_completed(self, job_id: str, lease_token: str, generation: int) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """UPDATE memory_jobs SET status = 'completed', completed_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE job_id = ? AND status = 'running' AND lease_token = ? AND generation = ?""",
                (utc_now(), job_id, lease_token, int(generation)),
            )
            return bool(cur.rowcount)

    def mark_retry(
        self,
        job_id: str,
        lease_token: str,
        generation: int,
        error: str,
        *,
        retry_after_seconds: int = 30,
        max_retries: int = 5,
    ) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT retry_count FROM memory_jobs WHERE job_id = ? AND status = 'running' AND lease_token = ? AND generation = ?",
                (job_id, lease_token, int(generation)),
            ).fetchone()
            if row is None:
                return "stale"
            retry_count = int(row["retry_count"] or 0)
            status = "dead_letter" if retry_count >= max(1, int(max_retries)) else "retrying"
            conn.execute(
                """UPDATE memory_jobs SET status = ?, last_error = ?, next_retry_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE job_id = ? AND status = 'running' AND lease_token = ? AND generation = ?""",
                (status, str(error)[:2000], _iso_after(retry_after_seconds) if status == "retrying" else None,
                 job_id, lease_token, int(generation)),
            )
            counter("hdb.memory.jobs_failed", attributes={"status": status})
            if status == "dead_letter":
                counter("hdb.memory.job_dead_letter")
                _dead_letter_alert(
                    {
                        "queue": "job",
                        "job_id": str(job_id),
                        "attempts": retry_count,
                        "reason": str(error)[:200],
                    }
                )
            return status

    def requeue_dead_letter(self, job_id: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "UPDATE memory_jobs SET status = 'pending', retry_count = 0, last_error = '', next_retry_at = NULL, available_at = ? WHERE job_id = ? AND status = 'dead_letter'",
                (utc_now(), job_id),
            )
            return bool(cur.rowcount)

    def ensure_checkpoint(self, job_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        c = conn or self._connect()
        own = conn is None
        try:
            c.execute(
                "INSERT OR IGNORE INTO memory_checkpoints (job_id, committed_generation, updated_at) VALUES (?, 0, ?)",
                (job_id, utc_now()),
            )
        finally:
            if own:
                c.close()

    def save_run(
        self,
        *,
        job: MemoryJob,
        run_key: str,
        source_snapshot_hash: str,
        source_snapshot_ref: str | None,
        extractor_model: str,
        extractor_version: str,
        schema_version: str,
        conn: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        own = conn is None
        if own:
            conn = self._connect()
        assert conn is not None
        try:
            row = conn.execute("SELECT * FROM memory_reflection_runs WHERE run_key = ?", (run_key,)).fetchone()
            if row is not None:
                return row
            run_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO memory_reflection_runs
                (run_id, job_id, generation, job_kind, consent_event_id, run_key, consent_revoke_generation,
                 authorized_start_turn_id, authorized_start_message_id, authorized_end_turn_id,
                 authorized_end_message_id, authorized_source_hash, source_snapshot_hash,
                 encrypted_source_snapshot_ref, extractor_model, extractor_version, schema_version,
                 status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)""",
                (run_id, job.job_id, job.generation, job.job_kind, job.consent_event_id, run_key,
                 job.consent_revoke_generation, job.authorized_start_turn_id, job.authorized_start_message_id,
                 job.authorized_end_turn_id, job.authorized_end_message_id, job.authorized_source_hash,
                 source_snapshot_hash, source_snapshot_ref, extractor_model, extractor_version, schema_version, utc_now()),
            )
            return conn.execute("SELECT * FROM memory_reflection_runs WHERE run_id = ?", (run_id,)).fetchone()
        finally:
            if own:
                conn.close()

    def persist_output(
        self,
        run_id: str,
        output: list[dict[str, Any]],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | None:
        own = conn is None
        if own:
            conn = self._connect()
        assert conn is not None
        try:
            output_json = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output_hash = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
            conn.execute(
                "UPDATE memory_reflection_runs SET output_payload_json = ?, output_hash = ?, status = 'output_persisted' WHERE run_id = ? AND output_payload_json IS NULL",
                (output_json, output_hash, run_id),
            )
            return conn.execute("SELECT * FROM memory_reflection_runs WHERE run_id = ?", (run_id,)).fetchone()
        finally:
            if own:
                conn.close()

    def persist_output_for_job(
        self,
        *,
        job: MemoryJob,
        run_id: str,
        output: list[dict[str, Any]],
    ) -> bool:
        """Persist model output only while the claimed job is still valid.

        Output and source snapshots can contain private conversation content.
        The consent/generation check is deliberately in the same SQLite write
        transaction as the output insert, so an opt-out cannot race and leave
        a replayable output behind after its cleanup transaction commits.
        """
        output_json = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        output_hash = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = get_job(conn, job.job_id)
                if (
                    current is None
                    or current.status != "running"
                    or current.lease_token != job.lease_token
                    or current.generation != int(job.generation)
                ):
                    conn.rollback()
                    return False
                if current.job_kind == "user_reflection":
                    self.validate_consent_for_job(current, conn=conn)
                row = conn.execute(
                    "SELECT output_payload_json FROM memory_reflection_runs WHERE run_id = ? AND job_id = ? AND generation = ?",
                    (run_id, job.job_id, int(job.generation)),
                ).fetchone()
                if row is None:
                    raise ValueError("reflection run does not belong to the claimed job generation")
                if row["output_payload_json"] is None:
                    conn.execute(
                        "UPDATE memory_reflection_runs SET output_payload_json = ?, output_hash = ?, status = 'output_persisted' WHERE run_id = ? AND job_id = ? AND generation = ? AND output_payload_json IS NULL",
                        (output_json, output_hash, run_id, job.job_id, int(job.generation)),
                    )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def list_run_items(self, run_id: str, *, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        c = conn or self._connect()
        own = conn is None
        try:
            return c.execute("SELECT * FROM memory_run_items WHERE run_id = ? ORDER BY item_ordinal", (run_id,)).fetchall()
        finally:
            if own:
                c.close()

    def upsert_run_item(
        self,
        *,
        run_id: str,
        item_ordinal: int,
        output_item_hash: str,
        planned_action: str,
        memory_id: str | None = None,
        revision_no: int | None = None,
        projection_id: str | None = None,
        expected_fence: int | None = None,
        status: str = "planned",
        error: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        c = conn or self._connect()
        own = conn is None
        try:
            c.execute(
                """INSERT INTO memory_run_items
                (run_id, item_ordinal, output_item_hash, planned_action, memory_id, revision_no, projection_id, expected_fence, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, item_ordinal) DO UPDATE SET
                    output_item_hash = excluded.output_item_hash,
                    planned_action = excluded.planned_action,
                    memory_id = COALESCE(memory_run_items.memory_id, excluded.memory_id),
                    revision_no = COALESCE(memory_run_items.revision_no, excluded.revision_no),
                    projection_id = COALESCE(memory_run_items.projection_id, excluded.projection_id),
                    expected_fence = COALESCE(memory_run_items.expected_fence, excluded.expected_fence),
                    status = excluded.status, error = excluded.error""",
                (run_id, int(item_ordinal), output_item_hash, planned_action, memory_id, revision_no,
                 projection_id, expected_fence, status, error),
            )
        finally:
            if own:
                c.close()

    def update_checkpoint(
        self,
        *,
        job_id: str,
        lease_token: str,
        generation: int,
        committed_turn_id: str,
        committed_message_id: int,
        source_fingerprint: str,
    ) -> bool:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self.ensure_checkpoint(job_id, conn=conn)
                updated = self.update_checkpoint_in_connection(
                    conn,
                    job_id=job_id,
                    lease_token=lease_token,
                    generation=generation,
                    committed_turn_id=committed_turn_id,
                    committed_message_id=committed_message_id,
                    source_fingerprint=source_fingerprint,
                )
                conn.commit()
                return updated
            except Exception:
                conn.rollback()
                raise

    def update_checkpoint_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        lease_token: str,
        generation: int,
        committed_turn_id: str,
        committed_message_id: int,
        source_fingerprint: str,
    ) -> bool:
        """Advance a checkpoint inside the caller's mutation barrier.

        The Worker applies Catalog revisions, advances the checkpoint and
        completes the Job in one ``BEGIN IMMEDIATE`` transaction.  Keeping
        this repository method connection-aware avoids a second SQLite write
        transaction and makes the lease/generation/consent guard explicit at
        the checkpoint boundary.
        """
        current = get_job(conn, job_id)
        if current is None or current.status != "running" or current.lease_token != lease_token or current.generation != int(generation):
            return False
        if current.job_kind == "user_reflection":
            # Re-check the immutable manifest, revoke generation and policy
            # while the write transaction still holds SQLite's writer lock.
            self.validate_consent_for_job(current, conn=conn)
        cur = conn.execute(
            """UPDATE memory_checkpoints SET committed_turn_id = ?, committed_message_id = ?,
               committed_generation = ?, source_fingerprint = ?, updated_at = ?
               WHERE job_id = ? AND committed_generation <= ?""",
            (
                committed_turn_id,
                int(committed_message_id),
                int(generation),
                source_fingerprint,
                utc_now(),
                job_id,
                int(generation),
            ),
        )
        if cur.rowcount:
            conn.execute(
                """UPDATE memory_reflection_runs
                   SET committed_at = ?, status = 'projected'
                   WHERE job_id = ? AND generation = ?
                     AND status IN ('output_persisted', 'catalog_committed', 'projected')""",
                (utc_now(), job_id, int(generation)),
            )
        return bool(cur.rowcount)

    # ------------------------------------------------------------------
    # User settings and consent

    def get_user_settings(self, user_id: str | int, *, conn: sqlite3.Connection | None = None) -> UserMemorySettings:
        c = conn or self._connect()
        own = conn is None
        try:
            row = c.execute("SELECT * FROM user_memory_settings WHERE user_id = ?", (str(user_id),)).fetchone()
            if row is None:
                return UserMemorySettings(str(user_id), False, "v1", 0, "")
            return UserMemorySettings(row["user_id"], bool(row["opt_in"]), row["policy_version"], int(row["revoke_generation"]), row["updated_at"])
        finally:
            if own:
                c.close()

    def set_user_opt_in(
        self,
        user_id: str | int,
        enabled: bool,
        *,
        policy_version: str = "v1",
        reason: str = "",
        request_id: str = "",
    ) -> UserMemorySettings:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self.get_user_settings(user_id, conn=conn)
                generation = current.revoke_generation + (0 if enabled else 1)
                now = utc_now()
                conn.execute(
                    """INSERT INTO user_memory_settings (user_id, opt_in, policy_version, revoke_generation, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET opt_in = excluded.opt_in,
                        policy_version = excluded.policy_version, revoke_generation = excluded.revoke_generation,
                        updated_at = excluded.updated_at""",
                    (str(user_id), int(bool(enabled)), policy_version, generation, now),
                )
                if not enabled:
                    self._revoke_user_consent_in_transaction(
                        conn,
                        str(user_id),
                        generation,
                        reason="account_opt_out",
                        request_id=request_id,
                    )
                conn.execute(
                    """INSERT INTO memory_audit_events
                       (audit_event_id, event_type, actor_id, request_id, metadata_json, created_at)
                       VALUES (?, 'user_memory_settings_changed', ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        str(user_id),
                        str(request_id or ""),
                        json_dumps({"opt_in": bool(enabled), "reason": str(reason or "")}),
                        now,
                    ),
                )
                conn.commit()
                return self.get_user_settings(user_id)
            except Exception:
                conn.rollback()
                raise

    def _revoke_user_consent_in_transaction(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        generation: int,
        *,
        reason: str,
        request_id: str = "",
    ) -> int:
        now = utc_now()
        rows = conn.execute(
            "SELECT consent_event_id FROM memory_consent_events WHERE user_id = ? AND revoked_at IS NULL",
            (user_id,),
        ).fetchall()
        if not rows:
            return 0
        ids = [row["consent_event_id"] for row in rows]
        conn.executemany(
            "UPDATE memory_consent_events SET revoked_at = ? WHERE consent_event_id = ? AND revoked_at IS NULL",
            [(now, value) for value in ids],
        )
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE memory_jobs SET status = 'cancelled', generation = generation + 1, last_error = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL WHERE consent_event_id IN ({placeholders}) AND status IN ('pending', 'retrying', 'running')",
            (reason, *ids),
        )
        conn.execute(
            f"UPDATE memory_reflection_runs SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL, status = 'failed' WHERE consent_event_id IN ({placeholders})",
            ids,
        )
        sources = conn.execute(
            f"SELECT DISTINCT memory_id FROM memory_sources WHERE consent_event_id IN ({placeholders}) AND source_valid = 1",
            ids,
        ).fetchall()
        conn.execute(
            f"UPDATE memory_sources SET source_valid = 0, invalidated_at = ? WHERE consent_event_id IN ({placeholders}) AND source_valid = 1",
            (now, *ids),
        )
        for row in sources:
            _invalidate_memory_in_transaction(conn, row["memory_id"], reason=reason)
        for consent_id in ids:
            self._audit_consent(
                conn,
                consent_id,
                "consent_revoked",
                request_id=request_id,
                metadata={"reason": reason, "generation": generation},
            )
        return len(ids)

    def revoke_consent(
        self,
        user_id: str | int,
        consent_event_id: str,
        *,
        reason: str = "explicit_revoke",
        request_id: str = "",
    ) -> bool:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM memory_consent_events WHERE consent_event_id = ? AND user_id = ? AND revoked_at IS NULL",
                    (consent_event_id, str(user_id)),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return False
                now = utc_now()
                conn.execute("UPDATE memory_consent_events SET revoked_at = ? WHERE consent_event_id = ? AND revoked_at IS NULL", (now, consent_event_id))
                conn.execute("UPDATE memory_jobs SET status = 'cancelled', generation = generation + 1, last_error = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL WHERE consent_event_id = ? AND status IN ('pending', 'retrying', 'running')", (str(reason)[:2000], consent_event_id))
                # A revoked consent must not leave a replayable source snapshot
                # or model output behind even when the job had not yet created a
                # Catalog memory item.
                conn.execute(
                    "UPDATE memory_reflection_runs SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL, status = 'failed' WHERE consent_event_id = ?",
                    (consent_event_id,),
                )
                sources = conn.execute("SELECT DISTINCT memory_id FROM memory_sources WHERE consent_event_id = ? AND source_valid = 1", (consent_event_id,)).fetchall()
                conn.execute("UPDATE memory_sources SET source_valid = 0, invalidated_at = ? WHERE consent_event_id = ? AND source_valid = 1", (now, consent_event_id))
                for source in sources:
                    _invalidate_memory_in_transaction(conn, source["memory_id"], reason=reason)
                self._audit_consent(
                    conn,
                    consent_event_id,
                    "consent_revoked",
                    request_id=request_id,
                    metadata={"reason": reason},
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def create_consent_event(
        self,
        *,
        user_id: str | int,
        session_id: int,
        turn_id: str,
        message_id: int,
        policy_version: str,
        consent_revoke_generation: int,
        manifest: MemoryConsentManifest,
        conn: sqlite3.Connection | None = None,
    ) -> ConsentSnapshot:
        """Persist an immutable consent manifest and its User Job atomically."""
        if not manifest.items:
            raise ValueError("consent manifest must contain at least one source")
        computed_hash = manifest_hash(manifest)
        own = conn is None
        if own:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
        assert conn is not None
        try:
            settings = self.get_user_settings(user_id, conn=conn)
            if not settings.opt_in:
                raise PermissionError("user memory opt-in is required")
            if settings.policy_version != policy_version or settings.revoke_generation != int(consent_revoke_generation):
                raise PermissionError("consent policy or revoke generation is stale")
            event_id = str(uuid.uuid4())
            first, last = manifest.items[0], manifest.items[-1]
            now = utc_now()
            conn.execute(
                """INSERT INTO memory_consent_events
                (consent_event_id, user_id, session_id, turn_id, message_id, consent_kind, policy_version,
                 consent_revoke_generation, granted_at, authorized_start_turn_id, authorized_start_message_id,
                 authorized_end_turn_id, authorized_end_message_id, authorized_source_hash, created_at)
                VALUES (?, ?, ?, ?, ?, 'user_memory_extract', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, str(user_id), int(session_id), str(turn_id), int(message_id), policy_version,
                 int(consent_revoke_generation), now, first.turn_id, first.message_id, last.turn_id,
                 last.message_id, computed_hash, now),
            )
            conn.executemany(
                "INSERT INTO memory_consent_source_items (consent_event_id, ordinal, turn_id, message_id, role, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
                [(event_id, item.ordinal, item.turn_id, item.message_id, item.role, item.content_hash) for item in manifest.items],
            )
            snapshot = ConsentSnapshot(
                consent_event_id=event_id,
                user_id=str(user_id),
                session_id=int(session_id),
                turn_id=str(turn_id),
                message_id=int(message_id),
                consent_kind="user_memory_extract",
                policy_version=policy_version,
                consent_revoke_generation=int(consent_revoke_generation),
                granted_at=now,
                revoked_at=None,
                authorized_start_turn_id=first.turn_id,
                authorized_start_message_id=first.message_id,
                authorized_end_turn_id=last.turn_id,
                authorized_end_message_id=last.message_id,
                authorized_source_hash=computed_hash,
                manifest=tuple({"ordinal": item.ordinal, "turn_id": item.turn_id, "message_id": item.message_id, "role": item.role, "content_hash": item.content_hash} for item in manifest.items),
            )
            # User scope fingerprint is supplied by the service; this method
            # only owns the consent and job transaction.
            if own:
                conn.commit()
            return snapshot
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    def get_consent(self, consent_event_id: str, *, conn: sqlite3.Connection | None = None) -> ConsentSnapshot | None:
        c = conn or self._connect()
        own = conn is None
        try:
            row = c.execute("SELECT * FROM memory_consent_events WHERE consent_event_id = ?", (consent_event_id,)).fetchone()
            if row is None:
                return None
            items = c.execute("SELECT * FROM memory_consent_source_items WHERE consent_event_id = ? ORDER BY ordinal", (consent_event_id,)).fetchall()
            return ConsentSnapshot(
                consent_event_id=row["consent_event_id"], user_id=row["user_id"], session_id=int(row["session_id"]),
                turn_id=row["turn_id"], message_id=int(row["message_id"]), consent_kind=row["consent_kind"],
                policy_version=row["policy_version"], consent_revoke_generation=int(row["consent_revoke_generation"]),
                granted_at=row["granted_at"], revoked_at=row["revoked_at"],
                authorized_start_turn_id=row["authorized_start_turn_id"], authorized_start_message_id=int(row["authorized_start_message_id"]),
                authorized_end_turn_id=row["authorized_end_turn_id"], authorized_end_message_id=int(row["authorized_end_message_id"]),
                authorized_source_hash=row["authorized_source_hash"],
                # ``memory_consent_source_items`` carries the parent event id
                # as a relational key.  Do not expose that implementation
                # column to the frozen semantic source-item schema, whose
                # extra-field rejection is part of the manifest invariant.
                manifest=tuple(
                    {
                        "ordinal": int(item["ordinal"]),
                        "turn_id": item["turn_id"],
                        "message_id": int(item["message_id"]),
                        "role": item["role"],
                        "content_hash": item["content_hash"],
                    }
                    for item in items
                ),
            )
        finally:
            if own:
                c.close()

    def validate_consent_for_job(self, job: MemoryJob, *, conn: sqlite3.Connection | None = None) -> ConsentSnapshot:
        if job.job_kind != "user_reflection" or not job.consent_event_id:
            raise ValueError("job is not a user reflection")
        c = conn or self._connect()
        own = conn is None
        try:
            consent = self.get_consent(job.consent_event_id, conn=c)
            if consent is None or consent.revoked_at:
                raise PermissionError("consent is missing or revoked")
            settings = self.get_user_settings(consent.user_id, conn=c)
            if not settings.opt_in or settings.policy_version != job.consent_policy_version or settings.revoke_generation != job.consent_revoke_generation:
                raise PermissionError("user memory settings no longer authorize this job")
            if (consent.authorized_start_turn_id, consent.authorized_start_message_id, consent.authorized_end_turn_id, consent.authorized_end_message_id, consent.authorized_source_hash) != (
                job.authorized_start_turn_id, job.authorized_start_message_id, job.authorized_end_turn_id, job.authorized_end_message_id, job.authorized_source_hash
            ):
                raise PermissionError("job consent bounds do not match immutable consent manifest")
            if int(job.session_id) != int(consent.session_id) or str(job.target_turn_id) != str(consent.turn_id):
                raise PermissionError("job target does not match immutable consent event")
            try:
                manifest = MemoryConsentManifest(
                    items=tuple(MemoryConsentSourceItem.model_validate(item) for item in consent.manifest)
                )
            except Exception as exc:
                raise PermissionError("consent source manifest is invalid") from exc
            if manifest_hash(manifest) != consent.authorized_source_hash:
                raise PermissionError("consent source manifest hash is invalid")
            return consent
        finally:
            if own:
                c.close()

    def _audit_consent(
        self,
        conn: sqlite3.Connection,
        consent_event_id: str,
        event_type: str,
        *,
        request_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO memory_audit_events
               (audit_event_id, operation_id, event_type, request_id, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), consent_event_id, event_type, str(request_id or ""), json_dumps(metadata or {}), utc_now()),
        )


def apply_source_invalidation(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    ref_id: str,
    reason: str,
) -> None:
    """Fence one Memory record after its backing sources became invalid.

    Shared by Session-level clear/delete (§43) and single-message
    edit/redaction.  Classification follows LangMem V2 §43: User records are
    privacy-deleted with payload scrub; Verified drops to provenance_missing;
    Candidate memories either rebuild from remaining valid sources or die.
    Projections are retired (fence++) and deletion outbox entries are queued
    inside the caller's transaction.
    """
    now = utc_now()
    record = conn.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
    if record is None:
        return
    valid_count = conn.execute(
        "SELECT COUNT(*) AS count FROM memory_sources WHERE memory_id = ? AND source_valid = 1",
        (memory_id,),
    ).fetchone()["count"]
    if record["scope"] == "user":
        next_status = "deleted"
    elif record["status"] == "verified":
        next_status = "provenance_missing"
    else:
        next_status = "needs_rebuild" if int(valid_count or 0) else "deleted"
    conn.execute(
        "UPDATE memory_records SET status = ?, deleted_at = CASE WHEN ? = 'deleted' THEN ? ELSE deleted_at END, updated_at = ? WHERE memory_id = ?",
        (next_status, next_status, now, now, memory_id),
    )
    if record["scope"] == "user":
        # Losing the last authorization is a privacy deletion for User
        # Memory.  Keep only the tombstone/hash and audit evidence; the
        # semantic payload and replayable run output must not survive.
        redacted_hash = content_hash({}, schema_version=record["schema_version"] or MEMORY_SCHEMA_VERSION)
        conn.execute(
            "UPDATE memory_records SET content_json = '{}', content_hash = ?, title = '已删除记忆', subject = NULL, memory_type = 'context' WHERE memory_id = ?",
            (redacted_hash, memory_id),
        )
        conn.execute(
            "UPDATE memory_revisions SET before_content_json = NULL, after_content_json = NULL WHERE memory_id = ?",
            (memory_id,),
        )
        conn.execute(
            "UPDATE memory_reflection_runs SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL, status = 'failed'"
            " WHERE run_id IN (SELECT run_id FROM memory_run_items WHERE memory_id = ?)",
            (memory_id,),
        )
    projections = conn.execute(
        "SELECT * FROM memory_projections WHERE memory_id = ? AND retired_at IS NULL",
        (memory_id,),
    ).fetchall()
    for projection in projections:
        next_fence = int(projection["fence_version"]) + 1
        conn.execute(
            "UPDATE memory_projections SET active = 0, manager_writable = 0, fence_version = ?, retired_at = ?"
            " WHERE projection_id = ? AND retired_at IS NULL AND fence_version = ?",
            (next_fence, now, projection["projection_id"], int(projection["fence_version"])),
        )
        operation_id = str(uuid.uuid4())
        payload = {
            "projection_id": projection["projection_id"],
            "namespace": json.loads(projection["namespace_json"]),
            "store_key": projection["store_key"],
            "reason": reason,
        }
        conn.execute(
            """INSERT OR IGNORE INTO memory_deletion_outbox
            (operation_id, memory_id, source_or_consent_id, operation, expected_revision, expected_fence, idempotency_key, payload_json, created_at)
            VALUES (?, ?, ?, 'delete_projection', ?, ?, ?, ?, ?)""",
            (
                operation_id,
                memory_id,
                ref_id,
                int(record["current_revision"]),
                next_fence,
                f"delete:{memory_id}:{projection['projection_id']}:{next_fence}",
                json_dumps(payload),
                now,
            ),
        )
    conn.execute(
        "INSERT INTO memory_audit_events (audit_event_id, memory_id, event_type, metadata_json, created_at)"
        " VALUES (?, ?, 'source_invalidated', ?, ?)",
        (
            str(uuid.uuid4()),
            memory_id,
            json_dumps({"ref": ref_id, "reason": reason, "remaining_valid_sources": int(valid_count or 0)}),
            now,
        ),
    )


def _source_delete_metric(value: int, *, kind: str) -> None:
    try:
        from src.observability.metrics import counter

        counter("hdb.memory.source_delete", value=value, attributes={"kind": kind})
    except Exception:
        pass


def invalidate_message_memory_in_connection(
    conn: sqlite3.Connection,
    session_id: int,
    message_id: int,
    *,
    reason: str,
) -> int:
    """Invalidate memory sourced from a single edited/redacted message.

    Must run inside the same transaction that rewrites the raw chat row
    (LangMem V2 §43): sources are invalidated and their hashes frozen before
    the new content becomes visible; unfinished Jobs are fenced by bumping
    generation; uncommitted reflection runs lose their cached outputs so a
    stale snapshot can never be applied after the edit.
    """
    ref_id = f"message:{int(session_id)}:{int(message_id)}"
    now = utc_now()
    conn.execute(
        """UPDATE memory_jobs SET status = 'cancelled', generation = generation + 1, last_error = ?,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
        WHERE session_id = ? AND status IN ('pending', 'retrying', 'running')""",
        (reason, int(session_id)),
    )
    # Only non-catalog-committed runs carry source-derived payloads worth
    # scrubbing here; projected runs keep their audit ledger and just lose
    # this specific source through the classification below.
    conn.execute(
        """UPDATE memory_reflection_runs
           SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL, status = 'failed'
           WHERE status IN ('prepared', 'output_persisted')
             AND job_id IN (SELECT job_id FROM memory_jobs WHERE session_id = ?)""",
        (int(session_id),),
    )
    source_rows = conn.execute(
        """SELECT DISTINCT memory_id FROM memory_sources
           WHERE session_id = ? AND message_id = ? AND source_valid = 1""",
        (int(session_id), int(message_id)),
    ).fetchall()
    if not source_rows:
        return 0
    conn.execute(
        """UPDATE memory_sources SET source_valid = 0, invalidated_at = ?
           WHERE session_id = ? AND message_id = ? AND source_valid = 1""",
        (now, int(session_id), int(message_id)),
    )
    affected = 0
    for source_row in source_rows:
        apply_source_invalidation(
            conn,
            str(source_row["memory_id"]),
            ref_id=ref_id,
            reason=reason,
        )
        affected += 1
    _source_delete_metric(affected, kind="message")
    return affected


def invalidate_session_memory_in_connection(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    reason: str,
) -> int:
    """Invalidate memory derived from a session before raw chat deletion.

    A memory with remaining valid sources becomes ``needs_rebuild`` and stays
    out of the active allowlist until a fresh reflection supplies a new
    revision.  A memory without remaining sources becomes a tombstone.  Both
    paths fence and queue every physical projection in the same transaction.
    """
    now = utc_now()
    consent_rows = conn.execute(
        "SELECT consent_event_id FROM memory_consent_events WHERE session_id = ? AND revoked_at IS NULL",
        (int(session_id),),
    ).fetchall()
    consent_ids = [row["consent_event_id"] for row in consent_rows]
    # A reflection run can contain source-derived model output before it has
    # created a Catalog record (for example, a crash between output
    # persistence and Catalog apply).  Scrub every run for this Session before
    # raw chat rows are cleared; filtering only by consent would leave a
    # project run replayable after session deletion.
    conn.execute(
        """UPDATE memory_reflection_runs
           SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL, status = 'failed'
           WHERE job_id IN (SELECT job_id FROM memory_jobs WHERE session_id = ?)""",
        (int(session_id),),
    )
    if consent_ids:
        placeholders = ",".join("?" for _ in consent_ids)
        # Clearing/deleting a source session also invalidates its explicit
        # consent boundary.  Otherwise a later retry could still claim that
        # the deleted messages are authorized.
        conn.execute(
            f"UPDATE memory_consent_events SET revoked_at = ? WHERE consent_event_id IN ({placeholders}) AND revoked_at IS NULL",
            (now, *consent_ids),
        )
        conn.executemany(
            """INSERT INTO memory_audit_events
               (audit_event_id, operation_id, event_type, metadata_json, created_at)
               VALUES (?, ?, 'consent_revoked', ?, ?)""",
            [
                (
                    str(uuid.uuid4()),
                    consent_id,
                    json_dumps({"reason": reason, "session_id": int(session_id)}),
                    now,
                )
                for consent_id in consent_ids
            ],
        )
        conn.execute(
            f"UPDATE memory_reflection_runs SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL, status = 'failed' WHERE consent_event_id IN ({placeholders})",
            consent_ids,
        )
    conn.execute(
        """UPDATE memory_jobs SET status = 'cancelled', generation = generation + 1, last_error = ?,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
        WHERE session_id = ? AND status IN ('pending', 'retrying', 'running')""",
        (reason, int(session_id)),
    )
    source_rows = conn.execute(
        "SELECT DISTINCT memory_id FROM memory_sources WHERE session_id = ? AND source_valid = 1",
        (int(session_id),),
    ).fetchall()
    if not source_rows:
        return 0
    conn.execute(
        "UPDATE memory_sources SET source_valid = 0, invalidated_at = ? WHERE session_id = ? AND source_valid = 1",
        (now, int(session_id)),
    )
    affected = 0
    for source_row in source_rows:
        apply_source_invalidation(
            conn,
            str(source_row["memory_id"]),
            ref_id=f"session:{int(session_id)}",
            reason=reason,
        )
        affected += 1
    _source_delete_metric(affected, kind="session")
    return affected


def _insert_project_job(
    conn: sqlite3.Connection,
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
        """INSERT INTO memory_jobs
        (job_id, session_id, job_kind, scope_fingerprint, target_turn_id, target_message_id,
         source_start_turn_id, source_start_message_id, generation, status, requested_at, available_at)
        VALUES (?, ?, 'project_reflection', ?, ?, ?, ?, ?, 1, 'pending', ?, ?)""",
        (job_id, int(session_id), scope_fingerprint, str(target_turn_id), int(target_message_id),
         source_start_turn_id, source_start_message_id, requested_at, available_at),
    )
    return job_id


def enqueue_project_reflection_in_connection(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    scope_fingerprint: str,
    target_turn_id: str,
    target_message_id: int,
    available_at: str,
    source_start_turn_id: str | None = None,
    source_start_message_id: int | None = None,
    force: bool = False,
) -> str:
    """Transaction-safe entry point used by ``ConversationService``."""
    now = utc_now()
    existing = conn.execute(
        "SELECT * FROM memory_jobs WHERE session_id = ? AND job_kind = 'project_reflection'",
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
    if _message_order(target_turn_id, target_message_id) <= _message_order(existing["target_turn_id"], existing["target_message_id"]):
        if force:
            conn.execute(
                """UPDATE memory_jobs SET scope_fingerprint = ?, target_turn_id = ?, target_message_id = ?,
                    source_start_turn_id = COALESCE(source_start_turn_id, ?),
                    source_start_message_id = COALESCE(source_start_message_id, ?),
                    generation = generation + 1, status = 'pending', available_at = ?,
                    requested_at = ?, completed_at = NULL, last_error = '',
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE job_id = ?""",
                (scope_fingerprint, str(target_turn_id), int(target_message_id), source_start_turn_id,
                 source_start_message_id, available_at, now, existing["job_id"]),
            )
        return existing["job_id"]
    # A newer turn fences any in-flight reflection before the next claim.
    status = "pending"
    conn.execute(
        """UPDATE memory_jobs SET scope_fingerprint = ?, target_turn_id = ?, target_message_id = ?,
            source_start_turn_id = COALESCE(source_start_turn_id, ?),
            source_start_message_id = COALESCE(source_start_message_id, ?),
            generation = generation + 1, status = ?, available_at = ?, requested_at = ?,
            completed_at = NULL, last_error = '' WHERE job_id = ?""",
        (scope_fingerprint, str(target_turn_id), int(target_message_id), source_start_turn_id,
         source_start_message_id, status, available_at, now, existing["job_id"]),
    )
    return existing["job_id"]


def _invalidate_memory_in_transaction(conn: sqlite3.Connection, memory_id: str, *, reason: str) -> None:
    """Immediately remove a derived record from the active allowlist."""
    now = utc_now()
    row = conn.execute("SELECT current_revision FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
    if row is None:
        return
    revision = int(row["current_revision"])
    projections = conn.execute(
        "SELECT * FROM memory_projections WHERE memory_id = ? AND retired_at IS NULL",
        (memory_id,),
    ).fetchall()
    conn.execute(
        "UPDATE memory_records SET status = 'deleted', deleted_at = ?, updated_at = ? WHERE memory_id = ? AND status NOT IN ('deleted', 'rejected', 'superseded')",
        (now, now, memory_id),
    )
    # Consent/opt-out is a privacy deletion, not only an ACL change. Remove
    # the semantic payload and replayable run output in the same transaction;
    # retain hashes/tombstones in the audit/source ledger for accountability.
    redacted_hash = content_hash({}, schema_version=MEMORY_SCHEMA_VERSION)
    conn.execute(
        "UPDATE memory_records SET content_json = '{}', content_hash = ?, title = '已删除记忆', subject = NULL, memory_type = 'context' WHERE memory_id = ?",
        (redacted_hash, memory_id),
    )
    conn.execute("UPDATE memory_revisions SET before_content_json = NULL, after_content_json = NULL WHERE memory_id = ?", (memory_id,))
    conn.execute(
        "UPDATE memory_reflection_runs SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL, status = 'failed' WHERE run_id IN (SELECT run_id FROM memory_run_items WHERE memory_id = ?)",
        (memory_id,),
    )
    for projection in projections:
        next_fence = int(projection["fence_version"]) + 1
        conn.execute(
            "UPDATE memory_projections SET active = 0, manager_writable = 0, fence_version = ?, retired_at = ? WHERE projection_id = ? AND retired_at IS NULL",
            (next_fence, now, projection["projection_id"]),
        )
        operation_id = str(uuid.uuid4())
        payload = {
            "projection_id": projection["projection_id"],
            "namespace": json.loads(projection["namespace_json"]),
            "store_key": projection["store_key"],
            "reason": reason,
        }
        conn.execute(
            """INSERT OR IGNORE INTO memory_deletion_outbox
            (operation_id, memory_id, source_or_consent_id, operation, expected_revision, expected_fence, idempotency_key, payload_json, created_at)
            VALUES (?, ?, ?, 'delete_projection', ?, ?, ?, ?, ?)""",
            (operation_id, memory_id, reason, revision, next_fence, f"delete:{memory_id}:{projection['projection_id']}:{next_fence}", json_dumps(payload), now),
        )
    conn.execute(
        "INSERT INTO memory_audit_events (audit_event_id, memory_id, event_type, metadata_json, created_at) VALUES (?, ?, 'source_invalidated', ?, ?)",
        (str(uuid.uuid4()), memory_id, json_dumps({"reason": reason}), now),
    )


__all__ = [
    "ConsentSnapshot",
    "MemoryJobRepository",
    "UserMemorySettings",
    "enqueue_project_reflection_in_connection",
    "invalidate_message_memory_in_connection",
    "invalidate_session_memory_in_connection",
]
