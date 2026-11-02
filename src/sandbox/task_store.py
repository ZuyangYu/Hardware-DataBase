"""Durable task ledger for the HDB sandbox control plane.

This store is intentionally independent from the existing conversation and
MCP stores.  It records governance metadata and manifests, never runtime
commands or secrets.  SQLite is a supported same-host P0 control-plane
backend; the API is written so a transactional PostgreSQL adapter can replace
it later without changing the state-machine contract.
"""

from __future__ import annotations

import json
import os
import sqlite3
import re
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import src.settings as settings

from .models import (
    TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    UNCERTAIN_TASK_STATUSES,
    SandboxTask,
    SandboxTaskSpec,
    canonical_json,
)
from .policy import validate_task_spec
from .migrations.v1_task_runtime import migrate_runtime_events
from .runtime_contract import RuntimeContractError, parse_skill_result, validate_skill_event

_ARTIFACT_ID_RE = re.compile(r"^art-[a-f0-9]{16,64}$")


class SandboxTaskStoreError(RuntimeError):
    """Base error for task-ledger operations."""


class TaskNotFound(SandboxTaskStoreError, KeyError):
    pass


class TaskConflict(SandboxTaskStoreError):
    pass


class IdempotencyConflict(TaskConflict):
    pass


class InvalidTransition(TaskConflict):
    pass


class LeaseLost(TaskConflict):
    pass


class TaskAlreadyTerminal(TaskConflict):
    pass


_DDL = """
CREATE TABLE IF NOT EXISTS sandbox_tasks (
    task_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    department_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    bundle_ref TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    profile_ref TEXT NOT NULL,
    policy_snapshot_hash TEXT NOT NULL,
    skill_ref TEXT NOT NULL DEFAULT '',
    skill_content_digest TEXT NOT NULL DEFAULT '',
    skill_version TEXT NOT NULL DEFAULT '',
    runtime_contract_version TEXT NOT NULL DEFAULT 'v1',
    parent_task_id TEXT NOT NULL DEFAULT '',
    resume_from TEXT NOT NULL DEFAULT '',
    capability_snapshot_hash TEXT NOT NULL DEFAULT '',
    network_policy_ref TEXT NOT NULL DEFAULT '',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    output_manifest_json TEXT NOT NULL DEFAULT '{}',
    result_status TEXT NOT NULL DEFAULT '',
    skill_result_json TEXT NOT NULL DEFAULT '{}',
    latest_step_id TEXT NOT NULL DEFAULT '',
    latest_step_status TEXT NOT NULL DEFAULT '',
    latest_progress REAL NOT NULL DEFAULT -1,
    latest_event_seq INTEGER NOT NULL DEFAULT 0,
    latest_event_attempt INTEGER NOT NULL DEFAULT 0,
    latest_checkpoint_artifact_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'admitted',
    desired_state TEXT NOT NULL DEFAULT 'running',
    observed_state TEXT NOT NULL DEFAULT '',
    cleanup_state TEXT NOT NULL DEFAULT 'pending',
    execution_id TEXT NOT NULL DEFAULT '',
    backend_ref TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    fencing_token INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    deadline_at TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sandbox_task_idempotency
    ON sandbox_tasks(owner_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_sandbox_task_queue
    ON sandbox_tasks(status, deadline_at, created_at);
CREATE INDEX IF NOT EXISTS idx_sandbox_task_lease
    ON sandbox_tasks(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_sandbox_task_scope
    ON sandbox_tasks(owner_id, department_id, created_at);

CREATE TABLE IF NOT EXISTS sandbox_task_runtime_events (
    runtime_event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    skill_content_digest TEXT NOT NULL DEFAULT '',
    runtime_contract_version TEXT NOT NULL DEFAULT 'v1',
    seq INTEGER NOT NULL,
    step_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    progress REAL,
    message TEXT NOT NULL DEFAULT '',
    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    heartbeat INTEGER NOT NULL DEFAULT 0,
    emitted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES sandbox_tasks(task_id) ON DELETE CASCADE,
    UNIQUE(task_id, attempt, seq)
);
CREATE INDEX IF NOT EXISTS idx_sandbox_task_runtime_events_task
    ON sandbox_task_runtime_events(task_id, attempt, seq);

CREATE TABLE IF NOT EXISTS sandbox_task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    fencing_token INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES sandbox_tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sandbox_task_events_task
    ON sandbox_task_events(task_id, event_id);
"""

_TRANSITIONS: dict[str, frozenset[str]] = {
    "admitted": frozenset({"queued", "rejected", "expired", "cancel_requested", "cancelled"}),
    "queued": frozenset({"running", "rejected", "expired", "cancel_requested", "cancelled", "reconcile_required"}),
    "running": frozenset({"collecting", "failed", "timed_out", "cancel_requested", "unknown", "reconcile_required"}),
    "collecting": frozenset({"succeeded", "failed", "cleanup_pending", "unknown", "reconcile_required"}),
    "cancel_requested": frozenset({"cancelled", "unknown", "cleanup_pending", "reconcile_required"}),
    "timed_out": frozenset({"cleanup_pending", "unknown", "reconcile_required"}),
    "unknown": frozenset({"cleanup_pending", "reconcile_required", "succeeded", "failed", "cancelled"}),
    "cleanup_pending": frozenset({"cleanup_pending", "reconcile_required", "succeeded", "failed", "cancelled", "unknown"}),
    "reconcile_required": frozenset({"cleanup_pending", "succeeded", "failed", "cancelled", "unknown"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}

_ACTIVE_STATUSES = frozenset({"admitted", "queued", "running", "collecting", "cancel_requested", "timed_out"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value: Any, default: Any) -> str:
    try:
        return canonical_json(value)
    except (TypeError, ValueError):
        return canonical_json(default)


def _load_json(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep events useful without allowing secret/content fields into the ledger."""

    blocked = {
        "secret",
        "token",
        "password",
        "authorization",
        "headers",
        "command",
        "shell",
        "script",
        "content",
        "body",
        "response",
        "parameters",
        "env",
    }

    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 3:
            return "<truncated>"
        if isinstance(item, Mapping):
            return {
                str(key)[:80]: clean(child, depth + 1)
                for key, child in item.items()
                if str(key).strip().lower() not in blocked
            }
        if isinstance(item, (list, tuple, set)):
            return [clean(child, depth + 1) for child in list(item)[:50]]
        if item is None or isinstance(item, (str, int, float, bool)):
            return str(item)[:300] if isinstance(item, str) else item
        return str(item)[:300]

    return clean(dict(value or {}))


def _safe_error_message(value: Any) -> str:
    """Bound runner errors without persisting credentials or host paths."""

    import re

    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = re.sub(
        r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s,;]+)", "<internal-ref>", text)
    return text[:300]


def _safe_manifest(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Persist only bounded artifact references returned by a Runner."""

    raw_items: Any = (value or {}).get("items", (value or {}).get("artifacts", []))
    if isinstance(raw_items, Mapping):
        raw_items = [raw_items]
    if not isinstance(raw_items, (list, tuple)):
        return {}
    items: list[dict[str, Any]] = []
    for raw in list(raw_items)[:256]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in ("artifact_id", "sha256", "state", "role", "filename", "media_type", "size"):
            if key not in raw:
                continue
            value_item = raw[key]
            if key == "size":
                try:
                    item[key] = max(0, min(int(value_item), 2**63 - 1))
                except (TypeError, ValueError):
                    continue
            else:
                item[key] = str(value_item or "")[:256]
        if item.get("artifact_id"):
            items.append(item)
    return {"items": items}


def _new_task_id() -> str:
    return f"task-{uuid.uuid4().hex}"


class SandboxTaskStore:
    """SQLite task ledger with CAS, leases and monotonic fencing tokens."""

    def __init__(self, db_path: str | None = None, *, validate_on_create: bool = True) -> None:
        self.db_path = os.path.abspath(str(db_path or settings.SANDBOX_TASK_DB_PATH))
        self.validate_on_create = bool(validate_on_create)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._init_lock, closing(self._connect()) as conn:
            conn.executescript(_DDL)
            # Forward-compatible migrations for databases created during the
            # early P0 implementation.
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sandbox_tasks)")}
            migrations = {
                "backend_ref": "TEXT NOT NULL DEFAULT ''",
                "observed_state": "TEXT NOT NULL DEFAULT ''",
                "cleanup_state": "TEXT NOT NULL DEFAULT 'pending'",
                "output_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
                "capability_snapshot_hash": "TEXT NOT NULL DEFAULT ''",
                "skill_ref": "TEXT NOT NULL DEFAULT ''",
                "skill_content_digest": "TEXT NOT NULL DEFAULT ''",
                "skill_version": "TEXT NOT NULL DEFAULT ''",
                "runtime_contract_version": "TEXT NOT NULL DEFAULT 'v1'",
                "parent_task_id": "TEXT NOT NULL DEFAULT ''",
                "resume_from": "TEXT NOT NULL DEFAULT ''",
                "result_status": "TEXT NOT NULL DEFAULT ''",
                "skill_result_json": "TEXT NOT NULL DEFAULT '{}'",
                "latest_step_id": "TEXT NOT NULL DEFAULT ''",
                "latest_step_status": "TEXT NOT NULL DEFAULT ''",
                "latest_progress": "REAL NOT NULL DEFAULT -1",
                "latest_event_seq": "INTEGER NOT NULL DEFAULT 0",
                "latest_event_attempt": "INTEGER NOT NULL DEFAULT 0",
                "latest_checkpoint_artifact_id": "TEXT NOT NULL DEFAULT ''",
            }
            for column, ddl in migrations.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE sandbox_tasks ADD COLUMN {column} {ddl}")
            event_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(sandbox_task_runtime_events)")
            }
            migrate_runtime_events(conn, event_columns)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> SandboxTask:
        keys = set(row.keys())

        def get(key: str, default: Any = "") -> Any:
            return row[key] if key in keys and row[key] is not None else default

        return SandboxTask(
            task_id=str(get("task_id")),
            idempotency_key=str(get("idempotency_key")),
            request_fingerprint=str(get("request_fingerprint")),
            owner_id=str(get("owner_id")),
            department_id=str(get("department_id")),
            kind=str(get("kind", "task")),
            bundle_ref=str(get("bundle_ref")),
            bundle_digest=str(get("bundle_digest")),
            profile_ref=str(get("profile_ref")),
            policy_snapshot_hash=str(get("policy_snapshot_hash")),
            skill_ref=str(get("skill_ref")),
            skill_content_digest=str(get("skill_content_digest")),
            skill_version=str(get("skill_version")),
            runtime_contract_version=str(get("runtime_contract_version", "v1")),
            parent_task_id=str(get("parent_task_id")),
            resume_from=str(get("resume_from")),
            capability_snapshot_hash=str(get("capability_snapshot_hash")),
            network_policy_ref=str(get("network_policy_ref")),
            required_capabilities=tuple(_load_json(get("required_capabilities_json", "[]"), [])),
            input_artifact_ids=tuple(_load_json(get("input_artifact_ids_json", "[]"), [])),
            parameters=dict(_load_json(get("parameters_json", "{}"), {}) or {}),
            output_manifest=dict(_load_json(get("output_manifest_json", "{}"), {}) or {}),
            status=str(get("status", "admitted")),
            result_status=str(get("result_status")),
            skill_result=dict(_load_json(get("skill_result_json", "{}"), {}) or {}),
            latest_step_id=str(get("latest_step_id")),
            latest_step_status=str(get("latest_step_status")),
            latest_progress=(
                None if float(get("latest_progress", -1) or -1) < 0
                else float(get("latest_progress", -1))
            ),
            latest_event_seq=int(get("latest_event_seq", 0) or 0),
            latest_event_attempt=int(get("latest_event_attempt", 0) or 0),
            latest_checkpoint_artifact_id=str(get("latest_checkpoint_artifact_id")),
            desired_state=str(get("desired_state", "running")),
            observed_state=str(get("observed_state")),
            cleanup_state=str(get("cleanup_state", "pending")),
            execution_id=str(get("execution_id")),
            backend_ref=str(get("backend_ref")),
            lease_owner=str(get("lease_owner")),
            lease_expires_at=str(get("lease_expires_at")),
            fencing_token=int(get("fencing_token", 0) or 0),
            attempt=int(get("attempt", 0) or 0),
            deadline_at=str(get("deadline_at")),
            error_code=str(get("error_code")),
            error_message=str(get("error_message")),
            revision=int(get("revision", 0) or 0),
            created_at=str(get("created_at")),
            updated_at=str(get("updated_at")),
            started_at=str(get("started_at")),
            finished_at=str(get("finished_at")),
        )

    @staticmethod
    def _select(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM sandbox_tasks WHERE task_id = ?", (str(task_id),)).fetchone()

    def get(self, task_id: str) -> SandboxTask | None:
        with closing(self._connect()) as conn:
            row = self._select(conn, task_id)
        return self._row_to_task(row) if row else None

    def require(self, task_id: str) -> SandboxTask:
        task = self.get(task_id)
        if task is None:
            raise TaskNotFound(f"sandbox task not found: {task_id}")
        return task

    def list(
        self,
        *,
        owner_id: str | int | None = None,
        department_id: str | int | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[SandboxTask]:
        clauses: list[str] = []
        params: list[Any] = []
        if owner_id not in (None, ""):
            clauses.append("owner_id = ?")
            params.append(str(owner_id))
        if department_id not in (None, ""):
            clauses.append("department_id = ?")
            params.append(str(department_id))
        wanted = [str(item) for item in (statuses or ()) if str(item)]
        if wanted:
            unknown = set(wanted) - set(TASK_STATUSES)
            if unknown:
                raise ValueError(f"unknown task statuses: {', '.join(sorted(unknown))}")
            clauses.append("status IN (" + ",".join("?" for _ in wanted) + ")")
            params.extend(wanted)
        params.append(max(1, min(int(limit), 1000)))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM sandbox_tasks{where} ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def create(self, spec: SandboxTaskSpec, *, task_id: str | None = None, actor_id: str = "") -> SandboxTask:
        normalized = validate_task_spec(spec) if self.validate_on_create else spec
        now = _utc_now()
        deadline = normalized.deadline_at or _iso(
            now + timedelta(seconds=max(1, int(getattr(settings, "SANDBOX_TASK_TIMEOUT_SECONDS", 300))))
        )
        task_id = str(task_id or _new_task_id()).strip()
        if not task_id or len(task_id) > 128 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for char in task_id):
            raise ValueError("task_id must be a bounded opaque reference")
        fingerprint = normalized.fingerprint()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM sandbox_tasks WHERE owner_id = ? AND idempotency_key = ?",
                    (normalized.owner_id, normalized.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != fingerprint:
                        raise IdempotencyConflict("idempotency key was already used for another task")
                    conn.execute("COMMIT")
                    return self._row_to_task(existing)
                conn.execute(
                    """
                    INSERT INTO sandbox_tasks(
                        task_id, idempotency_key, request_fingerprint, owner_id, department_id,
                        kind, bundle_ref, bundle_digest, profile_ref, policy_snapshot_hash,
                        capability_snapshot_hash, skill_ref, skill_content_digest,
                        skill_version, runtime_contract_version, parent_task_id, resume_from,
                        network_policy_ref, required_capabilities_json, input_artifact_ids_json,
                        parameters_json, output_manifest_json, result_status, skill_result_json,
                        latest_step_id, latest_step_status, latest_progress, latest_event_seq,
                        latest_event_attempt, latest_checkpoint_artifact_id, status, desired_state,
                        observed_state, cleanup_state, execution_id, backend_ref, lease_owner,
                        lease_expires_at, fencing_token, attempt, deadline_at, error_code,
                        error_message, revision, created_at, updated_at, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, '{}', '', '{}', '', '', -1, 0, 0, '', 'admitted', 'running',
                              '', 'pending', '', '', '', '', 0, 0, ?, '', '', 0, ?, ?, '', '')
                    """,
                    (
                        task_id,
                        normalized.idempotency_key,
                        fingerprint,
                        normalized.owner_id,
                        normalized.department_id,
                        normalized.kind,
                        normalized.bundle_ref,
                        normalized.bundle_digest,
                        normalized.profile_ref,
                        normalized.policy_snapshot_hash,
                        normalized.capability_snapshot_hash,
                        normalized.skill_ref,
                        normalized.skill_content_digest,
                        normalized.skill_version,
                        normalized.runtime_contract_version,
                        normalized.parent_task_id,
                        normalized.resume_from,
                        normalized.network_policy_ref,
                        _json(list(normalized.required_capabilities), []),
                        _json(list(normalized.input_artifact_ids), []),
                        _json(dict(normalized.parameters), {}),
                        deadline,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._event(
                    conn,
                    task_id=task_id,
                    action="sandbox.task_submit",
                    from_status="",
                    to_status="admitted",
                    actor_id=actor_id or normalized.owner_id,
                    revision=0,
                    metadata={"kind": normalized.kind, "bundle_ref": normalized.bundle_ref},
                )
                row = self._select(conn, task_id)
                conn.execute("COMMIT")
                if row is None:  # pragma: no cover - defensive
                    raise SandboxTaskStoreError("task insert did not return a row")
                return self._row_to_task(row)
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                # A concurrent retry may have won the unique idempotency
                # insert. Resolve it exactly like the in-transaction replay
                # path instead of leaking a database-specific error.
                with closing(self._connect()) as retry_conn:
                    existing = retry_conn.execute(
                        "SELECT * FROM sandbox_tasks WHERE owner_id = ? AND idempotency_key = ?",
                        (normalized.owner_id, normalized.idempotency_key),
                    ).fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != fingerprint:
                        raise IdempotencyConflict("idempotency key was already used for another task")
                    return self._row_to_task(existing)
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    submit = create

    def bind_inputs(
        self,
        task_id: str,
        artifact_ids: tuple[str, ...],
        *,
        expected_revision: int | None = None,
        actor_id: str = "",
    ) -> SandboxTask:
        """Atomically bind committed inputs before a task is dispatched.

        The task fingerprint is re-derived because input references are part of
        the request identity; replaying the original idempotency key with a
        different input set must never silently reuse the old task.
        """

        values = tuple(str(item).strip() for item in artifact_ids)
        if len(set(values)) != len(values) or any(not _ARTIFACT_ID_RE.fullmatch(item) for item in values):
            raise ValueError("input artifacts must be unique opaque IDs")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                if expected_revision is not None and current.revision != int(expected_revision):
                    raise TaskConflict("task revision changed")
                if current.status not in {"admitted", "queued"} or current.execution_id:
                    raise InvalidTransition("inputs can only be bound before execution starts")
                spec = SandboxTaskSpec(
                    idempotency_key=current.idempotency_key,
                    owner_id=current.owner_id,
                    department_id=current.department_id,
                    bundle_ref=current.bundle_ref,
                    bundle_digest=current.bundle_digest,
                    profile_ref=current.profile_ref,
                    policy_snapshot_hash=current.policy_snapshot_hash,
                    capability_snapshot_hash=current.capability_snapshot_hash,
                    kind=current.kind,
                    network_policy_ref=current.network_policy_ref,
                    required_capabilities=current.required_capabilities,
                    input_artifact_ids=values,
                    parameters=current.parameters,
                    deadline_at=current.deadline_at,
                    skill_ref=current.skill_ref,
                    skill_content_digest=current.skill_content_digest,
                    skill_version=current.skill_version,
                    runtime_contract_version=current.runtime_contract_version,
                    parent_task_id=current.parent_task_id,
                    resume_from=current.resume_from,
                )
                if "sandbox.internal_data" not in current.required_capabilities and values:
                    raise InvalidTransition("task capability snapshot does not allow input artifacts")
                fingerprint = spec.fingerprint()
                now = _utc_now().isoformat()
                revision = current.revision + 1
                conn.execute(
                    "UPDATE sandbox_tasks SET input_artifact_ids_json = ?, request_fingerprint = ?, "
                    "revision = ?, updated_at = ? WHERE task_id = ? AND revision = ?",
                    (
                        _json(list(values), []),
                        fingerprint,
                        revision,
                        now,
                        current.task_id,
                        current.revision,
                    ),
                )
                self._event(
                    conn,
                    task_id=current.task_id,
                    action="sandbox.task_bind_inputs",
                    from_status=current.status,
                    to_status=current.status,
                    actor_id=actor_id or current.owner_id,
                    revision=revision,
                    metadata={"input_artifact_ids": list(values)},
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                return self._row_to_task(updated) if updated else current
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def enqueue(self, task_id: str, *, expected_revision: int | None = None, actor_id: str = "") -> SandboxTask:
        return self.transition(
            task_id,
            "queued",
            expected_revision=expected_revision,
            actor_id=actor_id,
            action="sandbox.task_queue",
        )

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        action: str,
        from_status: str,
        to_status: str,
        actor_id: str = "",
        lease_owner: str = "",
        fencing_token: int = 0,
        revision: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        event_metadata = _safe_metadata(metadata)
        lineage = conn.execute(
            "SELECT bundle_digest, skill_content_digest, runtime_contract_version "
            "FROM sandbox_tasks WHERE task_id = ?",
            (str(task_id),),
        ).fetchone()
        if lineage is not None:
            event_metadata.setdefault("bundle_digest", str(lineage["bundle_digest"] or ""))
            event_metadata.setdefault(
                "skill_content_digest", str(lineage["skill_content_digest"] or "")
            )
            event_metadata.setdefault(
                "runtime_contract_version",
                str(lineage["runtime_contract_version"] or "v1"),
            )
        try:
            from src.observability.context import current_trace_identity

            trace_identity = current_trace_identity()
            if trace_identity.trace_id:
                event_metadata.setdefault("trace_id", trace_identity.trace_id)
            if trace_identity.span_id:
                event_metadata.setdefault("span_id", trace_identity.span_id)
        except Exception:
            pass
        conn.execute(
            """
            INSERT INTO sandbox_task_events(
                task_id, action, from_status, to_status, actor_id, lease_owner,
                fencing_token, revision, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                str(action)[:100],
                str(from_status)[:50],
                str(to_status)[:50],
                str(actor_id or "")[:128],
                str(lease_owner or "")[:128],
                int(fencing_token or 0),
                int(revision or 0),
                _json(event_metadata, {}),
                _iso(),
            ),
        )

    def events(
        self,
        task_id: str,
        *,
        limit: int = 200,
        include_control: bool = False,
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM sandbox_task_events WHERE task_id = ? ORDER BY event_id DESC LIMIT ?",
                (str(task_id), max(1, min(int(limit), 1000))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = {
                "event_id": int(row["event_id"]),
                "task_id": str(row["task_id"]),
                "action": str(row["action"]),
                "from_status": str(row["from_status"]),
                "to_status": str(row["to_status"]),
                "actor_id": str(row["actor_id"]),
                "revision": int(row["revision"] or 0),
                "metadata": _load_json(row["metadata_json"], {}),
                "created_at": str(row["created_at"]),
            }
            if include_control:
                item.update(
                    {
                        "lease_owner": str(row["lease_owner"]),
                        "fencing_token": int(row["fencing_token"] or 0),
                    }
                )
            result.append(item)
        return result

    def append_runtime_event(
        self,
        task_id: str,
        value: Mapping[str, Any],
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
        actor_id: str = "",
    ) -> SandboxTask:
        """Validate and durably append one Skill runtime event.

        Runtime events are separate from task lifecycle transitions so a Skill
        can report step progress without widening the task state machine.  The
        task's latest step projection is updated in the same transaction.
        """

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                if require_lease:
                    self._assert_lease(current, lease_owner, fencing_token)
                event = validate_skill_event(
                    value,
                    task_id=current.task_id,
                    attempt=current.attempt,
                )
                if current.latest_event_attempt > event.attempt:
                    raise RuntimeContractError("runtime event attempt is older than the task projection")
                now = _utc_now().isoformat()
                artifact_ids = list(event.artifact_ids)
                metadata = _safe_metadata(event.metadata)
                # event_id is only required to be unique inside one task's
                # event stream.  Namespace the physical primary key so two
                # tasks may legitimately reuse evt-1, evt-2, ...
                storage_event_id = f"{current.task_id}:{event.attempt}:{event.event_id}"
                existing = conn.execute(
                    "SELECT * FROM sandbox_task_runtime_events "
                    "WHERE task_id = ? AND attempt = ? AND seq = ?",
                    (current.task_id, event.attempt, event.seq),
                ).fetchone()
                if existing is not None:
                    # Polling an observation may replay the same snapshot.  A
                    # byte-identical event is idempotent; a different payload
                    # for the same (attempt, seq) is a contract conflict.
                    same = (
                        str(existing["runtime_event_id"]) == storage_event_id
                        and str(existing["step_id"]) == event.step_id
                        and str(existing["event_type"]) == event.event
                        and str(existing["status"]) == event.status
                        and existing["progress"] == event.progress
                        and str(existing["message"]) == event.message
                        and _load_json(existing["artifact_ids_json"], []) == artifact_ids
                        and _load_json(existing["metadata_json"], {}) == metadata
                    )
                    if same:
                        conn.execute("COMMIT")
                        return current
                    raise RuntimeContractError("runtime event sequence conflicts with a stored event")
                max_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM sandbox_task_runtime_events "
                    "WHERE task_id = ? AND attempt = ?",
                    (current.task_id, event.attempt),
                ).fetchone()
                if event.seq <= int(max_row["max_seq"] or 0):
                    raise RuntimeContractError("event sequence must be strictly increasing")
                checkpoint = ""
                if event.event == "checkpoint_created" and artifact_ids:
                    checkpoint = artifact_ids[0]
                latest_progress = -1 if event.progress is None else event.progress
                revision = current.revision + 1
                conn.execute(
                    """
                    INSERT INTO sandbox_task_runtime_events(
                        runtime_event_id, task_id, attempt, fencing_token,
                        skill_content_digest, runtime_contract_version, seq, step_id, event_type,
                        status, progress, message, artifact_ids_json, metadata_json,
                        heartbeat, emitted_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storage_event_id,
                        current.task_id,
                        event.attempt,
                        current.fencing_token,
                        current.skill_content_digest,
                        current.runtime_contract_version,
                        event.seq,
                        event.step_id,
                        event.event,
                        event.status,
                        event.progress,
                        event.message,
                        _json(artifact_ids, []),
                        _json(metadata, {}),
                        1 if event.heartbeat else 0,
                        event.emitted_at,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE sandbox_tasks
                    SET latest_step_id = ?, latest_step_status = ?,
                        latest_progress = CASE WHEN ? >= 0 THEN ? ELSE latest_progress END,
                        latest_event_seq = ?, latest_event_attempt = ?,
                        latest_checkpoint_artifact_id = CASE
                            WHEN ? <> '' THEN ? ELSE latest_checkpoint_artifact_id END,
                        revision = ?, updated_at = ?
                    WHERE task_id = ? AND revision = ?
                    """,
                    (
                        event.step_id,
                        event.status or event.event,
                        latest_progress,
                        latest_progress,
                        event.seq,
                        event.attempt,
                        checkpoint,
                        checkpoint,
                        revision,
                        now,
                        current.task_id,
                        current.revision,
                    ),
                )
                self._event(
                    conn,
                    task_id=current.task_id,
                    action=f"skill.{event.event}",
                    from_status=current.status,
                    to_status=current.status,
                    actor_id=actor_id or lease_owner or current.owner_id,
                    lease_owner=str(lease_owner or current.lease_owner),
                    fencing_token=int(fencing_token if fencing_token is not None else current.fencing_token),
                    revision=revision,
                    metadata={
                        "runtime_event_id": event.event_id,
                        "seq": event.seq,
                        "attempt": event.attempt,
                        "step_id": event.step_id,
                        "status": event.status,
                        "progress": event.progress,
                        "artifact_ids": list(event.artifact_ids),
                        "heartbeat": event.heartbeat,
                    },
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                if updated is None:  # pragma: no cover - defensive
                    raise SandboxTaskStoreError("task disappeared after runtime event")
                return self._row_to_task(updated)
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise RuntimeContractError("runtime event sequence or event_id already exists") from exc
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def runtime_events(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return validated Skill events in ascending sequence order."""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM sandbox_task_runtime_events "
                "WHERE task_id = ? ORDER BY attempt ASC, seq ASC LIMIT ?",
                (str(task_id), max(1, min(int(limit), 1000))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            task_id = str(row["task_id"])
            attempt = int(row["attempt"] or 0)
            prefix = f"{task_id}:{attempt}:"
            stored_event_id = str(row["runtime_event_id"])
            result.append(
                {
                    "event_id": stored_event_id.removeprefix(prefix),
                    "task_id": task_id,
                    "attempt": int(row["attempt"] or 0),
                    "fencing_token": int(row["fencing_token"] or 0)
                    if "fencing_token" in row.keys()
                    else 0,
                    "skill_content_digest": str(row["skill_content_digest"] or "")
                    if "skill_content_digest" in row.keys()
                    else "",
                    "runtime_contract_version": str(row["runtime_contract_version"] or "v1")
                    if "runtime_contract_version" in row.keys()
                    else "v1",
                    "seq": int(row["seq"] or 0),
                    "step_id": str(row["step_id"]),
                    "event": str(row["event_type"]),
                    "status": str(row["status"]),
                    "progress": row["progress"],
                    "message": str(row["message"]),
                    "artifact_ids": _load_json(row["artifact_ids_json"], []),
                    "metadata": _load_json(row["metadata_json"], {}),
                    "heartbeat": bool(row["heartbeat"]),
                    "emitted_at": str(row["emitted_at"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def record_result(
        self,
        task_id: str,
        value: Mapping[str, Any],
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
        actor_id: str = "",
    ) -> SandboxTask:
        """Persist a Skill business result independently of task execution state."""

        result = parse_skill_result(value)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                if require_lease:
                    self._assert_lease(current, lease_owner, fencing_token)
                serialized = result.to_dict()
                if current.result_status == result.result_status and dict(current.skill_result) == serialized:
                    conn.execute("COMMIT")
                    return current
                now = _utc_now().isoformat()
                revision = current.revision + 1
                checkpoint = result.checkpoint_artifact_id
                conn.execute(
                    """
                    UPDATE sandbox_tasks
                    SET result_status = ?, skill_result_json = ?,
                        latest_checkpoint_artifact_id = CASE WHEN ? <> '' THEN ? ELSE latest_checkpoint_artifact_id END,
                        revision = ?, updated_at = ?
                    WHERE task_id = ? AND revision = ?
                    """,
                    (
                        result.result_status,
                        _json(serialized, {}),
                        checkpoint,
                        checkpoint,
                        revision,
                        now,
                        current.task_id,
                        current.revision,
                    ),
                )
                self._event(
                    conn,
                    task_id=current.task_id,
                    action="skill.result",
                    from_status=current.status,
                    to_status=current.status,
                    actor_id=actor_id or lease_owner or current.owner_id,
                    lease_owner=str(lease_owner or current.lease_owner),
                    fencing_token=int(fencing_token if fencing_token is not None else current.fencing_token),
                    revision=revision,
                    metadata={
                        "result_status": result.result_status,
                        "error_code": result.error_code,
                        "issue_count": len(result.issues),
                        "artifact_ids": list(result.artifact_ids),
                        "required_inputs": list(result.required_inputs),
                        "required_tools": list(result.required_tools),
                        "checkpoint_artifact_id": checkpoint,
                    },
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                if updated is None:  # pragma: no cover - defensive
                    raise SandboxTaskStoreError("task disappeared after result")
                return self._row_to_task(updated)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def transition(
        self,
        task_id: str,
        to_status: str,
        *,
        expected_revision: int | None = None,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
        actor_id: str = "",
        action: str = "sandbox.task_transition",
        observed_state: str | None = None,
        cleanup_state: str | None = None,
        desired_state: str | None = None,
        execution_id: str | None = None,
        backend_ref: str | None = None,
        output_manifest: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SandboxTask:
        target = str(to_status or "").strip()
        if target not in TASK_STATUSES:
            raise InvalidTransition(f"unknown task status: {target}")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                if expected_revision is not None and current.revision != int(expected_revision):
                    raise TaskConflict("task revision changed")
                if require_lease:
                    self._assert_lease(current, lease_owner, fencing_token)
                if current.status == target:
                    # Idempotent callbacks are safe, but a caller cannot use a
                    # repeated status to smuggle a different terminal result.
                    if output_manifest is not None and dict(output_manifest) != dict(current.output_manifest):
                        raise TaskConflict("task already has a different result manifest")
                    conn.execute("COMMIT")
                    return current
                allowed = _TRANSITIONS.get(current.status, frozenset())
                if target not in allowed:
                    if current.status in TERMINAL_TASK_STATUSES:
                        raise TaskAlreadyTerminal(f"task is already terminal: {current.status}")
                    raise InvalidTransition(f"cannot transition {current.status} -> {target}")
                if target == "succeeded":
                    manifest = _safe_manifest(output_manifest or current.output_manifest or {})
                    empty_interaction = not manifest.get("items") and current.result_status in {
                        "needs_user_input",
                        "needs_tool",
                    }
                    if not empty_interaction and (not manifest or not _manifest_committed(manifest)):
                        raise TaskConflict("task cannot succeed before committed output artifacts are recorded")
                    output_manifest = manifest
                now = _utc_now()
                values: dict[str, Any] = {
                    "status": target,
                    "updated_at": now.isoformat(),
                    "revision": current.revision + 1,
                }
                if observed_state is not None:
                    values["observed_state"] = str(observed_state)[:200]
                if cleanup_state is not None:
                    values["cleanup_state"] = str(cleanup_state)[:50]
                if desired_state is not None:
                    values["desired_state"] = str(desired_state)[:50]
                if execution_id is not None:
                    values["execution_id"] = str(execution_id)[:256]
                if backend_ref is not None:
                    values["backend_ref"] = str(backend_ref)[:256]
                if output_manifest is not None:
                    values["output_manifest_json"] = _json(_safe_manifest(output_manifest), {})
                if error_code is not None:
                    values["error_code"] = str(error_code)[:80]
                if error_message is not None:
                    values["error_message"] = _safe_error_message(error_message)
                if target in {"succeeded", "rejected", "failed", "cancelled", "expired"}:
                    values["finished_at"] = now.isoformat()
                    values["lease_owner"] = ""
                    values["lease_expires_at"] = ""
                elif target in UNCERTAIN_TASK_STATUSES:
                    # Once a task is uncertain, the old worker must no longer
                    # be able to mutate it with its previous lease.  A future
                    # reconciliation gets a fresh, explicit decision path.
                    values["lease_owner"] = ""
                    values["lease_expires_at"] = ""
                assignments = ", ".join(f"{key} = ?" for key in values)
                params = list(values.values()) + [str(task_id), current.revision]
                cursor = conn.execute(
                    f"UPDATE sandbox_tasks SET {assignments} WHERE task_id = ? AND revision = ?",
                    params,
                )
                if int(cursor.rowcount or 0) != 1:
                    raise TaskConflict("task revision changed")
                transition_metadata = dict(metadata or {})
                if output_manifest is not None:
                    transition_metadata.setdefault(
                        "artifact_digests",
                        [
                            str(item.get("sha256") or "")
                            for item in (_safe_manifest(output_manifest).get("items") or [])
                            if isinstance(item, Mapping) and item.get("sha256")
                        ][:64],
                    )
                self._event(
                    conn,
                    task_id=str(task_id),
                    action=action,
                    from_status=current.status,
                    to_status=target,
                    actor_id=actor_id,
                    lease_owner=str(lease_owner or current.lease_owner),
                    fencing_token=int(fencing_token if fencing_token is not None else current.fencing_token),
                    revision=current.revision + 1,
                    metadata=transition_metadata,
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                if updated is None:  # pragma: no cover
                    raise SandboxTaskStoreError("task disappeared after transition")
                return self._row_to_task(updated)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _assert_lease(task: SandboxTask, lease_owner: str | None, fencing_token: int | None) -> None:
        if not lease_owner or fencing_token is None:
            raise LeaseLost("lease owner and fencing token are required")
        if task.lease_owner != str(lease_owner) or task.fencing_token != int(fencing_token):
            raise LeaseLost("task lease or fencing token is stale")
        expires = _parse_time(task.lease_expires_at)
        if expires is None or expires <= _utc_now():
            raise LeaseLost("task lease has expired")

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: int | None = None,
        actor_id: str = "",
    ) -> SandboxTask | None:
        owner = str(worker_id or "").strip()
        if not owner:
            raise ValueError("worker_id is required")
        duration = max(1, int(lease_seconds or getattr(settings, "SANDBOX_TASK_LEASE_SECONDS", 60)))
        now = _utc_now()
        expires = _iso(now + timedelta(seconds=duration))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                if current.status not in {"admitted", "queued"}:
                    if current.status in TERMINAL_TASK_STATUSES or current.status in UNCERTAIN_TASK_STATUSES:
                        conn.execute("COMMIT")
                        return None
                    raise InvalidTransition(f"cannot claim task in {current.status}")
                deadline = _parse_time(current.deadline_at)
                if deadline is not None and deadline <= now:
                    revision = current.revision + 1
                    conn.execute(
                        "UPDATE sandbox_tasks SET status = 'expired', desired_state = 'cancelled', "
                        "error_code = 'deadline_expired', finished_at = ?, revision = ?, updated_at = ? "
                        "WHERE task_id = ? AND revision = ? AND status IN ('admitted','queued')",
                        (now.isoformat(), revision, now.isoformat(), str(task_id), current.revision),
                    )
                    self._event(
                        conn,
                        task_id=str(task_id),
                        action="sandbox.task_expire",
                        from_status=current.status,
                        to_status="expired",
                        actor_id="scheduler",
                        revision=revision,
                        metadata={"reason": "deadline_expired"},
                    )
                    conn.execute("COMMIT")
                    return None
                live = _parse_time(current.lease_expires_at)
                if current.lease_owner and live and live > now and current.lease_owner != owner:
                    conn.execute("COMMIT")
                    return None
                max_concurrent = max(1, int(getattr(settings, "SANDBOX_MAX_CONCURRENT", 2)))
                active = conn.execute(
                    "SELECT COUNT(*) AS count FROM sandbox_tasks "
                    "WHERE status IN ('running','collecting','cancel_requested','timed_out') "
                    "AND lease_expires_at <> '' AND lease_expires_at > ?",
                    (now.isoformat(),),
                ).fetchone()
                if int(active["count"] or 0) >= max_concurrent:
                    conn.execute("COMMIT")
                    return None
                user_limit = max(
                    1,
                    int(getattr(settings, "SANDBOX_MAX_CONCURRENT_PER_USER", 1)),
                )
                user_active = conn.execute(
                    "SELECT COUNT(*) AS count FROM sandbox_tasks "
                    "WHERE owner_id = ? AND status IN ('running','collecting','cancel_requested','timed_out') "
                    "AND lease_expires_at <> '' AND lease_expires_at > ?",
                    (current.owner_id, now.isoformat()),
                ).fetchone()
                if int(user_active["count"] or 0) >= user_limit:
                    conn.execute("COMMIT")
                    return None
                department_limit = max(
                    1,
                    int(getattr(settings, "SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT", max_concurrent)),
                )
                department_active = conn.execute(
                    "SELECT COUNT(*) AS count FROM sandbox_tasks "
                    "WHERE department_id = ? AND status IN ('running','collecting','cancel_requested','timed_out') "
                    "AND lease_expires_at <> '' AND lease_expires_at > ?",
                    (current.department_id, now.isoformat()),
                ).fetchone()
                if int(department_active["count"] or 0) >= department_limit:
                    conn.execute("COMMIT")
                    return None
                token = current.fencing_token + 1
                revision = current.revision + 1
                cursor = conn.execute(
                    """
                    UPDATE sandbox_tasks
                    SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                        fencing_token = ?, attempt = attempt + 1, revision = ?,
                        started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                        updated_at = ?, observed_state = ''
                    WHERE task_id = ? AND revision = ? AND status IN ('admitted', 'queued')
                    """,
                    (owner, expires, token, revision, now.isoformat(), now.isoformat(), str(task_id), current.revision),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise TaskConflict("task was claimed by another worker")
                self._event(
                    conn,
                    task_id=str(task_id),
                    action="sandbox.task_start",
                    from_status=current.status,
                    to_status="running",
                    actor_id=actor_id or owner,
                    lease_owner=owner,
                    fencing_token=token,
                    revision=revision,
                    metadata={"attempt": current.attempt + 1},
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                return self._row_to_task(updated) if updated else None
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int | None = None,
        limit: int = 1,
        department_id: str | int | None = None,
    ) -> list[SandboxTask]:
        clauses = ["status IN ('admitted', 'queued')"]
        params: list[Any] = []
        if department_id not in (None, ""):
            clauses.append("department_id = ?")
            params.append(str(department_id))
        params.append(max(1, min(int(limit), 100)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT task_id FROM sandbox_tasks WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
        claimed: list[SandboxTask] = []
        for row in rows:
            item = self.claim(str(row["task_id"]), worker_id, lease_seconds=lease_seconds)
            if item is not None:
                claimed.append(item)
        return claimed

    def renew_lease(
        self,
        task_id: str,
        lease_owner: str,
        fencing_token: int,
        *,
        lease_seconds: int | None = None,
    ) -> SandboxTask:
        duration = max(1, int(lease_seconds or getattr(settings, "SANDBOX_TASK_LEASE_SECONDS", 60)))
        now = _utc_now()
        expires = _iso(now + timedelta(seconds=duration))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                self._assert_lease(current, lease_owner, fencing_token)
                revision = current.revision + 1
                cursor = conn.execute(
                    "UPDATE sandbox_tasks SET lease_expires_at = ?, revision = ?, updated_at = ? "
                    "WHERE task_id = ? AND revision = ? AND lease_owner = ? AND fencing_token = ?",
                    (expires, revision, now.isoformat(), str(task_id), current.revision, str(lease_owner), int(fencing_token)),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise LeaseLost("task lease changed while renewing")
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                if updated is None:
                    raise TaskNotFound(str(task_id))
                return self._row_to_task(updated)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    heartbeat = renew_lease

    def request_cancel(self, task_id: str, *, actor_id: str = "", reason: str = "") -> SandboxTask:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                if current.status in TERMINAL_TASK_STATUSES:
                    conn.execute("COMMIT")
                    return current
                if current.status in UNCERTAIN_TASK_STATUSES or current.status == "timed_out":
                    # Preserve uncertainty: a cancellation request is recorded
                    # as desired state, but does not fabricate a stopped
                    # execution.
                    if current.desired_state == "cancelled":
                        conn.execute("COMMIT")
                        return current
                    revision = current.revision + 1
                    now = _utc_now().isoformat()
                    conn.execute(
                        "UPDATE sandbox_tasks SET desired_state = 'cancelled', updated_at = ?, revision = ? "
                        "WHERE task_id = ? AND revision = ?",
                        (now, revision, str(task_id), current.revision),
                    )
                    self._event(
                        conn,
                        task_id=str(task_id),
                        action="sandbox.task_cancel",
                        from_status=current.status,
                        to_status=current.status,
                        actor_id=actor_id,
                        fencing_token=current.fencing_token,
                        revision=revision,
                        metadata={"reason": reason or "", "confirmed": False},
                    )
                    updated = self._select(conn, task_id)
                    conn.execute("COMMIT")
                    return self._row_to_task(updated) if updated else current
                target = "cancel_requested"
                if current.status == target:
                    conn.execute("COMMIT")
                    return current
                # No Runner instance exists for an admitted/queued task, so a
                # queue cancellation is a confirmed cancellation rather than
                # an unbounded cancel request.
                if current.status in {"admitted", "queued"}:
                    target = "cancelled"
                if target not in _TRANSITIONS.get(current.status, frozenset()):
                    raise InvalidTransition(f"cannot cancel task in {current.status}")
                revision = current.revision + 1
                now = _utc_now().isoformat()
                cursor = conn.execute(
                    "UPDATE sandbox_tasks SET desired_state = 'cancelled', status = ?, "
                    "error_code = CASE WHEN error_code = '' THEN 'cancel_requested' ELSE error_code END, "
                    "error_message = ?, finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END, "
                    "lease_owner = CASE WHEN ? = 'cancelled' THEN '' ELSE lease_owner END, "
                    "lease_expires_at = CASE WHEN ? = 'cancelled' THEN '' ELSE lease_expires_at END, "
                    "revision = ?, updated_at = ? "
                    "WHERE task_id = ? AND revision = ?",
                    (
                        target,
                        _safe_error_message(reason),
                        target,
                        now,
                        target,
                        target,
                        revision,
                        now,
                        str(task_id),
                        current.revision,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise TaskConflict("task revision changed")
                self._event(
                    conn,
                    task_id=str(task_id),
                    action="sandbox.task_cancel",
                    from_status=current.status,
                    to_status=target,
                    actor_id=actor_id,
                    lease_owner=current.lease_owner,
                    fencing_token=current.fencing_token,
                    revision=revision,
                    metadata={"reason": reason or "", "confirmed": target == "cancelled"},
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                return self._row_to_task(updated) if updated else current
            except Exception:
                conn.execute("ROLLBACK")
                raise

    cancel = request_cancel

    def record_observation(
        self,
        task_id: str,
        *,
        lease_owner: str,
        fencing_token: int,
        observed_state: str,
        execution_id: str = "",
        backend_ref: str = "",
        cleanup_state: str = "",
        expected_revision: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SandboxTask:
        """Persist a Runner observation without allowing it to widen authority."""

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(f"sandbox task not found: {task_id}")
                current = self._row_to_task(row)
                if expected_revision is not None and current.revision != int(expected_revision):
                    raise TaskConflict("task revision changed")
                self._assert_lease(current, lease_owner, fencing_token)
                revision = current.revision + 1
                now = _utc_now().isoformat()
                cursor = conn.execute(
                    "UPDATE sandbox_tasks SET observed_state = ?, execution_id = CASE WHEN ? <> '' THEN ? ELSE execution_id END, "
                    "backend_ref = CASE WHEN ? <> '' THEN ? ELSE backend_ref END, "
                    "cleanup_state = CASE WHEN ? <> '' THEN ? ELSE cleanup_state END, revision = ?, updated_at = ? "
                    "WHERE task_id = ? AND revision = ? AND lease_owner = ? AND fencing_token = ?",
                    (
                        str(observed_state or "")[:200],
                        str(execution_id or ""),
                        str(execution_id or "")[:256],
                        str(backend_ref or ""),
                        str(backend_ref or "")[:256],
                        str(cleanup_state or ""),
                        str(cleanup_state or "")[:50],
                        revision,
                        now,
                        str(task_id),
                        current.revision,
                        str(lease_owner),
                        int(fencing_token),
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise LeaseLost("task lease changed while recording observation")
                self._event(
                    conn,
                    task_id=str(task_id),
                    action="sandbox.task_observe",
                    from_status=current.status,
                    to_status=current.status,
                    actor_id=lease_owner,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                    revision=revision,
                    metadata=metadata,
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                return self._row_to_task(updated) if updated else current
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def complete(
        self,
        task_id: str,
        *,
        lease_owner: str,
        fencing_token: int,
        output_manifest: Mapping[str, Any],
        expected_revision: int | None = None,
        actor_id: str = "",
    ) -> SandboxTask:
        return self.transition(
            task_id,
            "succeeded",
            expected_revision=expected_revision,
            lease_owner=lease_owner,
            fencing_token=fencing_token,
            require_lease=True,
            actor_id=actor_id or lease_owner,
            action="sandbox.task_complete",
            output_manifest=output_manifest,
            observed_state="succeeded",
            cleanup_state="complete",
        )

    def fail(
        self,
        task_id: str,
        *,
        lease_owner: str,
        fencing_token: int,
        error_code: str = "runner_failed",
        error_message: str = "",
        expected_revision: int | None = None,
        output_manifest: Mapping[str, Any] | None = None,
    ) -> SandboxTask:
        return self.transition(
            task_id,
            "failed",
            expected_revision=expected_revision,
            lease_owner=lease_owner,
            fencing_token=fencing_token,
            require_lease=True,
            actor_id=lease_owner,
            action="sandbox.task_failed",
            error_code=error_code,
            error_message=error_message,
            observed_state="failed",
            cleanup_state="complete",
            output_manifest=output_manifest,
        )

    def mark_unknown(
        self,
        task_id: str,
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        reason: str = "control_plane_lost",
    ) -> SandboxTask:
        return self.transition(
            task_id,
            "unknown",
            lease_owner=lease_owner,
            fencing_token=fencing_token,
            require_lease=lease_owner is not None,
            actor_id=lease_owner or "reconciler",
            action="sandbox.task_unknown",
            observed_state=reason,
            cleanup_state="unknown",
            error_code="unknown_execution",
            error_message=reason,
        )

    def timeout(
        self,
        task_id: str,
        *,
        lease_owner: str,
        fencing_token: int,
        reason: str = "deadline_exceeded",
    ) -> SandboxTask:
        return self.transition(
            task_id,
            "timed_out",
            lease_owner=lease_owner,
            fencing_token=fencing_token,
            require_lease=True,
            actor_id=lease_owner,
            action="sandbox.task_timeout",
            desired_state="cancelled",
            observed_state="timed_out",
            cleanup_state="unknown",
            error_code="timeout",
            error_message=reason,
        )

    def reconcile_outcome(
        self,
        task_id: str,
        outcome: str,
        *,
        execution_id: str = "",
        output_manifest: Mapping[str, Any] | None = None,
        cleanup_state: str = "complete",
        actor_id: str = "reconciler",
        proof: Mapping[str, Any] | None = None,
    ) -> SandboxTask:
        """Apply an explicit reconciliation result; unknown is never guessed."""

        target = str(outcome or "").strip().lower()
        if target not in {"succeeded", "failed", "cancelled", "cleanup_pending", "unknown", "reconcile_required"}:
            raise ValueError("unsupported reconciliation outcome")
        task = self.require(task_id)
        if task.status not in UNCERTAIN_TASK_STATUSES and task.status not in {"cancel_requested", "timed_out"}:
            if task.status == target:
                return task
            raise InvalidTransition(f"task is not awaiting reconciliation: {task.status}")
        manifest = _safe_manifest(output_manifest or task.output_manifest or {})
        if (
            target == "succeeded"
            and not _manifest_committed(manifest)
            and task.result_status not in {"needs_user_input", "needs_tool"}
        ):
            raise TaskConflict("reconciliation cannot mark success without committed artifacts")
        metadata = dict(proof or {})
        metadata["execution_id_present"] = bool(execution_id or task.execution_id)
        return self.transition(
            task_id,
            target,
            actor_id=actor_id,
            action="sandbox.task_reconcile",
            execution_id=execution_id or task.execution_id,
            output_manifest=output_manifest,
            cleanup_state=cleanup_state,
            observed_state=f"reconciled:{target}",
            metadata=metadata,
        )

    def reconcile_expired_leases(self, *, now: datetime | None = None, limit: int = 100) -> list[SandboxTask]:
        current_time = now or _utc_now()
        now_text = current_time.isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT * FROM sandbox_tasks WHERE status IN ('running','collecting','cancel_requested','timed_out') "
                    "AND lease_expires_at <> '' AND lease_expires_at <= ? ORDER BY updated_at LIMIT ?",
                    (now_text, max(1, min(int(limit), 1000))),
                ).fetchall()
                changed: list[SandboxTask] = []
                for row in rows:
                    task = self._row_to_task(row)
                    target = "unknown" if task.execution_id else "reconcile_required"
                    revision = task.revision + 1
                    conn.execute(
                        "UPDATE sandbox_tasks SET status = ?, observed_state = 'lease_expired', cleanup_state = 'unknown', "
                        "lease_owner = '', lease_expires_at = '', error_code = 'lease_expired', revision = ?, updated_at = ? "
                        "WHERE task_id = ? AND revision = ?",
                        (target, revision, now_text, task.task_id, task.revision),
                    )
                    self._event(
                        conn,
                        task_id=task.task_id,
                        action="sandbox.task_unknown",
                        from_status=task.status,
                        to_status=target,
                        actor_id="reconciler",
                        lease_owner=task.lease_owner,
                        fencing_token=task.fencing_token,
                        revision=revision,
                        metadata={"reason": "lease_expired", "execution_id_present": bool(task.execution_id)},
                    )
                    updated = self._select(conn, task.task_id)
                    if updated:
                        changed.append(self._row_to_task(updated))
                conn.execute("COMMIT")
                return changed
            except Exception:
                conn.execute("ROLLBACK")
                raise

    expire_leases = reconcile_expired_leases

    def expire_deadlines(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[SandboxTask]:
        """Expire unclaimed work whose absolute deadline has passed.

        Running tasks are intentionally left for Runner cancellation and lease
        reconciliation; relabeling a live execution as terminal would hide an
        unknown side effect.
        """

        current_time = now or _utc_now()
        now_text = current_time.isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT * FROM sandbox_tasks WHERE status IN ('admitted','queued') "
                    "AND deadline_at <> '' AND deadline_at <= ? ORDER BY deadline_at LIMIT ?",
                    (now_text, max(1, min(int(limit), 1000))),
                ).fetchall()
                expired: list[SandboxTask] = []
                for row in rows:
                    task = self._row_to_task(row)
                    revision = task.revision + 1
                    cursor = conn.execute(
                        "UPDATE sandbox_tasks SET status = 'expired', desired_state = 'cancelled', "
                        "error_code = 'deadline_expired', finished_at = ?, revision = ?, updated_at = ? "
                        "WHERE task_id = ? AND revision = ? AND status IN ('admitted','queued')",
                        (now_text, revision, now_text, task.task_id, task.revision),
                    )
                    if int(cursor.rowcount or 0) != 1:
                        continue
                    self._event(
                        conn,
                        task_id=task.task_id,
                        action="sandbox.task_expire",
                        from_status=task.status,
                        to_status="expired",
                        actor_id="reconciler",
                        revision=revision,
                        metadata={"reason": "deadline_expired"},
                    )
                    updated = self._select(conn, task.task_id)
                    if updated is not None:
                        expired.append(self._row_to_task(updated))
                conn.execute("COMMIT")
                return expired
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def set_cleanup_state(
        self,
        task_id: str,
        cleanup_state: str,
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        actor_id: str = "reconciler",
    ) -> SandboxTask:
        allowed = {"pending", "running", "complete", "unknown"}
        if cleanup_state not in allowed:
            raise ValueError("invalid cleanup state")
        task = self.require(task_id)
        if lease_owner is not None:
            self._assert_lease(task, lease_owner, fencing_token)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(conn, task_id)
                if row is None:
                    raise TaskNotFound(str(task_id))
                current = self._row_to_task(row)
                if lease_owner is not None:
                    self._assert_lease(current, lease_owner, fencing_token)
                revision = current.revision + 1
                where = "task_id = ? AND revision = ?"
                params: list[Any] = [cleanup_state, revision, _iso(), str(task_id), current.revision]
                if lease_owner is not None:
                    where += " AND lease_owner = ? AND fencing_token = ?"
                    params.extend([str(lease_owner), int(fencing_token)])
                cursor = conn.execute(
                    f"UPDATE sandbox_tasks SET cleanup_state = ?, revision = ?, updated_at = ? WHERE {where}",
                    params,
                )
                if int(cursor.rowcount or 0) != 1:
                    raise LeaseLost("task changed while updating cleanup state")
                self._event(
                    conn,
                    task_id=str(task_id),
                    action="sandbox.task_cleanup",
                    from_status=current.status,
                    to_status=current.status,
                    actor_id=actor_id,
                    lease_owner=lease_owner or current.lease_owner,
                    fencing_token=int(fencing_token if fencing_token is not None else current.fencing_token),
                    revision=revision,
                    metadata={"cleanup_state": cleanup_state},
                )
                updated = self._select(conn, task_id)
                conn.execute("COMMIT")
                return self._row_to_task(updated) if updated else current
            except Exception:
                conn.execute("ROLLBACK")
                raise


def _manifest_committed(manifest: Mapping[str, Any]) -> bool:
    """Check the broker-facing shape without trusting arbitrary runner text."""

    if not manifest:
        return False
    items: Any = manifest.get("items", manifest.get("artifacts", manifest))
    if isinstance(items, Mapping):
        items = [items]
    if not isinstance(items, (list, tuple)) or not items:
        return False
    for item in items:
        if not isinstance(item, Mapping):
            return False
        if str(item.get("state") or "") not in {"committed", "consumed"}:
            return False
        artifact_id = str(item.get("artifact_id") or "")
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
            return False
        digest = str(item.get("sha256") or "").lower().removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return False
    return True


# Compatibility aliases make the control-plane boundary easy to discover from
# callers that use the shorter names in the implementation plan.
TaskStore = SandboxTaskStore
SandboxTaskStoreErrorBase = SandboxTaskStoreError


__all__ = [
    "IdempotencyConflict",
    "InvalidTransition",
    "LeaseLost",
    "SandboxTaskStore",
    "SandboxTaskStoreError",
    "TaskAlreadyTerminal",
    "TaskConflict",
    "TaskNotFound",
    "TaskStore",
]
