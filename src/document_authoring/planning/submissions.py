"""Durable outbox records for confirmed document-plan submissions.

Plan confirmation lives in the document-authoring database.  The worker later
claims these records and bridges them to the separate durable job database.
Only immutable identifiers/hashes are stored here; the accepted plan and
session remain the source of truth for materialization.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


SUBMISSION_STATUSES = frozenset({
    "pending", "running", "dispatched", "waiting_human", "retrying",
    "failed", "dead_letter", "cancelled",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(raw: str | None, default: Any = None) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value


@dataclass
class DocumentPlanSubmission:
    submission_id: str
    tenant_id: str
    user_id: str
    task_id: str
    session_id: str
    knowledge_base_name: str
    output_spec_id: str
    output_spec_version: int
    output_spec_hash: str
    document_plan_id: str
    document_plan_version: int
    document_plan_hash: str
    source_snapshot_id: str
    source_snapshot_hash: str
    client_request_id: str
    status: str = "pending"
    attempt: int = 0
    max_attempts: int = 3
    available_at: str = field(default_factory=_iso)
    lease_owner: str | None = None
    lease_token: int = 0
    lease_expires_at: str | None = None
    work_order_id: str | None = None
    job_id: str | None = None
    last_error: str = ""
    created_at: str = field(default_factory=_iso)
    updated_at: str = field(default_factory=_iso)
    completed_at: str | None = None

    @property
    def dead_letter(self) -> bool:
        return self.status == "dead_letter"

    def references(self) -> dict[str, Any]:
        """Return the safe, identifier-only payload persisted in the outbox."""
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "knowledge_base_name": self.knowledge_base_name,
            "output_spec_id": self.output_spec_id,
            "output_spec_version": self.output_spec_version,
            "output_spec_hash": self.output_spec_hash,
            "document_plan_id": self.document_plan_id,
            "document_plan_version": self.document_plan_version,
            "document_plan_hash": self.document_plan_hash,
            "source_snapshot_id": self.source_snapshot_id,
            "source_snapshot_hash": self.source_snapshot_hash,
        }


class DocumentPlanSubmissionStore:
    """SQLite lease/claim repository for the plan-submission outbox."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_plan_submissions (
                    submission_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    knowledge_base_name TEXT NOT NULL,
                    output_spec_id TEXT NOT NULL,
                    output_spec_version INTEGER NOT NULL,
                    output_spec_hash TEXT NOT NULL,
                    document_plan_id TEXT NOT NULL,
                    document_plan_version INTEGER NOT NULL,
                    document_plan_hash TEXT NOT NULL,
                    source_snapshot_id TEXT NOT NULL,
                    source_snapshot_hash TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    work_order_id TEXT,
                    job_id TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_plan_submissions_plan
                    ON document_plan_submissions(
                        tenant_id, user_id, document_plan_id,
                        document_plan_version, document_plan_hash
                    );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_plan_submissions_request
                    ON document_plan_submissions(tenant_id, user_id, session_id, client_request_id);
                CREATE INDEX IF NOT EXISTS idx_document_plan_submissions_queue
                    ON document_plan_submissions(status, available_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_document_plan_submissions_session
                    ON document_plan_submissions(tenant_id, user_id, session_id, created_at);
                """
            )
            # Existing installations may have been initialized by the
            # planning store before this repository was introduced.
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(document_plan_submissions)"
                ).fetchall()
            }
            for column, ddl in (
                ("knowledge_base_name", "TEXT NOT NULL DEFAULT ''"),
                ("client_request_id", "TEXT NOT NULL DEFAULT ''"),
                ("work_order_id", "TEXT"),
                ("job_id", "TEXT"),
                ("last_error", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("completed_at", "TEXT"),
            ):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE document_plan_submissions ADD COLUMN {column} {ddl}"
                    )

    @staticmethod
    def _validate_owner(tenant_id: str, user_id: str) -> tuple[str, str]:
        tenant = str(tenant_id or "").strip()
        user = str(user_id or "").strip()
        if not tenant or not user:
            raise ValueError("submission tenant and user are required")
        return tenant, user

    @staticmethod
    def _validate_status(status: str) -> str:
        value = str(status or "").strip()
        if value not in SUBMISSION_STATUSES:
            raise ValueError("unsupported document plan submission status")
        return value

    def insert_or_get(
        self,
        submission: DocumentPlanSubmission,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> DocumentPlanSubmission:
        """Insert an outbox row or return the exact idempotent existing row.

        ``connection`` is used by the confirmation transaction.  The method
        deliberately does not commit when a caller supplies an open
        connection.
        """
        tenant, user = self._validate_owner(submission.tenant_id, submission.user_id)
        if submission.status not in SUBMISSION_STATUSES:
            raise ValueError("unsupported document plan submission status")
        if not submission.task_id or not submission.session_id:
            raise ValueError("submission task and session ids are required")
        if not submission.client_request_id:
            raise ValueError("submission client request id is required")
        conn = connection or self._connect()
        close = connection is None
        try:
            if close:
                conn.execute("BEGIN IMMEDIATE")
            by_plan = conn.execute(
                """SELECT * FROM document_plan_submissions
                   WHERE tenant_id = ? AND user_id = ?
                     AND document_plan_id = ? AND document_plan_version = ?
                     AND document_plan_hash = ?""",
                (
                    tenant, user, submission.document_plan_id,
                    int(submission.document_plan_version), submission.document_plan_hash,
                ),
            ).fetchone()
            if by_plan is not None:
                existing = _row_to_submission(by_plan)
                if existing.client_request_id != submission.client_request_id:
                    # A second browser/request key confirming the same
                    # accepted plan is a replay of the same submission, not a
                    # new execution branch.
                    pass
                if close:
                    conn.execute("COMMIT")
                return existing
            by_request = conn.execute(
                """SELECT * FROM document_plan_submissions
                   WHERE tenant_id = ? AND user_id = ? AND session_id = ?
                     AND client_request_id = ?""",
                (tenant, user, submission.session_id, submission.client_request_id),
            ).fetchone()
            if by_request is not None:
                existing = _row_to_submission(by_request)
                if (
                    existing.document_plan_id != submission.document_plan_id
                    or existing.document_plan_hash != submission.document_plan_hash
                    or existing.output_spec_hash != submission.output_spec_hash
                ):
                    raise ValueError("submission idempotency key conflicts with existing plan")
                if close:
                    conn.execute("COMMIT")
                return existing
            now = _now()
            values = (
                submission.submission_id, tenant, user, submission.task_id,
                submission.session_id, submission.knowledge_base_name,
                submission.output_spec_id, int(submission.output_spec_version),
                submission.output_spec_hash, submission.document_plan_id,
                int(submission.document_plan_version), submission.document_plan_hash,
                submission.source_snapshot_id, submission.source_snapshot_hash,
                submission.client_request_id, submission.status,
                int(submission.attempt), int(submission.max_attempts),
                submission.available_at or _iso(now), submission.lease_owner,
                int(submission.lease_token), submission.lease_expires_at,
                submission.work_order_id, submission.job_id, submission.last_error,
                submission.created_at or _iso(now), submission.updated_at or _iso(now),
                submission.completed_at,
            )
            conn.execute(
                """INSERT INTO document_plan_submissions (
                       submission_id, tenant_id, user_id, task_id, session_id,
                       knowledge_base_name, output_spec_id, output_spec_version,
                       output_spec_hash, document_plan_id, document_plan_version,
                       document_plan_hash, source_snapshot_id, source_snapshot_hash,
                       client_request_id, status, attempt, max_attempts,
                       available_at, lease_owner, lease_token, lease_expires_at,
                       work_order_id, job_id, last_error, created_at, updated_at,
                       completed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            if close:
                conn.execute("COMMIT")
            return submission
        except Exception:
            if close:
                conn.execute("ROLLBACK")
            raise
        finally:
            if close:
                conn.close()

    def get(self, submission_id: str) -> DocumentPlanSubmission | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_plan_submissions WHERE submission_id = ?",
                (str(submission_id),),
            ).fetchone()
        return _row_to_submission(row) if row is not None else None

    def get_by_plan(
        self,
        document_plan_id: str,
        document_plan_version: int,
        document_plan_hash: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> DocumentPlanSubmission | None:
        clauses = [
            "document_plan_id = ?", "document_plan_version = ?",
            "document_plan_hash = ?",
        ]
        params: list[Any] = [document_plan_id, int(document_plan_version), document_plan_hash]
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(str(tenant_id))
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_plan_submissions WHERE " + " AND ".join(clauses),
                params,
            ).fetchone()
        return _row_to_submission(row) if row is not None else None

    def list_pending(self, limit: int = 16) -> list[DocumentPlanSubmission]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM document_plan_submissions
                   WHERE (status IN ('pending', 'retrying')
                          AND datetime(available_at) <= datetime('now'))
                      OR (status = 'running' AND lease_expires_at IS NOT NULL
                          AND datetime(lease_expires_at) <= datetime('now'))
                   ORDER BY created_at, submission_id LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [_row_to_submission(row) for row in rows]

    def queue_state(self) -> tuple[int, float]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS depth, MIN(created_at) AS oldest
                   FROM document_plan_submissions
                   WHERE (status IN ('pending', 'retrying')
                          AND datetime(available_at) <= datetime('now'))
                      OR (status = 'running' AND lease_expires_at IS NOT NULL
                          AND datetime(lease_expires_at) <= datetime('now'))"""
            ).fetchone()
        if row is None or not row["oldest"]:
            return int(row["depth"] if row else 0), 0.0
        try:
            oldest = datetime.fromisoformat(str(row["oldest"]).replace("Z", "+00:00"))
            return int(row["depth"] or 0), max(0.0, _now().timestamp() - oldest.timestamp())
        except (TypeError, ValueError, OverflowError):
            return int(row["depth"] or 0), 0.0

    def claim(
        self,
        submission_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> DocumentPlanSubmission | None:
        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("worker_id is required")
        now = _now()
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_plan_submissions WHERE submission_id = ?",
                    (str(submission_id),),
                ).fetchone()
                if row is None or row["status"] in {
                    "dispatched", "waiting_human", "failed", "dead_letter", "cancelled",
                }:
                    conn.execute("COMMIT")
                    return None
                available = row["status"] in {"pending", "retrying"} and str(row["available_at"]) <= _iso(now)
                expired = row["status"] == "running" and row["lease_expires_at"] and str(row["lease_expires_at"]) <= _iso(now)
                if not (available or expired):
                    conn.execute("COMMIT")
                    return None
                attempt = int(row["attempt"] or 0) + 1
                if attempt > int(row["max_attempts"] or 1):
                    conn.execute(
                        """UPDATE document_plan_submissions
                           SET status = 'dead_letter', last_error = ?,
                               updated_at = ?, completed_at = ?,
                               lease_owner = NULL, lease_expires_at = NULL
                           WHERE submission_id = ?""",
                        ("maximum_attempts_exceeded", _iso(now), _iso(now), str(submission_id)),
                    )
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """UPDATE document_plan_submissions
                       SET status = 'running', attempt = ?, lease_owner = ?,
                           lease_token = lease_token + 1, lease_expires_at = ?,
                           updated_at = ?, last_error = ''
                       WHERE submission_id = ?""",
                    (attempt, worker, _iso(expires), _iso(now), str(submission_id)),
                )
                claimed = conn.execute(
                    "SELECT * FROM document_plan_submissions WHERE submission_id = ?",
                    (str(submission_id),),
                ).fetchone()
                conn.execute("COMMIT")
                return _row_to_submission(claimed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def heartbeat(
        self,
        submission_id: str,
        worker_id: str,
        lease_token: int,
        lease_seconds: int = 60,
    ) -> DocumentPlanSubmission:
        now = _now()
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
                    """UPDATE document_plan_submissions
                       SET lease_expires_at = ?, updated_at = ?
                       WHERE submission_id = ? AND status = 'running'
                         AND lease_owner = ? AND lease_token = ?""",
                    (_iso(expires), _iso(now), str(submission_id), str(worker_id), int(lease_token)),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("document plan submission lease lost")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        result = self.get(submission_id)
        if result is None:  # pragma: no cover
            raise KeyError(submission_id)
        return result

    def _finish(
        self,
        submission_id: str,
        worker_id: str,
        lease_token: int,
        *,
        status: str,
        work_order_id: str | None = None,
        job_id: str | None = None,
        error: str = "",
    ) -> DocumentPlanSubmission:
        self._validate_status(status)
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_plan_submissions WHERE submission_id = ?",
                    (str(submission_id),),
                ).fetchone()
                if row is None:
                    raise KeyError(submission_id)
                updated = conn.execute(
                    """UPDATE document_plan_submissions
                       SET status = ?, work_order_id = COALESCE(?, work_order_id),
                           job_id = COALESCE(?, job_id), last_error = ?,
                           updated_at = ?, completed_at = CASE
                               WHEN ? IN ('dispatched', 'waiting_human') THEN ?
                               ELSE completed_at END,
                           lease_owner = NULL, lease_expires_at = NULL
                       WHERE submission_id = ? AND status = 'running'
                         AND lease_owner = ? AND lease_token = ?""",
                    (
                        status, work_order_id, job_id, str(error or "")[:1000],
                        _iso(now), status, _iso(now) if status in {"dispatched", "waiting_human"} else None,
                        str(submission_id), str(worker_id), int(lease_token),
                    ),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("document plan submission lease lost")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        result = self.get(submission_id)
        if result is None:  # pragma: no cover
            raise KeyError(submission_id)
        return result

    def mark_dispatched(
        self, submission_id: str, worker_id: str, lease_token: int,
        *, work_order_id: str, job_id: str,
    ) -> DocumentPlanSubmission:
        if not work_order_id or not job_id:
            raise ValueError("dispatched submission requires work order and job ids")
        return self._finish(
            submission_id, worker_id, lease_token, status="dispatched",
            work_order_id=work_order_id, job_id=job_id,
        )

    def mark_waiting_human(
        self, submission_id: str, worker_id: str, lease_token: int,
        *, work_order_id: str,
    ) -> DocumentPlanSubmission:
        if not work_order_id:
            raise ValueError("waiting submission requires a work order id")
        return self._finish(
            submission_id, worker_id, lease_token, status="waiting_human",
            work_order_id=work_order_id,
        )

    def fail(
        self,
        submission_id: str,
        worker_id: str,
        lease_token: int,
        message: str,
        *,
        retryable: bool = True,
        backoff_seconds: int | None = None,
    ) -> DocumentPlanSubmission:
        current = self.get(submission_id)
        if current is None:
            raise KeyError(submission_id)
        should_retry = retryable and current.attempt < current.max_attempts
        if should_retry:
            delay = max(1, int(backoff_seconds if backoff_seconds is not None else min(300, 2 ** max(0, current.attempt - 1))))
            status = "retrying"
            available = _now() + timedelta(seconds=delay)
            completed = None
        else:
            status = "dead_letter" if retryable else "failed"
            available = _now()
            completed = _now()
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
                    """UPDATE document_plan_submissions
                       SET status = ?, available_at = ?, last_error = ?,
                           updated_at = ?, completed_at = ?, lease_owner = NULL,
                           lease_expires_at = NULL
                       WHERE submission_id = ? AND status = 'running'
                         AND lease_owner = ? AND lease_token = ?""",
                    (
                        status, _iso(available), str(message or "")[:1000], _iso(now),
                        _iso(completed) if completed else None, str(submission_id),
                        str(worker_id), int(lease_token),
                    ),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("document plan submission lease lost")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        result = self.get(submission_id)
        if result is None:  # pragma: no cover
            raise KeyError(submission_id)
        return result


def _row_to_submission(row: sqlite3.Row) -> DocumentPlanSubmission:
    return DocumentPlanSubmission(
        submission_id=str(row["submission_id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        knowledge_base_name=str(row["knowledge_base_name"] or ""),
        output_spec_id=str(row["output_spec_id"]),
        output_spec_version=int(row["output_spec_version"]),
        output_spec_hash=str(row["output_spec_hash"]),
        document_plan_id=str(row["document_plan_id"]),
        document_plan_version=int(row["document_plan_version"]),
        document_plan_hash=str(row["document_plan_hash"]),
        source_snapshot_id=str(row["source_snapshot_id"]),
        source_snapshot_hash=str(row["source_snapshot_hash"]),
        client_request_id=str(row["client_request_id"]),
        status=str(row["status"]),
        attempt=int(row["attempt"] or 0),
        max_attempts=int(row["max_attempts"] or 1),
        available_at=str(row["available_at"]),
        lease_owner=row["lease_owner"],
        lease_token=int(row["lease_token"] or 0),
        lease_expires_at=row["lease_expires_at"],
        work_order_id=row["work_order_id"],
        job_id=row["job_id"],
        last_error=str(row["last_error"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=row["completed_at"],
    )


__all__ = [
    "DocumentPlanSubmission",
    "DocumentPlanSubmissionStore",
    "SUBMISSION_STATUSES",
]
