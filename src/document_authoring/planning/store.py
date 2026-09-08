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
from .submissions import DocumentPlanSubmission, DocumentPlanSubmissionStore


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
        # Keep the outbox repository on the same SQLite file.  The planning
        # store passes its open connection during confirmation so accepted
        # spec/plan/session/task rows and the outbox insert commit together.
        self.submissions = DocumentPlanSubmissionStore(db_path)

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
                         AND status IN ('proposed', 'blocked') AND plan_hash = ?""",
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

    def confirm_submission(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        expected_output_spec_hash: str,
        expected_plan_hash: str,
        client_request_id: str,
        source_snapshot_id: str | None = None,
    ) -> DocumentPlanSubmission:
        """Accept one executable plan and enqueue its worker submission atomically.

        The session, task, planning rows and outbox all live in this database.
        This method deliberately performs the complete state transition on a
        single ``BEGIN IMMEDIATE`` connection; the separate auth/job database
        is touched only by the later worker.
        """
        tenant, user = self._require_owner(tenant_id, user_id)
        session_key = str(session_id or "").strip()
        expected_spec = str(expected_output_spec_hash or "").strip()
        expected_plan = str(expected_plan_hash or "").strip()
        request_key = str(client_request_id or "").strip()[:128]
        if not session_key or not expected_spec or not expected_plan or not request_key:
            raise ValueError(
                "session, expected hashes and client_request_id are required"
            )

        # Imports are local to avoid making the planning models depend on the
        # session/task projection modules at import time.
        from src.document_authoring.generation_sessions import GenerationSession
        from src.document_authoring.tasks import DocumentTask

        from .submissions import _row_to_submission  # type: ignore[attr-defined]

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session_row = connection.execute(
                    """SELECT * FROM document_generation_sessions
                       WHERE session_id = ? AND tenant_id = ? AND user_id = ?""",
                    (session_key, tenant, user),
                ).fetchone()
                if session_row is None:
                    raise KeyError("generation session not found")
                session_payload = json.loads(session_row["payload_json"])
                session = GenerationSession.model_validate(session_payload)
                if session.contract_version != "output_spec_v1":
                    raise ValueError("plan confirmation requires output_spec_v1")
                if session.status not in {"awaiting_plan_confirmation", "planned"}:
                    raise ValueError("generation session is not awaiting plan confirmation")

                plan_id = str(session.document_plan_id or "").strip()
                plan_version = int(session.document_plan_version or 0)
                spec_id = str(session.output_spec_id or "").strip()
                spec_version = int(session.output_spec_version or 0)
                if not plan_id or plan_version < 1 or not spec_id or spec_version < 1:
                    raise ValueError("generation session is not bound to a complete document plan")

                plan_row = connection.execute(
                    """SELECT * FROM document_plans
                       WHERE document_plan_id = ? AND version = ?
                         AND tenant_id = ? AND user_id = ?""",
                    (plan_id, plan_version, tenant, user),
                ).fetchone()
                spec_row = connection.execute(
                    """SELECT * FROM document_output_specs
                       WHERE output_spec_id = ? AND version = ?
                         AND tenant_id = ? AND user_id = ?""",
                    (spec_id, spec_version, tenant, user),
                ).fetchone()
                if plan_row is None or spec_row is None:
                    raise KeyError("document plan or output spec not found")
                plan = self._validate_plan_row(plan_row)
                spec = self._validate_spec_row(spec_row)
                if plan.output_spec_id != spec.output_spec_id or plan.output_spec_version != spec.version:
                    raise ValueError("document plan is not bound to the session OutputSpec")
                if plan.output_spec_hash != spec.content_hash:
                    raise ValueError("document plan OutputSpec hash does not match persisted spec")
                if spec.content_hash != expected_spec:
                    raise ValueError("expected output spec hash does not match current spec")
                if plan.plan_hash != expected_plan:
                    raise ValueError("expected plan hash does not match current plan")
                if source_snapshot_id is not None and str(source_snapshot_id).strip() != plan.source_snapshot_id:
                    raise ValueError("source snapshot does not match current plan")
                if plan.status == "accepted" or spec.status == "accepted":
                    existing = connection.execute(
                        """SELECT * FROM document_plan_submissions
                           WHERE tenant_id = ? AND user_id = ?
                             AND document_plan_id = ? AND document_plan_version = ?
                             AND document_plan_hash = ?""",
                        (tenant, user, plan.document_plan_id, plan.version, plan.plan_hash),
                    ).fetchone()
                    if existing is None:
                        raise ValueError("accepted plan has no durable submission")
                    if existing["client_request_id"] != request_key:
                        # A second request key is an idempotent replay of the
                        # already accepted plan.  Return the original row.
                        pass
                    connection.execute("COMMIT")
                    return _row_to_submission(existing)
                if plan.status not in {"proposed"} or spec.status not in {"proposed"}:
                    raise ValueError("document plan or OutputSpec is not confirmable")
                if not plan.is_executable:
                    raise ValueError("document plan is not executable")
                if getattr(plan.layout_contract, "kind", "") != "template":
                    raise ValueError("template-free document plans are not confirmable in Phase 1")

                # Re-read the frozen source/template identities from this
                # same DB while the write lock is held.  The worker performs a
                # second live permission check; this protects the acceptance
                # transaction from stale/deleted immutable inputs.
                snapshot_row = connection.execute(
                    """SELECT tenant_id, knowledge_base_name, content_hash
                       FROM knowledge_base_source_snapshots
                       WHERE source_set_snapshot_id = ?""",
                    (plan.source_snapshot_id,),
                ).fetchone()
                if (
                    snapshot_row is None
                    or snapshot_row["tenant_id"] != tenant
                    or snapshot_row["knowledge_base_name"] != session.knowledge_base_name
                    or snapshot_row["content_hash"] != plan.source_snapshot_hash
                ):
                    raise ValueError("frozen source snapshot is missing or changed")
                layout = plan.layout_contract
                template_id = str(getattr(layout, "template_version_id", "") or "").strip()
                template_schema_id = str(getattr(layout, "template_schema_id", "") or "").strip()
                template_schema_version = str(getattr(layout, "template_schema_version", "") or "").strip()
                template_row = connection.execute(
                    """SELECT content_hash, payload_json
                       FROM template_versions WHERE template_version_id = ?""",
                    (template_id,),
                ).fetchone()
                if template_row is None:
                    raise ValueError("frozen template is missing")
                template_payload = json.loads(template_row["payload_json"])
                if (
                    template_payload.get("status") != "approved"
                    or template_payload.get("content_hash") != template_row["content_hash"]
                    or template_payload.get("template_schema_id") != template_schema_id
                    or str(template_payload.get("template_schema_version") or "") != template_schema_version
                ):
                    raise ValueError("frozen template is not approved or schema-bound")
                template_hash = str((plan.output_spec_summary or {}).get("template_content_hash") or "")
                if not template_hash or template_hash != str(template_row["content_hash"]):
                    raise ValueError("frozen template hash does not match the proposed plan")

                accepted_at = datetime.now(timezone.utc)
                accepted_spec = spec.model_copy(update={
                    "status": "accepted",
                    "confirmed_by": user,
                    "confirmed_at": accepted_at,
                })
                accepted_plan = plan.model_copy(update={"status": "accepted"})
                updated_spec = connection.execute(
                    """UPDATE document_output_specs
                       SET status = ?, payload_json = ?
                       WHERE output_spec_id = ? AND version = ?
                         AND tenant_id = ? AND user_id = ? AND status = 'proposed'
                         AND content_hash = ?""",
                    (
                        accepted_spec.status,
                        _safe_json(accepted_spec.model_dump(mode="json")),
                        spec.output_spec_id, spec.version, tenant, user,
                        expected_spec,
                    ),
                ).rowcount
                if updated_spec != 1:
                    raise ValueError("OutputSpec changed while confirming")
                updated_plan = connection.execute(
                    """UPDATE document_plans
                       SET status = ?, payload_json = ?
                       WHERE document_plan_id = ? AND version = ?
                         AND tenant_id = ? AND user_id = ? AND status = 'proposed'
                         AND plan_hash = ?""",
                    (
                        accepted_plan.status,
                        _safe_json(accepted_plan.model_dump(mode="json")),
                        plan.document_plan_id, plan.version, tenant, user,
                        expected_plan,
                    ),
                ).rowcount
                if updated_plan != 1:
                    raise ValueError("DocumentPlan changed while confirming")

                # Update the v2 session projection in the same transaction.
                session_updated = session.model_copy(update={
                    "status": "planned",
                    "output_spec_id": spec.output_spec_id,
                    "output_spec_version": spec.version,
                    "document_plan_id": plan.document_plan_id,
                    "document_plan_version": plan.version,
                    "updated_at": accepted_at,
                })
                session_cursor = connection.execute(
                    """UPDATE document_generation_sessions
                       SET output_spec_id = ?, output_spec_version = ?,
                           document_plan_id = ?, document_plan_version = ?,
                           status = ?, updated_at = ?, payload_json = ?
                       WHERE session_id = ? AND tenant_id = ? AND user_id = ?""",
                    (
                        session_updated.output_spec_id, session_updated.output_spec_version,
                        session_updated.document_plan_id, session_updated.document_plan_version,
                        session_updated.status, session_updated.updated_at.isoformat(),
                        json.dumps(session_updated.model_dump(mode="json", exclude={"messages"}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        session_key, tenant, user,
                    ),
                ).rowcount
                if session_cursor != 1:
                    raise ValueError("generation session changed while confirming")

                task_id = str(session.document_task_id or session_key).strip()
                task_row = connection.execute(
                    "SELECT * FROM document_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if task_row is not None:
                    task_payload = json.loads(task_row["payload_json"])
                    task = DocumentTask.model_validate(task_payload)
                    if task.tenant_id != tenant or task.user_id != user:
                        raise PermissionError("document task belongs to another owner")
                    if task.generation_session_id not in {None, session_key}:
                        raise ValueError("document task is bound to another generation session")
                    if task.output_spec_id not in {None, spec.output_spec_id} or task.document_plan_id not in {None, plan.document_plan_id}:
                        raise ValueError("document task is already bound to another plan")
                    updated_task = task.model_copy(update={
                        "output_spec_id": spec.output_spec_id,
                        "output_spec_version": spec.version,
                        "document_plan_id": plan.document_plan_id,
                        "document_plan_version": plan.version,
                        "status": "planned",
                        "updated_at": accepted_at,
                    })
                    connection.execute(
                        """UPDATE document_tasks
                           SET output_spec_id = ?, output_spec_version = ?,
                               document_plan_id = ?, document_plan_version = ?,
                               status = ?, updated_at = ?, payload_json = ?
                           WHERE task_id = ?""",
                        (
                            updated_task.output_spec_id, updated_task.output_spec_version,
                            updated_task.document_plan_id, updated_task.document_plan_version,
                            updated_task.status, updated_task.updated_at.isoformat(),
                            json.dumps(updated_task.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                            task_id,
                        ),
                    )
                    task_event_key = f"plan-confirmed:{plan.document_plan_id}:{plan.version}:{plan.plan_hash}"
                    connection.execute(
                        """INSERT OR IGNORE INTO document_task_events
                           (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            f"document-task-event-{uuid.uuid4().hex}", task_id,
                            "plan_confirmed", task_event_key, accepted_at.isoformat(),
                            _safe_json({
                                "document_plan_id": plan.document_plan_id,
                                "document_plan_version": plan.version,
                                "plan_hash": plan.plan_hash,
                                "output_spec_id": spec.output_spec_id,
                                "output_spec_version": spec.version,
                                "output_spec_hash": spec.content_hash,
                            }),
                        ),
                    )

                planning_task_id = task_id
                event_key = f"plan-confirmation:{plan.document_plan_id}:{plan.version}:{plan.plan_hash}"
                event_payload = {
                    "session_id": session_key,
                    "task_id": task_id,
                    "document_plan_id": plan.document_plan_id,
                    "document_plan_version": plan.version,
                    "plan_hash": plan.plan_hash,
                    "output_spec_id": spec.output_spec_id,
                    "output_spec_version": spec.version,
                    "output_spec_hash": spec.content_hash,
                    "source_snapshot_id": plan.source_snapshot_id,
                    "source_snapshot_hash": plan.source_snapshot_hash,
                    "actor_id": user,
                }
                _event_payload_is_safe(event_payload)
                connection.execute(
                    """INSERT OR IGNORE INTO document_planning_events
                       (event_id, task_id, event_type, idempotency_key, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"planning-event-{uuid.uuid4().hex}", planning_task_id,
                        "document_plan_confirmed", event_key, accepted_at.isoformat(),
                        _safe_json(event_payload),
                    ),
                )

                submission = DocumentPlanSubmission(
                    submission_id=f"document-plan-submission-{uuid.uuid4().hex}",
                    tenant_id=tenant,
                    user_id=user,
                    task_id=task_id,
                    session_id=session_key,
                    knowledge_base_name=session.knowledge_base_name,
                    output_spec_id=spec.output_spec_id,
                    output_spec_version=spec.version,
                    output_spec_hash=spec.content_hash,
                    document_plan_id=plan.document_plan_id,
                    document_plan_version=plan.version,
                    document_plan_hash=plan.plan_hash,
                    source_snapshot_id=plan.source_snapshot_id,
                    source_snapshot_hash=plan.source_snapshot_hash,
                    client_request_id=request_key,
                    created_at=accepted_at.isoformat(),
                    updated_at=accepted_at.isoformat(),
                )
                persisted = self.submissions.insert_or_get(submission, connection=connection)
                connection.execute("COMMIT")
                return persisted
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
