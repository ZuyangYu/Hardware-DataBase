"""Durable, lease-based document-authoring jobs used by chat tools.

The HTTP process only writes a job and its outbox record.  A worker later
claims the row and dispatches a small, allow-listed operation.  No callable or
request object is serialized into the database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import src.settings
from src.result_exports.models import ResourceLock


JOB_OPERATIONS = frozenset({"generate_work_order", "resume_work_order"})
JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "dead_letter"})
RESOURCE_LOCK_TYPES = frozenset({"project", "knowledge_base", "template", "work_order"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value


@dataclass
class DocumentAuthoringJob:
    job_id: str
    tenant_id: str
    user_id: str
    session_id: str
    client_request_id: str
    operation: str
    payload: dict[str, Any] = field(default_factory=dict)
    work_order_id: str | None = None
    status: str = "queued"
    attempt: int = 0
    max_attempts: int = 3
    available_at: str = field(default_factory=_iso)
    lease_owner: str | None = None
    lease_token: int = 0
    lease_expires_at: str | None = None
    resource_lock_token: int | None = None
    result: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    created_at: str = field(default_factory=_iso)
    updated_at: str = field(default_factory=_iso)
    completed_at: str | None = None

    @property
    def dead_letter(self) -> bool:
        return self.status == "dead_letter"


class DocumentAuthoringJobStore:
    """SQLite repository with atomic idempotent create/claim/lease methods."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or src.settings.AUTH_DB_PATH
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_authoring_jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    work_order_id TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    resource_lock_token INTEGER,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_authoring_jobs_idempotency
                    ON document_authoring_jobs(
                        tenant_id, user_id, session_id, client_request_id, operation
                    );
                CREATE INDEX IF NOT EXISTS idx_document_authoring_jobs_queue
                    ON document_authoring_jobs(status, available_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_document_authoring_jobs_chat_session
                    ON document_authoring_jobs(tenant_id, user_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS document_authoring_job_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_document_authoring_job_outbox_queue
                    ON document_authoring_job_outbox(status, available_at, created_at);
                CREATE TABLE IF NOT EXISTS document_resource_locks (
                    tenant_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL DEFAULT 1,
                    lease_expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, resource_type, resource_id)
                );
                CREATE INDEX IF NOT EXISTS idx_document_resource_locks_expiry
                    ON document_resource_locks(lease_expires_at);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(document_authoring_jobs)").fetchall()}
            if "resource_lock_token" not in columns:
                conn.execute("ALTER TABLE document_authoring_jobs ADD COLUMN resource_lock_token INTEGER")

    @staticmethod
    def _validate_operation(operation: str) -> str:
        normalized = str(operation or "").strip()
        if normalized not in JOB_OPERATIONS:
            raise ValueError("unsupported document authoring job operation")
        return normalized

    @staticmethod
    def _validate_status(status: str) -> str:
        if status not in JOB_STATUSES:
            raise ValueError("unsupported document authoring job status")
        return status

    @staticmethod
    def _validate_resource_scope(resource_type: str, resource_id: str) -> tuple[str, str]:
        normalized_type = str(resource_type or "").strip().lower()
        normalized_id = str(resource_id or "").strip()
        if normalized_type not in RESOURCE_LOCK_TYPES:
            raise ValueError("unsupported resource lock type")
        if not normalized_id or len(normalized_id) > 300:
            raise ValueError("resource lock id is required")
        return normalized_type, normalized_id

    @classmethod
    def _resource_scope_from_payload(cls, payload: dict[str, Any], work_order_id: str | None) -> tuple[str, str] | None:
        explicit = payload.get("resource_lock")
        if explicit is not None:
            if not isinstance(explicit, dict):
                raise ValueError("resource_lock must be an object")
            return cls._validate_resource_scope(explicit.get("type"), explicit.get("id"))
        # Prefer the broadest mutable scope supplied by the caller.  A
        # knowledge-base authoring job must not race a second work order that
        # writes the same KB, while independent KBs remain parallelizable.
        if payload.get("project_id") not in (None, ""):
            return cls._validate_resource_scope("project", payload["project_id"])
        if payload.get("knowledge_base_name") not in (None, ""):
            return cls._validate_resource_scope("knowledge_base", payload["knowledge_base_name"])
        if work_order_id not in (None, ""):
            return cls._validate_resource_scope("work_order", work_order_id)
        if payload.get("template_version_id") not in (None, ""):
            return cls._validate_resource_scope("template", payload["template_version_id"])
        return None

    @staticmethod
    def _job_lock_owner(job_id: str, attempt: int) -> str:
        return f"document-job:{job_id}:{int(attempt)}"

    @staticmethod
    def _resource_lock_expired(value: str | None, now: datetime) -> bool:
        if not value:
            return False
        try:
            expiry = datetime.fromisoformat(str(value))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry <= now
        except (TypeError, ValueError, OverflowError):
            # An unreadable lease is treated as active.  Taking a lock with an
            # ambiguous expiry could let two writers operate concurrently.
            return False

    @classmethod
    def _acquire_resource_lock_in_connection(
        cls,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> ResourceLock | None:
        tenant = str(tenant_id or "").strip()
        owner = str(owner_id or "").strip()
        if not tenant or not owner:
            raise ValueError("resource lock tenant and owner are required")
        resource_type, resource_id = cls._validate_resource_scope(resource_type, resource_id)
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        row = conn.execute(
            """SELECT * FROM document_resource_locks
               WHERE tenant_id = ? AND resource_type = ? AND resource_id = ?""",
            (tenant, resource_type, resource_id),
        ).fetchone()
        if row is None:
            token = 1
            conn.execute(
                """INSERT INTO document_resource_locks(
                       tenant_id, resource_type, resource_id, owner_id,
                       fencing_token, lease_expires_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (tenant, resource_type, resource_id, owner, token, _iso(expires), _iso(now), _iso(now)),
            )
        else:
            active = not cls._resource_lock_expired(row["lease_expires_at"], now)
            if active and row["owner_id"] != owner:
                return None
            token = int(row["fencing_token"] or 0) + (0 if row["owner_id"] == owner and active else 1)
            conn.execute(
                """UPDATE document_resource_locks
                   SET owner_id = ?, fencing_token = ?, lease_expires_at = ?, updated_at = ?
                   WHERE tenant_id = ? AND resource_type = ? AND resource_id = ?""",
                (owner, token, _iso(expires), _iso(now), tenant, resource_type, resource_id),
            )
        return ResourceLock(
            tenant_id=tenant,
            resource_type=resource_type,
            resource_id=resource_id,
            owner_id=owner,
            fencing_token=token,
            lease_expires_at=_iso(expires),
        )

    @classmethod
    def _release_resource_lock_in_connection(
        cls,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        resource_type, resource_id = cls._validate_resource_scope(resource_type, resource_id)
        cursor = conn.execute(
            """DELETE FROM document_resource_locks
               WHERE tenant_id = ? AND resource_type = ? AND resource_id = ?
                 AND owner_id = ? AND fencing_token = ?""",
            (str(tenant_id), resource_type, resource_id, str(owner_id), int(fencing_token)),
        )
        return cursor.rowcount == 1

    @classmethod
    def _release_job_resource_lock_in_connection(
        cls,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> bool:
        scope = cls._resource_scope_from_payload(
            _load(row["payload_json"], {}), row["work_order_id"]
        )
        if scope is None:
            return False
        resource_type, resource_id = scope
        return cls._release_resource_lock_in_connection(
            conn,
            tenant_id=str(row["tenant_id"]),
            resource_type=resource_type,
            resource_id=resource_id,
            owner_id=cls._job_lock_owner(row["job_id"], int(row["attempt"] or 0)),
            fencing_token=(
                int(row["resource_lock_token"])
                if "resource_lock_token" in row.keys() and row["resource_lock_token"] is not None
                else int(row["attempt"] or 0)
            ),
        )

    def acquire_resource_lock(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        lease_seconds: int = 60,
    ) -> ResourceLock | None:
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                lock = self._acquire_resource_lock_in_connection(
                    conn,
                    tenant_id=tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                    now=now,
                )
                conn.execute("COMMIT")
                return lock
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def renew_resource_lock(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        fencing_token: int,
        lease_seconds: int = 60,
    ) -> ResourceLock:
        resource_type, resource_id = self._validate_resource_scope(resource_type, resource_id)
        now = _now()
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT lease_expires_at FROM document_resource_locks
                       WHERE tenant_id = ? AND resource_type = ? AND resource_id = ?
                         AND owner_id = ? AND fencing_token = ?""",
                    (str(tenant_id), resource_type, resource_id, str(owner_id), int(fencing_token)),
                ).fetchone()
                if row is None or self._resource_lock_expired(row["lease_expires_at"], now):
                    raise RuntimeError("resource lock lost")
                cursor = conn.execute(
                    """UPDATE document_resource_locks
                       SET lease_expires_at = ?, updated_at = ?
                       WHERE tenant_id = ? AND resource_type = ? AND resource_id = ?
                         AND owner_id = ? AND fencing_token = ?""",
                    (_iso(expires), _iso(now), str(tenant_id), resource_type, resource_id,
                     str(owner_id), int(fencing_token)),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("resource lock lost")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return ResourceLock(
            tenant_id=str(tenant_id), resource_type=resource_type, resource_id=resource_id,
            owner_id=str(owner_id), fencing_token=int(fencing_token), lease_expires_at=_iso(expires),
        )

    def release_resource_lock(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                released = self._release_resource_lock_in_connection(
                    conn,
                    tenant_id=tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    owner_id=owner_id,
                    fencing_token=fencing_token,
                )
                conn.execute("COMMIT")
                return released
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def create_job(
        self,
        *,
        tenant_id: str,
        user_id: str | int,
        session_id: str | int,
        client_request_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        work_order_id: str | None = None,
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> DocumentAuthoringJob:
        operation = self._validate_operation(operation)
        tenant_id = str(tenant_id or "").strip()
        user_id = str(user_id or "").strip()
        session_id = str(session_id or "").strip()
        client_request_id = str(client_request_id or "").strip()[:128]
        if not tenant_id or not user_id or not session_id or not client_request_id:
            raise ValueError("durable document jobs require tenant, user, session and client request ids")
        if not isinstance(payload or {}, dict):
            raise ValueError("document authoring job payload must be an object")
        max_attempts = max(1, min(int(max_attempts), 20))
        now = _now()
        available = (available_at or now).astimezone(timezone.utc)
        payload_value = dict(payload or {})
        # Validate the optional explicit scope before writing an idempotent
        # record.  Inferred scopes are checked again atomically at claim time.
        self._resource_scope_from_payload(payload_value, work_order_id)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """SELECT * FROM document_authoring_jobs
                       WHERE tenant_id = ? AND user_id = ? AND session_id = ?
                         AND client_request_id = ? AND operation = ?""",
                    (tenant_id, user_id, session_id, client_request_id, operation),
                ).fetchone()
                if existing is not None:
                    current = _row_to_job(existing)
                    if current.payload != payload_value or current.work_order_id != work_order_id:
                        raise ValueError("document authoring job idempotency key conflicts with existing payload")
                    conn.execute("COMMIT")
                    return current
                job_id = f"document-job-{uuid.uuid4().hex}"
                conn.execute(
                    """INSERT INTO document_authoring_jobs (
                           job_id, tenant_id, user_id, session_id, client_request_id,
                           operation, work_order_id, status, attempt, max_attempts,
                           available_at, payload_json, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)""",
                    (
                        job_id, tenant_id, user_id, session_id, client_request_id,
                        operation, work_order_id, max_attempts, _iso(available),
                        _json(payload_value), _iso(now), _iso(now),
                    ),
                )
                conn.execute(
                    """INSERT INTO document_authoring_job_outbox (
                           outbox_id, job_id, operation, available_at, created_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (f"document-outbox-{uuid.uuid4().hex}", job_id, operation, _iso(available), _iso(now)),
                )
                row = conn.execute(
                    "SELECT * FROM document_authoring_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return _row_to_job(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get(self, job_id: str) -> DocumentAuthoringJob | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_authoring_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def get_by_idempotency(
        self, tenant_id: str, user_id: str | int, session_id: str | int,
        client_request_id: str, operation: str,
    ) -> DocumentAuthoringJob | None:
        operation = self._validate_operation(operation)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM document_authoring_jobs
                   WHERE tenant_id = ? AND user_id = ? AND session_id = ?
                     AND client_request_id = ? AND operation = ?""",
                (str(tenant_id), str(user_id), str(session_id), str(client_request_id), operation),
            ).fetchone()
        return _row_to_job(row) if row else None

    def get_by_work_order(
        self,
        work_order_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | int | None = None,
    ) -> DocumentAuthoringJob | None:
        """Return the newest durable job for a frozen work order.

        Status requests commonly arrive after a browser refresh and therefore
        do not necessarily carry the original client-request id.  The query is
        still optionally scoped by tenant/user so callers cannot turn this
        convenience lookup into a cross-tenant job oracle.
        """
        clauses = ["work_order_id = ?"]
        params: list[Any] = [str(work_order_id)]
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(str(tenant_id))
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_authoring_jobs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, job_id DESC LIMIT 1",
                params,
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_chat_session_jobs(
        self,
        *,
        user_id: str | int,
        session_id: str | int | None = None,
        tenant_id: str | None = None,
        limit: int = 64,
    ) -> list[DocumentAuthoringJob]:
        """List document jobs owned by one chat user/session.

        The chat UI uses this durable projection after a route change or a
        browser restart.  Scope is applied in SQL before any work-order status
        is projected, so a task from another user/session cannot become a
        downloadable card by accident.
        """
        clauses = ["user_id = ?"]
        params: list[Any] = [str(user_id)]
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(str(tenant_id))
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        params.append(max(1, min(int(limit), 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM document_authoring_jobs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, job_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def queue_state(self) -> tuple[int, float]:
        """Return queued/lease-expired depth and oldest creation age."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS depth, MIN(created_at) AS oldest
                   FROM document_authoring_jobs
                   WHERE (status = 'queued' AND datetime(available_at) <= datetime('now'))
                      OR (status = 'running' AND lease_expires_at IS NOT NULL
                          AND datetime(lease_expires_at) <= datetime('now'))"""
            ).fetchone()
        if row is None or not row["oldest"]:
            return int(row["depth"] if row else 0), 0.0
        try:
            age = max(
                0.0,
                _now().timestamp() - datetime.fromisoformat(
                    str(row["oldest"]).replace("Z", "+00:00")
                ).timestamp(),
            )
        except (TypeError, ValueError, OverflowError):
            age = 0.0
        return int(row["depth"] or 0), age

    def list_pending(self, limit: int = 16) -> list[DocumentAuthoringJob]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM document_authoring_jobs
                   WHERE (status = 'queued' AND datetime(available_at) <= datetime('now'))
                      OR (status = 'running' AND lease_expires_at IS NOT NULL
                          AND datetime(lease_expires_at) <= datetime('now'))
                   ORDER BY created_at, job_id LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def claim(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> DocumentAuthoringJob | None:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        now = _now()
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_authoring_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None or row["status"] in {"succeeded", "failed", "cancelled", "dead_letter"}:
                    conn.execute("COMMIT")
                    return None
                available = row["status"] == "queued" and str(row["available_at"]) <= _iso(now)
                expired = (
                    row["status"] == "running"
                    and row["lease_expires_at"]
                    and str(row["lease_expires_at"]) <= _iso(now)
                )
                if not (available or expired):
                    conn.execute("COMMIT")
                    return None
                next_attempt = int(row["attempt"] or 0) + 1
                if next_attempt > int(row["max_attempts"] or 1):
                    if row["status"] == "running":
                        self._release_job_resource_lock_in_connection(conn, row)
                    conn.execute(
                        """UPDATE document_authoring_jobs
                           SET status = 'dead_letter', last_error = ?, updated_at = ?,
                               lease_owner = NULL, lease_expires_at = NULL, completed_at = ?
                           WHERE job_id = ?""",
                        ("maximum_attempts_exceeded", _iso(now), _iso(now), job_id),
                    )
                    conn.execute("COMMIT")
                    return None
                scope = self._resource_scope_from_payload(
                    _load(row["payload_json"], {}), row["work_order_id"]
                )
                if scope is not None:
                    if expired:
                        # The job lease is the authoritative liveness signal.
                        # Remove the previous attempt's lock before acquiring
                        # the next fencing token; this also repairs legacy
                        # rows whose lock lease was not heartbeated yet.
                        self._release_resource_lock_in_connection(
                            conn,
                            tenant_id=str(row["tenant_id"]),
                            resource_type=scope[0],
                            resource_id=scope[1],
                            owner_id=self._job_lock_owner(job_id, int(row["attempt"] or 0)),
                            fencing_token=(
                                int(row["resource_lock_token"])
                                if "resource_lock_token" in row.keys() and row["resource_lock_token"] is not None
                                else int(row["attempt"] or 0)
                            ),
                        )
                    lock = self._acquire_resource_lock_in_connection(
                        conn,
                        tenant_id=str(row["tenant_id"]),
                        resource_type=scope[0],
                        resource_id=scope[1],
                        owner_id=self._job_lock_owner(job_id, next_attempt),
                        lease_seconds=lease_seconds,
                        now=now,
                    )
                    if lock is None:
                        conn.execute("COMMIT")
                        return None
                    resource_lock_token = lock.fencing_token
                else:
                    resource_lock_token = None
                conn.execute(
                    """UPDATE document_authoring_jobs
                       SET status = 'running', attempt = ?, lease_owner = ?,
                           lease_token = lease_token + 1, lease_expires_at = ?,
                           resource_lock_token = ?, updated_at = ?, last_error = ''
                       WHERE job_id = ?""",
                    (next_attempt, worker_id, _iso(expires), resource_lock_token, _iso(now), job_id),
                )
                claimed = conn.execute(
                    "SELECT * FROM document_authoring_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return _row_to_job(claimed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def heartbeat(self, job_id: str, worker_id: str, lease_token: int, lease_seconds: int = 60) -> DocumentAuthoringJob:
        now = _now()
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_authoring_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                cursor = conn.execute(
                    """UPDATE document_authoring_jobs
                       SET lease_expires_at = ?, updated_at = ?
                       WHERE job_id = ? AND status = 'running'
                         AND lease_owner = ? AND lease_token = ?""",
                    (_iso(expires), _iso(now), job_id, worker_id, int(lease_token)),
                )
                if cursor.rowcount != 1 or row is None:
                    raise RuntimeError("document authoring job lease lost")
                scope = self._resource_scope_from_payload(
                    _load(row["payload_json"], {}), row["work_order_id"]
                )
                if scope is not None:
                    lock_cursor = conn.execute(
                        """UPDATE document_resource_locks
                           SET lease_expires_at = ?, updated_at = ?
                           WHERE tenant_id = ? AND resource_type = ? AND resource_id = ?
                             AND owner_id = ? AND fencing_token = ?""",
                        (
                            _iso(expires), _iso(now), str(row["tenant_id"]), scope[0], scope[1],
                            self._job_lock_owner(job_id, int(row["attempt"] or 0)),
                            (
                                int(row["resource_lock_token"])
                                if "resource_lock_token" in row.keys() and row["resource_lock_token"] is not None
                                else int(row["attempt"] or 0)
                            ),
                        ),
                    )
                    if lock_cursor.rowcount != 1:
                        raise RuntimeError("document resource lock lost")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        job = self.get(job_id)
        if job is None:  # pragma: no cover - guarded by the update
            raise KeyError(job_id)
        return job

    def complete(
        self, job_id: str, worker_id: str, lease_token: int, result: dict[str, Any] | None = None,
    ) -> DocumentAuthoringJob:
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_authoring_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                cursor = conn.execute(
                    """UPDATE document_authoring_jobs
                       SET status = 'succeeded', result_json = ?, updated_at = ?, completed_at = ?,
                           lease_owner = NULL, lease_expires_at = NULL
                       WHERE job_id = ? AND status = 'running'
                         AND lease_owner = ? AND lease_token = ?""",
                    (_json(result or {}), _iso(now), _iso(now), job_id, worker_id, int(lease_token)),
                )
                if cursor.rowcount != 1 or row is None:
                    raise RuntimeError("document authoring job lease lost")
                self._release_job_resource_lock_in_connection(conn, row)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        job = self.get(job_id)
        if job is None:  # pragma: no cover
            raise KeyError(job_id)
        self._mark_outbox(job_id, "sent")
        return job

    def fail(
        self,
        job_id: str,
        worker_id: str,
        lease_token: int,
        message: str,
        *,
        retryable: bool = True,
        backoff_seconds: int | None = None,
    ) -> DocumentAuthoringJob:
        now = _now()
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        should_retry = retryable and current.attempt < current.max_attempts
        if should_retry:
            delay = max(1, int(backoff_seconds if backoff_seconds is not None else min(300, 2 ** max(0, current.attempt - 1))))
            status = "queued"
            available = now + timedelta(seconds=delay)
            completed = None
        else:
            status = "dead_letter" if retryable else "failed"
            available = now
            completed = now
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_authoring_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                cursor = conn.execute(
                    """UPDATE document_authoring_jobs
                       SET status = ?, available_at = ?, last_error = ?, updated_at = ?,
                           completed_at = ?, lease_owner = NULL, lease_expires_at = NULL
                       WHERE job_id = ? AND status = 'running'
                         AND lease_owner = ? AND lease_token = ?""",
                    (
                        status, _iso(available), str(message or "")[:1000], _iso(now),
                        _iso(completed) if completed else None, job_id, worker_id, int(lease_token),
                    ),
                )
                if cursor.rowcount != 1 or row is None:
                    raise RuntimeError("document authoring job lease lost")
                self._release_job_resource_lock_in_connection(conn, row)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        job = self.get(job_id)
        if job is None:  # pragma: no cover
            raise KeyError(job_id)
        if status != "queued":
            self._mark_outbox(job_id, "failed", error=str(message or "")[:1000])
        return job

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> DocumentAuthoringJob | None:
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                """UPDATE document_authoring_jobs
                   SET status = 'cancelled', last_error = ?, updated_at = ?, completed_at = ?,
                       lease_owner = NULL, lease_expires_at = NULL
                   WHERE job_id = ? AND status IN ('queued', 'running')""",
                (str(reason or "cancelled")[:1000], _iso(now), _iso(now), job_id),
            )
        return self.get(job_id)

    def list_pending_outbox(self, limit: int = 32) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM document_authoring_job_outbox
                   WHERE status = 'pending' AND datetime(available_at) <= datetime('now')
                   ORDER BY created_at, outbox_id LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _mark_outbox(self, job_id: str, status: str, *, error: str = "") -> None:
        if status not in {"sent", "failed", "pending"}:
            raise ValueError("unsupported document job outbox status")
        now = _iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """UPDATE document_authoring_job_outbox
                   SET status = ?, attempt = attempt + 1, last_error = ?, dispatched_at = ?
                   WHERE job_id = ?""",
                (status, error[:1000], now if status == "sent" else None, job_id),
            )


def _row_to_job(row: sqlite3.Row) -> DocumentAuthoringJob:
    return DocumentAuthoringJob(
        job_id=str(row["job_id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        client_request_id=str(row["client_request_id"]),
        operation=str(row["operation"]),
        payload=_load(row["payload_json"], {}),
        work_order_id=row["work_order_id"],
        status=str(row["status"]),
        attempt=int(row["attempt"] or 0),
        max_attempts=int(row["max_attempts"] or 1),
        available_at=str(row["available_at"]),
        lease_owner=row["lease_owner"],
        lease_token=int(row["lease_token"] or 0),
        lease_expires_at=row["lease_expires_at"],
        resource_lock_token=(
            int(row["resource_lock_token"])
            if "resource_lock_token" in row.keys() and row["resource_lock_token"] is not None
            else None
        ),
        result=_load(row["result_json"], {}),
        last_error=str(row["last_error"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=row["completed_at"],
    )


__all__ = [
    "DocumentAuthoringJob",
    "DocumentAuthoringJobStore",
    "JOB_OPERATIONS",
    "JOB_STATUSES",
    "RESOURCE_LOCK_TYPES",
    "ResourceLock",
]
