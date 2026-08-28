"""Canonical control-plane storage for long-term memory.

The LangGraph store is deliberately treated as a rebuildable projection.  All
identity, authorization, lifecycle, provenance, consent, and idempotency
information lives in this module's SQLite ledger.  The repository accepts an
existing ``sqlite3.Connection`` so Conversation can add jobs and outbox rows
in the same transaction as ``complete_turn``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from src.memory.schemas import MEMORY_SCHEMA_VERSION, content_hash, normalized_content


MEMORY_STATUSES = {
    "candidate",
    "verification_pending",
    "supersede_pending",
    "needs_rebuild",
    "verified",
    "superseded",
    "rejected",
    "deleted",
    "provenance_missing",
}
ACTIVE_MEMORY_STATUSES = {"candidate", "verified"}
PROJECTION_KINDS = {"candidate", "verified"}
OUTBOX_PENDING_STATUSES = {"pending", "retrying"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def namespace_for_project(department_id: str | int | None, kb_id: str | int | None, kind: str) -> tuple[str, ...]:
    """Build a complete project namespace and reject empty scope values."""
    if department_id in (None, "") or kb_id in (None, ""):
        raise ValueError("project memory requires a non-empty department_id and kb_id")
    if kind not in PROJECTION_KINDS:
        raise ValueError(f"unsupported memory projection kind: {kind}")
    return ("hdb", "department", str(department_id), "kb", str(kb_id), kind)


def namespace_for_user(user_id: str | int | None, kind: str) -> tuple[str, ...]:
    """Build the isolated user namespace without accepting ``None``."""
    if user_id in (None, ""):
        raise ValueError("user memory requires a non-empty user_id")
    if kind not in PROJECTION_KINDS:
        raise ValueError(f"unsupported memory projection kind: {kind}")
    return ("hdb", "user", str(user_id), kind)


def namespace_json(namespace: tuple[str, ...]) -> str:
    if not namespace or any(part in (None, "") for part in namespace):
        raise ValueError("memory namespace must be complete and non-empty")
    return json_dumps(list(namespace))


def scope_fingerprint(
    *,
    scope: str,
    user_id: str | int | None = None,
    department_id: str | int | None = None,
    kb_id: str | int | None = None,
) -> str:
    if scope not in {"user", "project"}:
        raise ValueError("memory scope must be user or project")
    if scope == "user":
        payload = {"scope": scope, "user_id": str(user_id) if user_id not in (None, "") else None}
    else:
        if department_id in (None, "") or kb_id in (None, ""):
            raise ValueError("project scope requires department_id and kb_id")
        payload = {"scope": scope, "department_id": str(department_id), "kb_id": str(kb_id)}
    return hashlib.sha256(normalized_content(payload)).hexdigest()


def ensure_memory_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the memory control plane on an existing connection.

    This migration is intentionally idempotent and contains no destructive
    statements.  Source ledger rows do not use cascading foreign keys to chat
    rows: conversation deletion first invalidates and queues them, then removes
    the raw messages while preserving the provenance/tombstone evidence.
    """
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_records (
            memory_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL CHECK(scope IN ('user', 'project')),
            user_id TEXT,
            department_id TEXT,
            kb_id TEXT,
            status TEXT NOT NULL CHECK(status IN (
                'candidate', 'verification_pending', 'supersede_pending',
                'needs_rebuild', 'verified', 'superseded', 'rejected',
                'deleted', 'provenance_missing'
            )),
            current_revision INTEGER NOT NULL DEFAULT 0 CHECK(current_revision >= 0),
            content_hash TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            title TEXT NOT NULL,
            subject TEXT,
            content_json TEXT NOT NULL,
            extractor_model TEXT NOT NULL DEFAULT '',
            extractor_version TEXT NOT NULL DEFAULT '',
            schema_version TEXT NOT NULL DEFAULT '1',
            replacement_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_records_scope_status ON memory_records(scope, user_id, department_id, kb_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_records_hash ON memory_records(content_hash)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_revisions (
            revision_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            revision_no INTEGER NOT NULL CHECK(revision_no > 0),
            projection_id TEXT,
            before_content_json TEXT,
            after_content_json TEXT,
            operation TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(memory_id, revision_no)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory ON memory_revisions(memory_id, revision_no)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_sources (
            source_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            session_id INTEGER,
            turn_id TEXT,
            message_id INTEGER,
            consent_event_id TEXT,
            source_hash TEXT NOT NULL,
            source_role TEXT NOT NULL DEFAULT '',
            contribution_kind TEXT NOT NULL DEFAULT 'extracted',
            source_valid INTEGER NOT NULL DEFAULT 1,
            invalidated_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(memory_id, session_id, turn_id, message_id, consent_event_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_sources_source ON memory_sources(session_id, turn_id, message_id, source_valid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_sources_memory ON memory_sources(memory_id, source_valid)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_projections (
            projection_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            projection_kind TEXT NOT NULL CHECK(projection_kind IN ('candidate', 'verified')),
            store_backend TEXT NOT NULL,
            namespace_json TEXT NOT NULL,
            store_key TEXT NOT NULL,
            current_revision INTEGER NOT NULL DEFAULT 0,
            current_content_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            manager_writable INTEGER NOT NULL DEFAULT 0,
            fence_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            retired_at TEXT,
            UNIQUE(store_backend, namespace_json, store_key)
        )
        """
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_active_projection
        ON memory_projections(memory_id, projection_kind) WHERE retired_at IS NULL"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_projections_acl ON memory_projections(projection_kind, active, retired_at, namespace_json)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_consent_events (
            consent_event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            consent_kind TEXT NOT NULL CHECK(consent_kind = 'user_memory_extract'),
            policy_version TEXT NOT NULL,
            consent_revoke_generation INTEGER NOT NULL DEFAULT 0,
            granted_at TEXT NOT NULL,
            revoked_at TEXT,
            authorized_start_turn_id TEXT NOT NULL,
            authorized_start_message_id INTEGER NOT NULL,
            authorized_end_turn_id TEXT NOT NULL,
            authorized_end_message_id INTEGER NOT NULL,
            authorized_source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_consent_user ON memory_consent_events(user_id, revoked_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_consent_source_items (
            consent_event_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            turn_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            PRIMARY KEY(consent_event_id, ordinal),
            UNIQUE(consent_event_id, turn_id, message_id)
        )
        """
    )
    # A consent manifest is a server-derived authorization boundary.  It may
    # only transition from active to revoked; its source window is immutable.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_memory_consent_immutable_update
        BEFORE UPDATE OF user_id, session_id, turn_id, message_id, consent_kind,
            policy_version, consent_revoke_generation, granted_at,
            authorized_start_turn_id, authorized_start_message_id,
            authorized_end_turn_id, authorized_end_message_id,
            authorized_source_hash ON memory_consent_events
        WHEN NEW.user_id != OLD.user_id
          OR NEW.session_id != OLD.session_id
          OR NEW.turn_id != OLD.turn_id
          OR NEW.message_id != OLD.message_id
          OR NEW.consent_kind != OLD.consent_kind
          OR NEW.policy_version != OLD.policy_version
          OR NEW.consent_revoke_generation != OLD.consent_revoke_generation
          OR NEW.granted_at != OLD.granted_at
          OR NEW.authorized_start_turn_id != OLD.authorized_start_turn_id
          OR NEW.authorized_start_message_id != OLD.authorized_start_message_id
          OR NEW.authorized_end_turn_id != OLD.authorized_end_turn_id
          OR NEW.authorized_end_message_id != OLD.authorized_end_message_id
          OR NEW.authorized_source_hash != OLD.authorized_source_hash
        BEGIN
            SELECT RAISE(ABORT, 'memory consent authorization fields are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_memory_consent_items_no_update
        BEFORE UPDATE ON memory_consent_source_items
        BEGIN
            SELECT RAISE(ABORT, 'memory consent source manifest is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_memory_consent_items_no_delete
        BEFORE DELETE ON memory_consent_source_items
        BEGIN
            SELECT RAISE(ABORT, 'memory consent source manifest is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_memory_settings (
            user_id TEXT PRIMARY KEY,
            opt_in INTEGER NOT NULL DEFAULT 0,
            policy_version TEXT NOT NULL DEFAULT 'v1',
            revoke_generation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_jobs (
            job_id TEXT PRIMARY KEY,
            session_id INTEGER NOT NULL,
            job_kind TEXT NOT NULL CHECK(job_kind IN ('project_reflection', 'user_reflection')),
            scope_fingerprint TEXT NOT NULL,
            consent_event_id TEXT,
            consent_policy_version TEXT,
            consent_revoke_generation INTEGER,
            target_turn_id TEXT NOT NULL,
            target_message_id INTEGER NOT NULL,
            source_start_turn_id TEXT,
            source_start_message_id INTEGER,
            authorized_start_turn_id TEXT,
            authorized_start_message_id INTEGER,
            authorized_end_turn_id TEXT,
            authorized_end_message_id INTEGER,
            authorized_source_hash TEXT,
            generation INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'retrying', 'completed', 'cancelled', 'dead_letter')),
            requested_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            next_retry_at TEXT,
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            claimed_at TEXT,
            completed_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(consent_event_id) REFERENCES memory_consent_events(consent_event_id),
            CHECK((job_kind = 'project_reflection' AND consent_event_id IS NULL) OR (job_kind = 'user_reflection' AND consent_event_id IS NOT NULL)),
            CHECK((job_kind = 'project_reflection' AND consent_revoke_generation IS NULL) OR (job_kind = 'user_reflection' AND consent_revoke_generation IS NOT NULL)),
            CHECK((job_kind = 'project_reflection' AND consent_policy_version IS NULL) OR (job_kind = 'user_reflection' AND consent_policy_version IS NOT NULL)),
            CHECK(
                (job_kind = 'project_reflection'
                 AND authorized_start_turn_id IS NULL AND authorized_start_message_id IS NULL
                 AND authorized_end_turn_id IS NULL AND authorized_end_message_id IS NULL
                 AND authorized_source_hash IS NULL)
                OR
                (job_kind = 'user_reflection'
                 AND source_start_turn_id IS NULL AND source_start_message_id IS NULL
                 AND authorized_start_turn_id IS NOT NULL AND authorized_start_message_id IS NOT NULL
                 AND authorized_end_turn_id IS NOT NULL AND authorized_end_message_id IS NOT NULL
                 AND authorized_source_hash IS NOT NULL)
            )
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_jobs_project_session ON memory_jobs(session_id) WHERE job_kind = 'project_reflection'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_jobs_user_consent ON memory_jobs(consent_event_id) WHERE consent_event_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_jobs_claim ON memory_jobs(status, available_at, next_retry_at, lease_expires_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_checkpoints (
            job_id TEXT PRIMARY KEY,
            committed_turn_id TEXT,
            committed_message_id INTEGER,
            committed_generation INTEGER NOT NULL DEFAULT 0,
            source_fingerprint TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES memory_jobs(job_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_reflection_runs (
            run_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            job_kind TEXT NOT NULL,
            consent_event_id TEXT,
            run_key TEXT NOT NULL UNIQUE,
            consent_revoke_generation INTEGER,
            authorized_start_turn_id TEXT,
            authorized_start_message_id INTEGER,
            authorized_end_turn_id TEXT,
            authorized_end_message_id INTEGER,
            authorized_source_hash TEXT,
            source_snapshot_hash TEXT NOT NULL,
            encrypted_source_snapshot_ref TEXT,
            extractor_model TEXT NOT NULL DEFAULT '',
            extractor_version TEXT NOT NULL DEFAULT '',
            schema_version TEXT NOT NULL DEFAULT '1',
            output_payload_json TEXT,
            output_hash TEXT,
            status TEXT NOT NULL CHECK(status IN ('prepared', 'output_persisted', 'catalog_committed', 'projected', 'failed')),
            created_at TEXT NOT NULL,
            committed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_runs_job ON memory_reflection_runs(job_id, generation)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_run_items (
            run_id TEXT NOT NULL,
            item_ordinal INTEGER NOT NULL CHECK(item_ordinal >= 0),
            output_item_hash TEXT NOT NULL,
            planned_action TEXT NOT NULL,
            memory_id TEXT,
            revision_no INTEGER,
            projection_id TEXT,
            expected_fence INTEGER,
            status TEXT NOT NULL DEFAULT 'planned',
            error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id, item_ordinal)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_projection_outbox (
            operation_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            projection_id TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('put', 'delete')),
            expected_revision INTEGER NOT NULL,
            expected_fence INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'retrying', 'completed', 'dead_letter')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_projection_outbox_claim ON memory_projection_outbox(status, next_retry_at, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_deletion_outbox (
            operation_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            source_or_consent_id TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('delete_projection', 'rebuild', 'redact')),
            expected_revision INTEGER NOT NULL,
            expected_fence INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'retrying', 'completed', 'dead_letter')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_deletion_outbox_claim ON memory_deletion_outbox(status, next_retry_at, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_audit_events (
            audit_event_id TEXT PRIMARY KEY,
            memory_id TEXT,
            operation_id TEXT,
            event_type TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            request_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_audit_memory ON memory_audit_events(memory_id, created_at)")


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    scope: str
    user_id: str | None
    department_id: str | None
    kb_id: str | None
    status: str
    current_revision: int
    content_hash: str
    memory_type: str
    title: str
    subject: str | None
    content: dict[str, Any]
    extractor_model: str
    extractor_version: str
    schema_version: str
    replacement_id: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result = {
            "id": self.memory_id,
            "memory_id": self.memory_id,
            "scope": self.scope,
            "status": self.status,
            "revision": self.current_revision,
            "content_hash": self.content_hash,
            "type": self.memory_type,
            "title": self.title,
            "subject": self.subject,
            "has_provenance": True,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "replacement_id": self.replacement_id,
        }
        if include_content:
            result["content"] = self.content.get("content", "")
            result["memory"] = self.content
        return result


@dataclass(frozen=True)
class MemoryProjection:
    projection_id: str
    memory_id: str
    projection_kind: str
    store_backend: str
    namespace: tuple[str, ...]
    store_key: str
    current_revision: int
    current_content_hash: str
    active: bool
    manager_writable: bool
    fence_version: int
    retired_at: str | None


@dataclass(frozen=True)
class MemoryJob:
    job_id: str
    session_id: int
    job_kind: str
    scope_fingerprint: str
    consent_event_id: str | None
    consent_policy_version: str | None
    consent_revoke_generation: int | None
    target_turn_id: str
    target_message_id: int
    source_start_turn_id: str | None
    source_start_message_id: int | None
    authorized_start_turn_id: str | None
    authorized_start_message_id: int | None
    authorized_end_turn_id: str | None
    authorized_end_message_id: int | None
    authorized_source_hash: str | None
    generation: int
    status: str
    requested_at: str
    available_at: str
    next_retry_at: str | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None
    claimed_at: str | None
    completed_at: str | None
    retry_count: int
    last_error: str


class MemoryCatalogRepository:
    """SQLite repository for Catalog and durable memory control-plane state."""

    def __init__(self, db_path: str | None = None, *, conn: sqlite3.Connection | None = None):
        self.db_path = db_path
        self._external_conn = conn
        if conn is None:
            if not db_path:
                raise ValueError("db_path is required when conn is not supplied")
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            self._conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA foreign_keys=ON")
        else:
            self._conn = conn
            if self._conn.row_factory is None:
                self._conn.row_factory = sqlite3.Row
        ensure_memory_schema(self._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        if not self._external_conn:
            self._conn.close()

    def transaction(self):
        return _Transaction(self._conn)

    def get_record(self, memory_id: str, *, conn: sqlite3.Connection | None = None) -> MemoryRecord | None:
        c = conn or self._conn
        row = c.execute("SELECT * FROM memory_records WHERE memory_id = ?", (str(memory_id),)).fetchone()
        return row_to_record(row) if row else None

    def list_records(
        self,
        *,
        scope: str | None = None,
        user_id: str | int | None = None,
        department_id: str | int | None = None,
        kb_id: str | int | None = None,
        statuses: Iterable[str] = ACTIVE_MEMORY_STATUSES,
        limit: int = 100,
        offset: int = 0,
        conn: sqlite3.Connection | None = None,
    ) -> list[MemoryRecord]:
        c = conn or self._conn
        where: list[str] = []
        params: list[Any] = []
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if scope == "user":
            where.append("user_id = ?")
            params.append(str(user_id))
        if scope == "project":
            where.extend(["department_id = ?", "kb_id = ?"])
            params.extend([str(department_id), str(kb_id)])
        values = tuple(statuses)
        if values:
            where.append("status IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        rows = c.execute(
            f"SELECT * FROM memory_records{clause} ORDER BY CASE status WHEN 'verified' THEN 0 ELSE 1 END, updated_at DESC, memory_id LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [row_to_record(row) for row in rows]

    def get_projections(
        self,
        *,
        scope: str,
        user_id: str | int | None = None,
        department_id: str | int | None = None,
        kb_id: str | int | None = None,
        kinds: Iterable[str] = PROJECTION_KINDS,
        active_only: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> list[MemoryProjection]:
        c = conn or self._conn
        projection_kinds = tuple(kinds)
        if not projection_kinds:
            return []
        conditions = ["p.projection_kind IN (" + ",".join("?" for _ in projection_kinds) + ")"]
        params: list[Any] = list(projection_kinds)
        conditions.append("r.scope = ?")
        params.append(scope)
        if scope == "user":
            conditions.append("r.user_id = ?")
            params.append(str(user_id))
        elif scope == "project":
            conditions.extend(["r.department_id = ?", "r.kb_id = ?"])
            params.extend([str(department_id), str(kb_id)])
        if active_only:
            conditions.extend(["p.active = 1", "p.retired_at IS NULL", "r.status IN ('candidate', 'verified')"])
        rows = c.execute(
            "SELECT p.* FROM memory_projections p JOIN memory_records r ON r.memory_id = p.memory_id WHERE "
            + " AND ".join(conditions),
            params,
        ).fetchall()
        return [row_to_projection(row) for row in rows]

    def get_projection(self, projection_id: str, *, conn: sqlite3.Connection | None = None) -> MemoryProjection | None:
        c = conn or self._conn
        row = c.execute("SELECT * FROM memory_projections WHERE projection_id = ?", (projection_id,)).fetchone()
        return row_to_projection(row) if row else None

    def get_projection_by_key(
        self, store_backend: str, namespace: tuple[str, ...], store_key: str, *, conn: sqlite3.Connection | None = None
    ) -> MemoryProjection | None:
        c = conn or self._conn
        row = c.execute(
            "SELECT * FROM memory_projections WHERE store_backend = ? AND namespace_json = ? AND store_key = ?",
            (store_backend, namespace_json(namespace), str(store_key)),
        ).fetchone()
        return row_to_projection(row) if row else None

    def get_sources(self, memory_id: str, *, valid_only: bool = False, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        c = conn or self._conn
        sql = "SELECT * FROM memory_sources WHERE memory_id = ?"
        params: list[Any] = [memory_id]
        if valid_only:
            sql += " AND source_valid = 1"
        sql += " ORDER BY created_at, source_id"
        return c.execute(sql, params).fetchall()

    def audit(
        self,
        event_type: str,
        *,
        memory_id: str | None = None,
        operation_id: str | None = None,
        actor_id: str = "",
        evidence_refs: Any = None,
        request_id: str = "",
        metadata: Any = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        c = conn or self._conn
        audit_id = str(uuid.uuid4())
        c.execute(
            """INSERT INTO memory_audit_events
            (audit_event_id, memory_id, operation_id, event_type, actor_id, evidence_refs_json, request_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, memory_id, operation_id, event_type, str(actor_id or ""), json_dumps(evidence_refs or []), str(request_id or ""), json_dumps(metadata or {}), utc_now()),
        )
        return audit_id

    def audit_events(self, memory_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM memory_audit_events WHERE memory_id = ? ORDER BY created_at DESC LIMIT ?",
            (memory_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [
            {
                "audit_event_id": row["audit_event_id"],
                "memory_id": row["memory_id"],
                "operation_id": row["operation_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "evidence_refs": json_loads(row["evidence_refs_json"], []),
                "request_id": row["request_id"],
                "metadata": json_loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def prepare_candidate(
        self,
        *,
        content: dict[str, Any],
        scope: str,
        user_id: str | int | None = None,
        department_id: str | int | None = None,
        kb_id: str | int | None = None,
        memory_id: str | None = None,
        source_refs: Iterable[dict[str, Any]] = (),
        actor_id: str = "memory-worker",
        reason: str = "reflection",
        expected_fence: int | None = None,
        projection_key: str | None = None,
        store_backend: str = "sqlite",
        extractor_model: str = "",
        extractor_version: str = "",
        schema_version: str = MEMORY_SCHEMA_VERSION,
        revision_operation: str | None = None,
        run_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[MemoryRecord, MemoryProjection, str]:
        """Stage one Candidate revision and its immutable projection payload.

        No Store write occurs here.  The returned outbox operation is the
        hand-off to the single writer.  Calling code should use ``conn`` when
        it also persists run-item mappings so the catalog, source ledger and
        outbox are one SQLite transaction.
        """
        if scope not in {"user", "project"}:
            raise ValueError("memory scope must be user or project")
        if scope == "user":
            namespace = namespace_for_user(user_id, "candidate")
            normalized_user_id = str(user_id)
            normalized_department_id = None
            normalized_kb_id = None
        else:
            namespace = namespace_for_project(department_id, kb_id, "candidate")
            normalized_user_id = None
            normalized_department_id = str(department_id)
            normalized_kb_id = str(kb_id)
        semantic = dict(content)
        memory_type = str(semantic.get("memory_type") or "context")
        title = str(semantic.get("title") or "未命名记忆").strip()
        subject = semantic.get("subject")
        subject = str(subject).strip() if subject not in (None, "") else None
        if not title or not semantic.get("content"):
            raise ValueError("memory content must contain a non-empty title and content")
        normalized_hash = memory_content_hash(semantic, schema_version=schema_version)
        now = utc_now()
        c = conn or self._conn
        existing = None
        if memory_id:
            existing = self.get_record(memory_id, conn=c)
        projection: MemoryProjection | None = None
        if existing:
            if existing.scope != scope or existing.user_id != normalized_user_id or existing.department_id != normalized_department_id or existing.kb_id != normalized_kb_id:
                raise PermissionError("memory scope cannot change during a candidate update")
            if existing.status not in {"candidate", "needs_rebuild"}:
                raise RuntimeError(f"memory {memory_id} is not manager-writable: {existing.status}")
            projection_rows = c.execute(
                "SELECT * FROM memory_projections WHERE memory_id = ? AND projection_kind = 'candidate' AND retired_at IS NULL",
                (memory_id,),
            ).fetchall()
            if projection_rows:
                projection = row_to_projection(projection_rows[0])
                if not projection.manager_writable:
                    raise RuntimeError("candidate projection is fenced")
                if expected_fence is not None and projection.fence_version != int(expected_fence):
                    raise RuntimeError("candidate projection fence changed")
            revision_no = existing.current_revision + 1
            cur = c.execute(
                """UPDATE memory_records SET status = 'candidate', current_revision = ?, content_hash = ?,
                    memory_type = ?, title = ?, subject = ?, content_json = ?, extractor_model = ?,
                    extractor_version = ?, schema_version = ?, updated_at = ?, deleted_at = NULL
                WHERE memory_id = ? AND current_revision = ? AND status IN ('candidate', 'needs_rebuild')""",
                (revision_no, normalized_hash, memory_type, title, subject, json_dumps(semantic), extractor_model,
                 extractor_version, schema_version, now, existing.memory_id, existing.current_revision),
            )
            if cur.rowcount != 1:
                raise RuntimeError("candidate revision CAS failed")
        else:
            memory_id = memory_id or str(uuid.uuid4())
            revision_no = 1
            c.execute(
                """INSERT INTO memory_records
                (memory_id, scope, user_id, department_id, kb_id, status, current_revision, content_hash,
                 memory_type, title, subject, content_json, extractor_model, extractor_version, schema_version,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (memory_id, scope, normalized_user_id, normalized_department_id, normalized_kb_id, revision_no,
                 normalized_hash, memory_type, title, subject, json_dumps(semantic), extractor_model,
                 extractor_version, schema_version, now, now),
            )
        if projection is None:
            projection_id = str(uuid.uuid4())
            # A retired projection row remains in the Catalog for audit and
            # uniqueness purposes.  A needs_rebuild record therefore cannot
            # safely reuse its old physical key: doing so would either hit
            # the full UNIQUE(namespace, store_key) constraint or let a
            # delayed delete remove a newly rebuilt object.  New records keep
            # the compact memory_id key; rebuilt records get a fresh,
            # revision-specific key unless LangMem proposed a new one.
            key = projection_key or (
                f"{memory_id}:candidate:{revision_no}"
                if existing is not None and existing.status == "needs_rebuild"
                else memory_id
            )
            c.execute(
                """INSERT INTO memory_projections
                (projection_id, memory_id, projection_kind, store_backend, namespace_json, store_key,
                 current_revision, current_content_hash, active, manager_writable, fence_version, created_at)
                VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, 0, 1, 1, ?)""",
                (projection_id, memory_id, store_backend, namespace_json(namespace), str(key), revision_no, normalized_hash, now),
            )
            projection = self.get_projection(projection_id, conn=c)
        else:
            cur = c.execute(
                """UPDATE memory_projections SET current_revision = ?, current_content_hash = ?,
                    active = 0, manager_writable = 1 WHERE projection_id = ? AND retired_at IS NULL
                      AND fence_version = ?""",
                (revision_no, normalized_hash, projection.projection_id, projection.fence_version),
            )
            if cur.rowcount != 1:
                raise RuntimeError("candidate projection CAS failed")
            projection = self.get_projection(projection.projection_id, conn=c)
        assert projection is not None
        before = existing.content if existing else None
        c.execute(
            """INSERT INTO memory_revisions
            (revision_id, memory_id, revision_no, projection_id, before_content_json, after_content_json, operation, actor_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), memory_id, revision_no, projection.projection_id, json_dumps(before) if before is not None else None,
             json_dumps(semantic), revision_operation or ("create" if revision_no == 1 else "update"), actor_id, reason, now),
        )
        for source in source_refs:
            _insert_source(c, memory_id, source)
        payload = {
            "kind": memory_type,
            "content": semantic,
            "memory_id": memory_id,
            "projection_id": projection.projection_id,
            "content_hash": normalized_hash,
            "schema_version": schema_version,
            "run_id": run_id,
        }
        operation_id = str(uuid.uuid4())
        idempotency_key = f"put:{projection.projection_id}:{revision_no}:{normalized_hash}"
        c.execute(
            """INSERT OR IGNORE INTO memory_projection_outbox
            (operation_id, memory_id, projection_id, operation, expected_revision, expected_fence, idempotency_key, payload_json, created_at)
            VALUES (?, ?, ?, 'put', ?, ?, ?, ?, ?)""",
            (operation_id, memory_id, projection.projection_id, revision_no, projection.fence_version, idempotency_key, json_dumps(payload), now),
        )
        self.audit("candidate_revision_prepared", memory_id=memory_id, operation_id=operation_id, actor_id=actor_id, metadata={"revision": revision_no, "reason": reason, "run_id": run_id}, conn=c)
        record = self.get_record(memory_id, conn=c)
        assert record is not None
        return record, projection, operation_id

    def retire_projection(
        self,
        projection_id: str,
        *,
        operation: str = "delete",
        reason: str = "governance",
        actor_id: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> str | None:
        """Fence and queue deletion of one physical projection."""
        c = conn or self._conn
        row = c.execute("SELECT * FROM memory_projections WHERE projection_id = ? AND retired_at IS NULL", (projection_id,)).fetchone()
        if row is None:
            return None
        now = utc_now()
        next_fence = int(row["fence_version"]) + 1
        cur = c.execute(
            "UPDATE memory_projections SET active = 0, manager_writable = 0, fence_version = ?, retired_at = ? WHERE projection_id = ? AND retired_at IS NULL AND fence_version = ?",
            (next_fence, now, projection_id, int(row["fence_version"])),
        )
        if cur.rowcount != 1:
            raise RuntimeError("projection fence CAS failed")
        operation_id = str(uuid.uuid4())
        payload = {
            "projection_id": row["projection_id"],
            "namespace": json_loads(row["namespace_json"], []),
            "store_key": row["store_key"],
            "reason": reason,
        }
        c.execute(
            """INSERT OR IGNORE INTO memory_deletion_outbox
            (operation_id, memory_id, source_or_consent_id, operation, expected_revision, expected_fence, idempotency_key, payload_json, created_at)
            VALUES (?, ?, ?, 'delete_projection', ?, ?, ?, ?, ?)""",
            (operation_id, row["memory_id"], reason, int(row["current_revision"]), next_fence,
             f"delete:{row['memory_id']}:{projection_id}:{next_fence}", json_dumps(payload), now),
        )
        self.audit("projection_retired", memory_id=row["memory_id"], operation_id=operation_id, actor_id=actor_id, metadata={"reason": reason, "operation": operation}, conn=c)
        return operation_id

    def fence_projection(
        self,
        projection_id: str,
        *,
        expected_fence: int,
        conn: sqlite3.Connection | None = None,
    ) -> int | None:
        """Make a projection unreadable/unwritable without retiring it.

        Verify uses this first phase to prevent Manager writes while the
        Verified projection is being published.  The row remains present so
        the deletion outbox is only created after Verified is active.
        """
        c = conn or self._conn
        next_fence = int(expected_fence) + 1
        cur = c.execute(
            """UPDATE memory_projections
               SET active = 0, manager_writable = 0, fence_version = ?
               WHERE projection_id = ? AND retired_at IS NULL AND fence_version = ?""",
            (next_fence, projection_id, int(expected_fence)),
        )
        return next_fence if cur.rowcount == 1 else None

    def mark_projection_active(
        self,
        operation_id: str,
        *,
        projection_id: str,
        expected_revision: int,
        expected_fence: int,
        content_hash_value: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        c = conn or self._conn
        row = c.execute("SELECT * FROM memory_projection_outbox WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None or row["operation"] != "put":
            return False
        projection = c.execute("SELECT * FROM memory_projections WHERE projection_id = ?", (projection_id,)).fetchone()
        record = c.execute("SELECT * FROM memory_records WHERE memory_id = ?", (row["memory_id"],)).fetchone()
        if projection is None or record is None:
            return False
        if int(projection["current_revision"]) != int(expected_revision) or int(projection["fence_version"]) != int(expected_fence):
            return False
        if projection["current_content_hash"] != content_hash_value or record["content_hash"] != content_hash_value:
            return False
        if record["status"] not in ACTIVE_MEMORY_STATUSES and not (
            record["status"] == "verification_pending" and projection["projection_kind"] == "verified"
        ):
            return False
        if projection["retired_at"] is not None:
            return False
        now = utc_now()
        c.execute("UPDATE memory_projections SET active = 1 WHERE projection_id = ? AND current_revision = ? AND fence_version = ? AND retired_at IS NULL", (projection_id, int(expected_revision), int(expected_fence)))
        if projection["projection_kind"] == "verified" and record["status"] == "verification_pending":
            c.execute(
                "UPDATE memory_records SET status = 'verified', updated_at = ? WHERE memory_id = ? AND status = 'verification_pending' AND current_revision = ?",
                (now, row["memory_id"], int(expected_revision)),
            )
            # The verified object is now Catalog-active.  Only at this point
            # is it safe to retire the fenced Candidate and enqueue its
            # physical deletion; a failed Verified put leaves the Candidate
            # frozen but still available for recovery.
            candidate = c.execute(
                """SELECT * FROM memory_projections
                   WHERE memory_id = ? AND projection_kind = 'candidate'
                     AND retired_at IS NULL""",
                (row["memory_id"],),
            ).fetchone()
            if candidate is not None:
                self.retire_projection(
                    candidate["projection_id"],
                    operation="verify",
                    reason="verified",
                    actor_id="memory-worker",
                    conn=c,
                )
        c.execute("UPDATE memory_projection_outbox SET status = 'completed', completed_at = ? WHERE operation_id = ? AND status IN ('pending', 'running', 'retrying')", (now, operation_id))
        self.audit("projection_active", memory_id=row["memory_id"], operation_id=operation_id, metadata={"revision": expected_revision, "fence": expected_fence}, conn=c)
        return True

    def mark_projection_deleted(self, operation_id: str, *, conn: sqlite3.Connection | None = None) -> bool:
        c = conn or self._conn
        row = c.execute(
            "SELECT memory_id FROM memory_deletion_outbox WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        cur = c.execute(
            "UPDATE memory_deletion_outbox SET status = 'completed', completed_at = ? WHERE operation_id = ? AND status IN ('pending', 'running', 'retrying')",
            (utc_now(), operation_id),
        )
        if row is not None and cur.rowcount:
            # Supersede is a two-phase lifecycle: the old record is already
            # excluded from reads, but its final tombstone status is recorded
            # only after the retired physical projection has been deleted.
            remaining = c.execute(
                "SELECT 1 FROM memory_projections WHERE memory_id = ? AND retired_at IS NULL LIMIT 1",
                (row["memory_id"],),
            ).fetchone()
            if remaining is None:
                c.execute(
                    "UPDATE memory_records SET status = 'superseded', updated_at = ? WHERE memory_id = ? AND status = 'supersede_pending'",
                    (utc_now(), row["memory_id"]),
                )
        return bool(cur.rowcount)


class _Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.started = False

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        self.started = True
        return self.conn

    def __exit__(self, exc_type, _exc, _tb):
        if not self.started:
            return False
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _insert_source(conn: sqlite3.Connection, memory_id: str, source: dict[str, Any]) -> None:
    """Insert a provenance edge without ever cascading to raw chat rows."""
    source_hash = str(source.get("source_hash") or source.get("content_hash") or "").strip()
    if not source_hash:
        raise ValueError("memory source requires a content hash")
    conn.execute(
        """INSERT OR IGNORE INTO memory_sources
        (source_id, memory_id, session_id, turn_id, message_id, consent_event_id, source_hash,
         source_role, contribution_kind, source_valid, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            str(uuid.uuid4()),
            memory_id,
            source.get("session_id"),
            source.get("turn_id"),
            source.get("message_id"),
            source.get("consent_event_id"),
            source_hash,
            str(source.get("source_role") or source.get("role") or ""),
            str(source.get("contribution_kind") or "extracted"),
            utc_now(),
        ),
    )


def row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row["memory_id"],
        scope=row["scope"],
        user_id=row["user_id"],
        department_id=row["department_id"],
        kb_id=row["kb_id"],
        status=row["status"],
        current_revision=int(row["current_revision"]),
        content_hash=row["content_hash"],
        memory_type=row["memory_type"],
        title=row["title"],
        subject=row["subject"],
        content=json_loads(row["content_json"], {}),
        extractor_model=row["extractor_model"] or "",
        extractor_version=row["extractor_version"] or "",
        schema_version=row["schema_version"] or MEMORY_SCHEMA_VERSION,
        replacement_id=row["replacement_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def row_to_projection(row: sqlite3.Row) -> MemoryProjection:
    namespace = json_loads(row["namespace_json"], [])
    return MemoryProjection(
        projection_id=row["projection_id"],
        memory_id=row["memory_id"],
        projection_kind=row["projection_kind"],
        store_backend=row["store_backend"],
        namespace=tuple(str(part) for part in namespace),
        store_key=row["store_key"],
        current_revision=int(row["current_revision"]),
        current_content_hash=row["current_content_hash"],
        active=bool(row["active"]),
        manager_writable=bool(row["manager_writable"]),
        fence_version=int(row["fence_version"]),
        retired_at=row["retired_at"],
    )


def row_to_job(row: sqlite3.Row) -> MemoryJob:
    return MemoryJob(
        job_id=row["job_id"],
        session_id=int(row["session_id"]),
        job_kind=row["job_kind"],
        scope_fingerprint=row["scope_fingerprint"],
        consent_event_id=row["consent_event_id"],
        consent_policy_version=row["consent_policy_version"],
        consent_revoke_generation=(int(row["consent_revoke_generation"]) if row["consent_revoke_generation"] is not None else None),
        target_turn_id=row["target_turn_id"],
        target_message_id=int(row["target_message_id"]),
        source_start_turn_id=row["source_start_turn_id"],
        source_start_message_id=(int(row["source_start_message_id"]) if row["source_start_message_id"] is not None else None),
        authorized_start_turn_id=row["authorized_start_turn_id"],
        authorized_start_message_id=(int(row["authorized_start_message_id"]) if row["authorized_start_message_id"] is not None else None),
        authorized_end_turn_id=row["authorized_end_turn_id"],
        authorized_end_message_id=(int(row["authorized_end_message_id"]) if row["authorized_end_message_id"] is not None else None),
        authorized_source_hash=row["authorized_source_hash"],
        generation=int(row["generation"]),
        status=row["status"],
        requested_at=row["requested_at"],
        available_at=row["available_at"],
        next_retry_at=row["next_retry_at"],
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        retry_count=int(row["retry_count"] or 0),
        last_error=row["last_error"] or "",
    )


def get_job(conn: sqlite3.Connection, job_id: str) -> MemoryJob | None:
    row = conn.execute("SELECT * FROM memory_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row_to_job(row) if row else None


def get_run(conn: sqlite3.Connection, run_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memory_reflection_runs WHERE run_key = ?", (run_key,)).fetchone()


def memory_content_hash(content: dict[str, Any], *, schema_version: str = MEMORY_SCHEMA_VERSION) -> str:
    """Single hash implementation used by Catalog, projections and adapters."""
    return content_hash(content, schema_version=schema_version)


__all__ = [
    "ACTIVE_MEMORY_STATUSES",
    "MEMORY_STATUSES",
    "MemoryCatalogRepository",
    "MemoryJob",
    "MemoryProjection",
    "MemoryRecord",
    "ensure_memory_schema",
    "get_job",
    "get_run",
    "json_dumps",
    "json_loads",
    "memory_content_hash",
    "namespace_for_project",
    "namespace_for_user",
    "namespace_json",
    "row_to_job",
    "scope_fingerprint",
    "utc_now",
]
