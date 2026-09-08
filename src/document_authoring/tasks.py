"""Persistent user-facing document-task identity and associations.

``DocumentTask`` is the user-level aggregate described by the authoring
architecture.  It deliberately does not replace WorkOrder or the durable job
queue: those objects continue to own frozen execution definitions and worker
leases respectively.  This module only owns the cross-entry-point identity
and its durable associations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


TASK_ORIGINS = frozenset({"chat", "workbench", "api", "legacy"})
TASK_STATUSES = frozenset({
    "planned", "needs_clarification", "queued", "running", "waiting_human",
    "awaiting_plan_confirmation", "awaiting_release",
    "completed", "failed", "cancelled", "legacy",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class DocumentTask(BaseModel):
    """The user-visible identity shared by Chat, Workbench and API flows."""

    task_id: str
    tenant_id: str
    user_id: str
    origin: Literal["chat", "workbench", "api", "legacy"]
    conversation_id: str | None = None
    initiating_turn_id: str | None = None
    project_id: str | None = None
    knowledge_base_name: str | None = None
    template_version_id: str | None = None
    generation_session_id: str | None = None
    output_spec_id: str | None = None
    output_spec_version: int | None = Field(default=None, ge=1)
    document_plan_id: str | None = None
    document_plan_version: int | None = Field(default=None, ge=1)
    work_order_id: str | None = None
    current_run_id: str | None = None
    current_artifact_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "planned", "needs_clarification", "queued", "running", "waiting_human",
        "awaiting_plan_confirmation", "awaiting_release",
        "completed", "failed", "cancelled", "legacy",
    ] = "planned"
    idempotency_key: str | None = None
    created_by: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_identity(self):
        for field_name in ("task_id", "tenant_id", "user_id", "created_by"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"document task {field_name} is required")
        if self.origin == "chat" and (
            not str(self.conversation_id or "").strip()
            or not str(self.initiating_turn_id or "").strip()
        ):
            raise ValueError("chat document tasks require conversation_id and initiating_turn_id")
        if self.origin != "legacy" and self.status == "legacy":
            raise ValueError("only legacy document tasks may use legacy status")
        if self.idempotency_key is not None:
            self.idempotency_key = str(self.idempotency_key).strip()[:256] or None
        for prefix in ("output_spec", "document_plan"):
            identifier = getattr(self, f"{prefix}_id")
            version = getattr(self, f"{prefix}_version")
            if (identifier is None) != (version is None):
                raise ValueError(f"{prefix} id and version must be provided together")
        self.artifact_ids = list(dict.fromkeys(
            str(item).strip() for item in self.artifact_ids if str(item).strip()
        ))
        return self


class DocumentTaskStore:
    """SQLite repository for task identity, associations and task events."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_tasks (
                    task_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    conversation_id TEXT,
                    initiating_turn_id TEXT,
                    project_id TEXT,
                    knowledge_base_name TEXT,
                    template_version_id TEXT,
                    generation_session_id TEXT,
                    output_spec_id TEXT,
                    output_spec_version INTEGER,
                    document_plan_id TEXT,
                    document_plan_version INTEGER,
                    work_order_id TEXT,
                    status TEXT NOT NULL,
                    idempotency_key TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_tasks_idempotency
                    ON document_tasks(tenant_id, user_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL AND idempotency_key != '';
                CREATE INDEX IF NOT EXISTS idx_document_tasks_generation_session
                    ON document_tasks(tenant_id, generation_session_id);
                CREATE INDEX IF NOT EXISTS idx_document_tasks_work_order
                    ON document_tasks(tenant_id, work_order_id);
                CREATE INDEX IF NOT EXISTS idx_document_tasks_owner
                    ON document_tasks(tenant_id, user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS document_task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(task_id, idempotency_key),
                    FOREIGN KEY(task_id) REFERENCES document_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_document_task_events_task
                    ON document_task_events(task_id, created_at, event_id);
                """
            )
            for column, ddl in (
                ("output_spec_id", "TEXT"),
                ("output_spec_version", "INTEGER"),
                ("document_plan_id", "TEXT"),
                ("document_plan_version", "INTEGER"),
            ):
                self._ensure_column(conn, "document_tasks", column, ddl)

    @staticmethod
    def _normalize(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _validate_origin(origin: str) -> str:
        normalized = str(origin or "").strip().lower()
        if normalized not in TASK_ORIGINS:
            raise ValueError("unsupported document task origin")
        return normalized

    @staticmethod
    def _validate_status(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized not in TASK_STATUSES:
            raise ValueError("unsupported document task status")
        return normalized

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def create_task(
        self,
        *,
        tenant_id: str,
        user_id: str | int,
        origin: str,
        created_by: str | int,
        conversation_id: str | int | None = None,
        initiating_turn_id: str | None = None,
        project_id: str | None = None,
        knowledge_base_name: str | None = None,
        template_version_id: str | None = None,
        generation_session_id: str | None = None,
        status: str = "planned",
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentTask:
        origin = self._validate_origin(origin)
        status = self._validate_status(status)
        task = DocumentTask(
            task_id=f"document-task-{uuid.uuid4().hex}",
            tenant_id=str(tenant_id or "").strip(),
            user_id=str(user_id or "").strip(),
            origin=origin,  # type: ignore[arg-type]
            conversation_id=self._normalize(conversation_id),
            initiating_turn_id=self._normalize(initiating_turn_id),
            project_id=self._normalize(project_id),
            knowledge_base_name=self._normalize(knowledge_base_name),
            template_version_id=self._normalize(template_version_id),
            generation_session_id=self._normalize(generation_session_id),
            output_spec_id=self._normalize(output_spec_id),
            output_spec_version=output_spec_version,
            document_plan_id=self._normalize(document_plan_id),
            document_plan_version=document_plan_version,
            status=status,  # type: ignore[arg-type]
            idempotency_key=self._normalize(idempotency_key),
            created_by=str(created_by or "").strip(),
            metadata=dict(metadata or {}),
        )
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if task.idempotency_key:
                    existing = conn.execute(
                        """SELECT * FROM document_tasks
                           WHERE tenant_id = ? AND user_id = ? AND idempotency_key = ?""",
                        (task.tenant_id, task.user_id, task.idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        current = _row_to_task(existing)
                        self._assert_idempotent_identity(current, task)
                        conn.execute("COMMIT")
                        return current
                conn.execute(
                    """INSERT INTO document_tasks (
                           task_id, tenant_id, user_id, origin, conversation_id,
                           initiating_turn_id, project_id, knowledge_base_name,
                           template_version_id, generation_session_id,
                           output_spec_id, output_spec_version, document_plan_id,
                           document_plan_version, work_order_id,
                           status, idempotency_key, created_by, created_at, updated_at,
                           payload_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task.task_id, task.tenant_id, task.user_id, task.origin,
                        task.conversation_id, task.initiating_turn_id, task.project_id,
                        task.knowledge_base_name, task.template_version_id,
                        task.generation_session_id, task.output_spec_id,
                        task.output_spec_version, task.document_plan_id,
                        task.document_plan_version, task.work_order_id, task.status,
                        task.idempotency_key, task.created_by, task.created_at.isoformat(),
                        task.updated_at.isoformat(), _json(task),
                    ),
                )
                conn.execute("COMMIT")
                return task
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _assert_idempotent_identity(current: DocumentTask, requested: DocumentTask) -> None:
        fields = (
            "tenant_id", "user_id", "origin", "conversation_id", "initiating_turn_id",
            "project_id", "knowledge_base_name", "template_version_id",
            "generation_session_id", "created_by",
        )
        if any(getattr(current, field) != getattr(requested, field) for field in fields):
            raise ValueError("document task idempotency key conflicts with existing payload")

    def get(self, task_id: str) -> DocumentTask | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_tasks WHERE task_id = ?", (str(task_id),)
            ).fetchone()
        return _row_to_task(row) if row else None

    def get_by_idempotency(
        self, tenant_id: str, user_id: str | int, idempotency_key: str,
    ) -> DocumentTask | None:
        key = self._normalize(idempotency_key)
        if not key:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM document_tasks
                   WHERE tenant_id = ? AND user_id = ? AND idempotency_key = ?""",
                (str(tenant_id), str(user_id), key),
            ).fetchone()
        return _row_to_task(row) if row else None

    def get_by_generation_session(
        self, generation_session_id: str, *, tenant_id: str | None = None,
        user_id: str | int | None = None,
    ) -> DocumentTask | None:
        clauses = ["generation_session_id = ?"]
        params: list[Any] = [str(generation_session_id)]
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(str(tenant_id))
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_tasks WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC, task_id DESC LIMIT 1",
                params,
            ).fetchone()
        return _row_to_task(row) if row else None

    def get_by_work_order(
        self, work_order_id: str, *, tenant_id: str | None = None,
        user_id: str | int | None = None,
    ) -> DocumentTask | None:
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
                "SELECT * FROM document_tasks WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC, task_id DESC LIMIT 1",
                params,
            ).fetchone()
        return _row_to_task(row) if row else None

    def attach_work_order(self, task_id: str, work_order_id: str) -> DocumentTask:
        task_id = str(task_id or "").strip()
        work_order_id = str(work_order_id or "").strip()
        if not task_id or not work_order_id:
            raise ValueError("task and work-order ids are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("document task not found")
                task = _row_to_task(row)
                if task.work_order_id and task.work_order_id != work_order_id:
                    raise ValueError("document task is already bound to another work order")
                if task.work_order_id == work_order_id:
                    conn.execute("COMMIT")
                    return task
                updated = task.model_copy(update={
                    "work_order_id": work_order_id,
                    "updated_at": _now(),
                })
                conn.execute(
                    """UPDATE document_tasks
                       SET work_order_id = ?, updated_at = ?, payload_json = ?
                       WHERE task_id = ?""",
                    (
                        work_order_id, updated.updated_at.isoformat(), _json(updated), task_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO document_task_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"document-task-event-{uuid.uuid4().hex}", task_id,
                        "work_order_bound", f"work-order:{work_order_id}", _iso(),
                        _json({"work_order_id": work_order_id}),
                    ),
                )
                conn.execute("COMMIT")
                return updated
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def advance_work_order(
        self,
        task_id: str,
        work_order_id: str,
        *,
        expected_work_order_id: str,
    ) -> DocumentTask:
        """Move the task's current execution pointer during an explicit restart.

        A task normally binds to one WorkOrder.  Restarting a cancelled
        WorkOrder is the deliberate exception: the new WorkOrder is the
        current execution definition while the old one remains immutable and
        addressable through its ``restart_of_work_order_id`` lineage.
        """
        task_id = str(task_id or "").strip()
        work_order_id = str(work_order_id or "").strip()
        expected_work_order_id = str(expected_work_order_id or "").strip()
        if not task_id or not work_order_id or not expected_work_order_id:
            raise ValueError("task and expected work-order ids are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("document task not found")
                task = _row_to_task(row)
                if task.work_order_id != expected_work_order_id:
                    raise ValueError("task current work order differs from expected work order")
                if work_order_id == expected_work_order_id:
                    conn.execute("COMMIT")
                    return task
                updated = task.model_copy(update={
                    "work_order_id": work_order_id,
                    "updated_at": _now(),
                })
                conn.execute(
                    """UPDATE document_tasks
                       SET work_order_id = ?, updated_at = ?, payload_json = ?
                       WHERE task_id = ?""",
                    (
                        work_order_id, updated.updated_at.isoformat(), _json(updated), task_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO document_task_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"document-task-event-{uuid.uuid4().hex}", task_id,
                        "work_order_restarted",
                        f"work-order-restart:{expected_work_order_id}:{work_order_id}",
                        _iso(),
                        _json({
                            "previous_work_order_id": expected_work_order_id,
                            "work_order_id": work_order_id,
                        }),
                    ),
                )
                conn.execute("COMMIT")
                return updated
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def attach_run(self, task_id: str, run_id: str) -> DocumentTask:
        """Record the latest execution attempt for a task."""
        task_id = str(task_id or "").strip()
        run_id = str(run_id or "").strip()
        if not task_id or not run_id:
            raise ValueError("task and run ids are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("document task not found")
                task = _row_to_task(row)
                if task.current_run_id == run_id:
                    conn.execute("COMMIT")
                    return task
                updated = task.model_copy(update={
                    "current_run_id": run_id,
                    "updated_at": _now(),
                })
                conn.execute(
                    "UPDATE document_tasks SET updated_at = ?, payload_json = ? WHERE task_id = ?",
                    (updated.updated_at.isoformat(), _json(updated), task_id),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO document_task_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"document-task-event-{uuid.uuid4().hex}", task_id,
                        "run_bound", f"run:{run_id}", _iso(), _json({"run_id": run_id}),
                    ),
                )
                conn.execute("COMMIT")
                return updated
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def attach_artifact(self, task_id: str, artifact_id: str) -> DocumentTask:
        """Append an immutable Artifact and move the current pointer to it."""
        task_id = str(task_id or "").strip()
        artifact_id = str(artifact_id or "").strip()
        if not task_id or not artifact_id:
            raise ValueError("task and artifact ids are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("document task not found")
                task = _row_to_task(row)
                if artifact_id in task.artifact_ids:
                    conn.execute("COMMIT")
                    return task
                updated = task.model_copy(update={
                    "current_artifact_id": artifact_id,
                    "artifact_ids": [*task.artifact_ids, artifact_id],
                    "updated_at": _now(),
                })
                conn.execute(
                    "UPDATE document_tasks SET updated_at = ?, payload_json = ? WHERE task_id = ?",
                    (updated.updated_at.isoformat(), _json(updated), task_id),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO document_task_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"document-task-event-{uuid.uuid4().hex}", task_id,
                        "artifact_bound", f"artifact:{artifact_id}", _iso(),
                        _json({"artifact_id": artifact_id}),
                    ),
                )
                conn.execute("COMMIT")
                return updated
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update_status(self, task_id: str, status: str) -> DocumentTask:
        normalized = self._validate_status(status)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
                if row is None:
                    raise KeyError("document task not found")
                task = _row_to_task(row)
                if task.status == normalized:
                    conn.execute("COMMIT")
                    return task
                updated = task.model_copy(update={"status": normalized, "updated_at": _now()})
                conn.execute(
                    "UPDATE document_tasks SET status = ?, updated_at = ?, payload_json = ? WHERE task_id = ?",
                    (updated.status, updated.updated_at.isoformat(), _json(updated), updated.task_id),
                )
                conn.execute(
                    """INSERT INTO document_task_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"document-task-event-{uuid.uuid4().hex}",
                        updated.task_id,
                        "status_changed",
                        f"status-change:{uuid.uuid4().hex}",
                        updated.updated_at.isoformat(),
                        _json({"from": task.status, "to": updated.status}),
                    ),
                )
                conn.execute("COMMIT")
                return updated
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def bind_plan(
        self,
        task_id: str,
        *,
        output_spec_id: str,
        output_spec_version: int,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
    ) -> DocumentTask:
        """Bind the current immutable planning pointers to a user task."""
        spec_id = self._normalize(output_spec_id)
        plan_id = self._normalize(document_plan_id)
        if not spec_id or output_spec_version < 1:
            raise ValueError("output spec id and positive version are required")
        if (plan_id is None) != (document_plan_version is None):
            raise ValueError("document plan id and version must be provided together")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
                if row is None:
                    raise KeyError("document task not found")
                task = _row_to_task(row)
                existing = (
                    task.output_spec_id, task.output_spec_version,
                    task.document_plan_id, task.document_plan_version,
                )
                requested = (spec_id, output_spec_version, plan_id, document_plan_version)
                if any(value is not None for value in existing):
                    if existing != requested:
                        raise ValueError("document task is already bound to another plan")
                    conn.execute("COMMIT")
                    return task
                updated = task.model_copy(update={
                    "output_spec_id": spec_id,
                    "output_spec_version": output_spec_version,
                    "document_plan_id": plan_id,
                    "document_plan_version": document_plan_version,
                    "updated_at": _now(),
                })
                conn.execute(
                    """UPDATE document_tasks
                       SET output_spec_id = ?, output_spec_version = ?,
                           document_plan_id = ?, document_plan_version = ?,
                           updated_at = ?, payload_json = ?
                       WHERE task_id = ?""",
                    (
                        updated.output_spec_id, updated.output_spec_version,
                        updated.document_plan_id, updated.document_plan_version,
                        updated.updated_at.isoformat(), _json(updated), updated.task_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO document_task_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"document-task-event-{uuid.uuid4().hex}", updated.task_id,
                        "plan_bound", f"plan:{spec_id}:{output_spec_version}", _iso(),
                        _json({
                            "output_spec_id": spec_id,
                            "output_spec_version": output_spec_version,
                            "document_plan_id": plan_id,
                            "document_plan_version": document_plan_version,
                        }),
                    ),
                )
                conn.execute("COMMIT")
                return updated
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_events(self, task_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the append-only task audit events in creation order."""
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise ValueError("task id is required")
        if limit is not None and limit < 1:
            raise ValueError("event list limit must be positive")
        parameters: list[Any] = [normalized_task_id]
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            parameters.append(int(limit))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT event_id, task_id, event_type, idempotency_key,
                          created_at, payload_json
                     FROM document_task_events
                    WHERE task_id = ?
                    ORDER BY created_at, event_id""" + limit_clause,
                parameters,
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "idempotency_key": row["idempotency_key"],
                "created_at": row["created_at"],
                "payload": _load(row["payload_json"], {}),
            }
            for row in rows
        ]

    def list_for_owner(
        self, *, tenant_id: str, user_id: str | int, limit: int = 100,
    ) -> list[DocumentTask]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM document_tasks
                   WHERE tenant_id = ? AND user_id = ?
                   ORDER BY updated_at DESC, task_id DESC LIMIT ?""",
                (str(tenant_id), str(user_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [_row_to_task(row) for row in rows]


class DocumentTaskService:
    """Resolve task identity from an authorized request context.

    The service is intentionally small.  It centralizes origin and trace
    extraction so Workbench/API code cannot accidentally infer a Chat turn
    from a synthetic session id.
    """

    def __init__(self, store: DocumentTaskStore):
        self.store = store

    def ensure_task(
        self,
        ctx: Any,
        *,
        template_version_id: str | None = None,
        generation_session_id: str | None = None,
        project_id: str | None = None,
        knowledge_base_name: str | None = None,
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        idempotency_key: str | None = None,
        status: str = "planned",
    ) -> DocumentTask:
        metadata = getattr(ctx, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        tenant_id = str(getattr(ctx, "tenant_id", None) or "default").strip()
        user_id = str(getattr(ctx, "user_id", None) or "").strip()
        explicit_origin = str(metadata.get("document_task_origin") or "").strip().lower()
        turn_id = self._optional(metadata.get("initiating_turn_id"))
        origin = explicit_origin or ("chat" if turn_id else "api")
        if origin not in TASK_ORIGINS:
            raise ValueError("unsupported document task origin")
        conversation_id = self._optional(metadata.get("conversation_id"))
        if origin == "chat" and conversation_id is None:
            conversation_id = self._optional(getattr(ctx, "session_id", None))
        normalized_session = self._optional(generation_session_id)
        normalized_template = self._optional(template_version_id)
        normalized_project = self._optional(project_id)
        normalized_kb = self._optional(knowledge_base_name)
        normalized_key = self._optional(
            idempotency_key or metadata.get("document_task_idempotency_key")
        )

        if normalized_session:
            existing = self.store.get_by_generation_session(
                normalized_session, tenant_id=tenant_id, user_id=user_id,
            )
            if existing is not None:
                self._assert_existing_request(
                    existing, normalized_template, normalized_kb, normalized_project,
                    origin=origin, conversation_id=conversation_id, initiating_turn_id=turn_id,
                )
                return existing
        if normalized_key:
            existing = self.store.get_by_idempotency(tenant_id, user_id, normalized_key)
            if existing is not None:
                self._assert_existing_request(
                    existing, normalized_template, normalized_kb, normalized_project,
                    origin=origin, conversation_id=conversation_id, initiating_turn_id=turn_id,
                )
                return existing
        return self.store.create_task(
            tenant_id=tenant_id,
            user_id=user_id,
            origin=origin,
            created_by=user_id,
            conversation_id=conversation_id,
            initiating_turn_id=turn_id,
            project_id=normalized_project,
            knowledge_base_name=normalized_kb,
            template_version_id=normalized_template,
            generation_session_id=normalized_session,
            output_spec_id=self._optional(output_spec_id),
            output_spec_version=output_spec_version,
            document_plan_id=self._optional(document_plan_id),
            document_plan_version=document_plan_version,
            status=status,
            idempotency_key=normalized_key,
        )

    def legacy_view_for_work_order(self, order: Any) -> DocumentTask:
        """Build a read-only task projection for a pre-task WorkOrder.

        The projection is intentionally not persisted.  In particular, it
        never invents a conversation or turn association for historical data.
        """
        status = {
            "complete": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "blocked": "failed",
            "waiting_human_input": "waiting_human",
            "waiting_human_approval": "waiting_human",
            "planned": "planned",
        }.get(str(getattr(order, "status", "planned") or "planned"), "running")
        created_by = str(getattr(order, "created_by", "legacy") or "legacy")
        return DocumentTask(
            task_id=f"legacy-document-task-{str(getattr(order, 'work_order_id', '') or '').strip()}",
            tenant_id=str(getattr(order, "tenant_id", "default") or "default"),
            user_id=created_by,
            origin="legacy",
            template_version_id=self._optional(getattr(order, "template_version_id", None)),
            generation_session_id=self._optional(getattr(order, "generation_session_id", None)),
            project_id=self._optional(getattr(order, "project_id", None)),
            knowledge_base_name=self._optional(getattr(order, "knowledge_base_name", None)),
            work_order_id=self._optional(getattr(order, "work_order_id", None)),
            status=status,  # type: ignore[arg-type]
            created_by=created_by,
            metadata={"legacy_source": "document_work_order"},
            created_at=getattr(order, "created_at", _now()),
            updated_at=getattr(order, "updated_at", _now()),
        )

    @staticmethod
    def _optional(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _assert_existing_request(
        existing: DocumentTask,
        template_version_id: str | None,
        knowledge_base_name: str | None,
        project_id: str | None,
        *,
        origin: str,
        conversation_id: str | None,
        initiating_turn_id: str | None,
    ) -> None:
        if existing.origin != origin:
            raise ValueError("document task idempotency key conflicts with existing origin")
        if origin == "chat" and (
            existing.conversation_id != conversation_id
            or existing.initiating_turn_id != initiating_turn_id
        ):
            raise ValueError("document task idempotency key conflicts with existing origin trace")
        if template_version_id and existing.template_version_id not in {None, template_version_id}:
            raise ValueError("document task request conflicts with existing template")
        if knowledge_base_name and existing.knowledge_base_name not in {None, knowledge_base_name}:
            raise ValueError("document task request conflicts with existing knowledge base")
        if project_id and existing.project_id not in {None, project_id}:
            raise ValueError("document task request conflicts with existing project")


def _row_to_task(row: sqlite3.Row) -> DocumentTask:
    payload = _load(row["payload_json"], {})
    if not isinstance(payload, dict):
        raise ValueError("document task payload must be an object")
    return DocumentTask.model_validate(payload)


__all__ = [
    "DocumentTask", "DocumentTaskService", "DocumentTaskStore", "TASK_ORIGINS", "TASK_STATUSES",
]
