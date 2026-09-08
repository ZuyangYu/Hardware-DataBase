"""SQLite persistence for immutable OutputSpec and DocumentPlan versions."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Mapping

from src.document_authoring.harness.idempotency import canonical_json

from .models import DocumentPlan, OutputSpec


_FORBIDDEN_EVENT_KEYS = frozenset({
    "api_key",
    "credential",
    "credentials",
    "content",
    "evidence",
    "evidence_content",
    "file",
    "password",
    "path",
    "prompt",
    "raw_content",
    "secret",
    "storage_ref",
    "template_bytes",
    "token",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json(value: Any) -> str:
    return canonical_json(value)


def _event_payload_is_safe(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text.casefold() in _FORBIDDEN_EVENT_KEYS:
                raise ValueError(f"{path} contains forbidden key: {key_text}")
            _event_payload_is_safe(child, path=f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _event_payload_is_safe(child, path=f"{path}[{index}]")


def _decode_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("planning payload is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("planning payload must be a JSON object")
    return value


class DocumentPlanningStore:
    """Repository with immutable version rows and compare-and-swap staleness."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_output_specs (
                    output_spec_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(output_spec_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_document_output_specs_task
                    ON document_output_specs(tenant_id, user_id, task_id, version DESC);
                CREATE TABLE IF NOT EXISTS document_plans (
                    document_plan_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    task_id TEXT,
                    output_spec_id TEXT NOT NULL,
                    output_spec_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    source_set_snapshot_id TEXT NOT NULL,
                    source_set_snapshot_hash TEXT NOT NULL,
                    stale_reason_code TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(document_plan_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_document_plans_task
                    ON document_plans(tenant_id, user_id, task_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_document_plans_output_spec
                    ON document_plans(output_spec_id, output_spec_version, version DESC);
                CREATE TABLE IF NOT EXISTS document_planning_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(task_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_document_planning_events_task
                    ON document_planning_events(task_id, created_at, event_id);
                """
            )
            self._ensure_column(connection, "document_plans", "stale_reason_code", "TEXT")

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _require_owner(tenant_id: str, user_id: str) -> tuple[str, str]:
        tenant = str(tenant_id or "").strip()
        user = str(user_id or "").strip()
        if not tenant or not user:
            raise ValueError("tenant_id and user_id are required")
        return tenant, user

    @staticmethod
    def _validate_spec_row(row: sqlite3.Row) -> OutputSpec:
        payload = _decode_payload(row)
        spec = OutputSpec.model_validate(payload)
        if (
            spec.output_spec_id != row["output_spec_id"]
            or spec.version != int(row["version"])
            or spec.status != row["status"]
            or spec.content_hash != row["content_hash"]
        ):
            raise ValueError("OutputSpec indexed identity/hash does not match payload")
        return spec

    @staticmethod
    def _validate_plan_row(row: sqlite3.Row) -> DocumentPlan:
        payload = _decode_payload(row)
        plan = DocumentPlan.model_validate(payload)
        if (
            plan.document_plan_id != row["document_plan_id"]
            or plan.version != int(row["version"])
            or plan.output_spec_id != row["output_spec_id"]
            or plan.output_spec_version != int(row["output_spec_version"])
            or plan.status != row["status"]
            or plan.plan_hash != row["plan_hash"]
            or plan.source_snapshot_id != row["source_set_snapshot_id"]
            or plan.source_snapshot_hash != row["source_set_snapshot_hash"]
        ):
            raise ValueError("DocumentPlan indexed identity/hash does not match payload")
        return plan

    def create_output_spec(
        self,
        spec: OutputSpec,
        *,
        tenant_id: str = "default",
        user_id: str = "system",
        task_id: str | None = None,
        created_at: str | None = None,
    ) -> OutputSpec:
        tenant, user = self._require_owner(tenant_id, user_id)
        if not isinstance(spec, OutputSpec):
            spec = OutputSpec.model_validate(spec)
        task = str(task_id).strip() if task_id is not None else None
        if task == "":
            raise ValueError("task_id must be non-empty when provided")
        payload_json = _safe_json(spec.model_dump(mode="json"))
        timestamp = str(created_at or _now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """SELECT * FROM document_output_specs
                       WHERE output_spec_id = ? AND version = ?""",
                    (spec.output_spec_id, spec.version),
                ).fetchone()
                if existing is not None:
                    if existing["tenant_id"] != tenant or existing["user_id"] != user:
                        raise PermissionError("OutputSpec belongs to a different owner")
                    persisted = self._validate_spec_row(existing)
                    if existing["payload_json"] != payload_json or persisted != spec:
                        raise ValueError("immutable OutputSpec version already contains different content")
                    connection.execute("COMMIT")
                    return persisted
                connection.execute(
                    """INSERT INTO document_output_specs
                       (output_spec_id, version, tenant_id, user_id, task_id, status,
                        content_hash, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        spec.output_spec_id,
                        spec.version,
                        tenant,
                        user,
                        task,
                        spec.status,
                        spec.content_hash,
                        timestamp,
                        payload_json,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return spec

    def get_output_spec(
        self,
        output_spec_id: str,
        version: int,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        task_id: str | None = None,
    ) -> OutputSpec | None:
        sql = "SELECT * FROM document_output_specs WHERE output_spec_id = ? AND version = ?"
        params: list[Any] = [output_spec_id, version]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        with closing(self._connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        return self._validate_spec_row(row) if row is not None else None

    def get_latest_output_spec(
        self,
        task_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> OutputSpec | None:
        sql = "SELECT * FROM document_output_specs WHERE task_id = ?"
        params: list[Any] = [task_id]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY version DESC, created_at DESC LIMIT 1"
        with closing(self._connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        return self._validate_spec_row(row) if row is not None else None

    def create_plan(
        self,
        plan: DocumentPlan,
        *,
        tenant_id: str = "default",
        user_id: str = "system",
        task_id: str | None = None,
        created_at: str | None = None,
    ) -> DocumentPlan:
        tenant, user = self._require_owner(tenant_id, user_id)
        if not isinstance(plan, DocumentPlan):
            plan = DocumentPlan.model_validate(plan)
        task = str(task_id).strip() if task_id is not None else None
        if task == "":
            raise ValueError("task_id must be non-empty when provided")
        payload_json = _safe_json(plan.model_dump(mode="json"))
        timestamp = str(created_at or _now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """SELECT * FROM document_plans
                       WHERE document_plan_id = ? AND version = ?""",
                    (plan.document_plan_id, plan.version),
                ).fetchone()
                if existing is not None:
                    if existing["tenant_id"] != tenant or existing["user_id"] != user:
                        raise PermissionError("DocumentPlan belongs to a different owner")
                    persisted = self._validate_plan_row(existing)
                    if existing["payload_json"] != payload_json or persisted != plan:
                        raise ValueError("immutable DocumentPlan version already contains different content")
                    connection.execute("COMMIT")
                    return persisted
                connection.execute(
                    """INSERT INTO document_plans
                       (document_plan_id, version, tenant_id, user_id, task_id,
                        output_spec_id, output_spec_version, status, plan_hash,
                        source_set_snapshot_id, source_set_snapshot_hash,
                        stale_reason_code, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        plan.document_plan_id,
                        plan.version,
                        tenant,
                        user,
                        task,
                        plan.output_spec_id,
                        plan.output_spec_version,
                        plan.status,
                        plan.plan_hash,
                        plan.source_snapshot_id,
                        plan.source_snapshot_hash,
                        None,
                        timestamp,
                        payload_json,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return plan

    def get_plan(
        self,
        document_plan_id: str,
        version: int,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        task_id: str | None = None,
    ) -> DocumentPlan | None:
        sql = "SELECT * FROM document_plans WHERE document_plan_id = ? AND version = ?"
        params: list[Any] = [document_plan_id, version]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        with closing(self._connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        return self._validate_plan_row(row) if row is not None else None

    def get_latest_plan(
        self,
        task_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> DocumentPlan | None:
        sql = "SELECT * FROM document_plans WHERE task_id = ?"
        params: list[Any] = [task_id]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY version DESC, created_at DESC LIMIT 1"
        with closing(self._connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        return self._validate_plan_row(row) if row is not None else None

    def mark_plan_stale(
        self,
        document_plan_id: str,
        version: int,
        *,
        expected_plan_hash: str,
        reason_code: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> DocumentPlan:
        reason = str(reason_code or "").strip()
        if not reason:
            raise ValueError("reason_code is required")
        if tenant_id is not None and user_id is not None:
            owner = self._require_owner(tenant_id, user_id)
        else:
            owner = None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM document_plans WHERE document_plan_id = ? AND version = ?",
                    (document_plan_id, version),
                ).fetchone()
                if row is None or (owner and (row["tenant_id"], row["user_id"]) != owner):
                    raise ValueError("document plan not found for owner")
                plan = self._validate_plan_row(row)
                if plan.plan_hash != expected_plan_hash:
                    raise ValueError("expected plan hash does not match current plan")
                if plan.status == "accepted":
                    raise ValueError("accepted document plan cannot be marked stale")
                if plan.status == "stale":
                    if row["stale_reason_code"] not in (None, reason):
                        raise ValueError("document plan is already stale for a different reason")
                    if row["stale_reason_code"] is None:
                        connection.execute(
                            "UPDATE document_plans SET stale_reason_code = ? WHERE document_plan_id = ? AND version = ?",
                            (reason, document_plan_id, version),
                        )
                    connection.execute("COMMIT")
                    return plan
                stale_payload = plan.model_dump(mode="json")
                stale_payload["status"] = "stale"
                stale_plan = DocumentPlan.model_validate(stale_payload)
                updated = connection.execute(
                    """UPDATE document_plans
                       SET status = ?, stale_reason_code = ?, payload_json = ?
                       WHERE document_plan_id = ? AND version = ?
                         AND status = 'proposed' AND plan_hash = ?""",
                    (
                        stale_plan.status,
                        reason,
                        _safe_json(stale_plan.model_dump(mode="json")),
                        document_plan_id,
                        version,
                        expected_plan_hash,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("document plan became stale or changed concurrently")
                connection.execute("COMMIT")
                return stale_plan
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        task = str(task_id or "").strip()
        kind = str(event_type or "").strip()
        key = str(idempotency_key or "").strip()
        if not task or not kind or not key:
            raise ValueError("task_id, event_type and idempotency_key are required")
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be a mapping")
        payload_value = dict(payload)
        _event_payload_is_safe(payload_value)
        payload_json = _safe_json(payload_value)
        timestamp = str(created_at or _now())
        identifier = str(event_id or f"planning-event-{uuid.uuid4().hex}")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """SELECT * FROM document_planning_events
                       WHERE task_id = ? AND idempotency_key = ?""",
                    (task, key),
                ).fetchone()
                if existing is not None:
                    if existing["event_type"] != kind or existing["payload_json"] != payload_json:
                        raise ValueError("event idempotency key conflicts with existing payload")
                    connection.execute("COMMIT")
                    return dict(existing)
                connection.execute(
                    """INSERT INTO document_planning_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (identifier, task, kind, key, timestamp, payload_json),
                )
                row = connection.execute(
                    "SELECT * FROM document_planning_events WHERE event_id = ?", (identifier,)
                ).fetchone()
                connection.execute("COMMIT")
                return dict(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM document_planning_events
                   WHERE task_id = ? ORDER BY created_at, event_id""",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]
