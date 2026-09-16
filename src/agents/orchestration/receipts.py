"""Persistent action-plan receipts (P2-001).

Key contract (plan §8.3): ``(tenant_id, session_generation, turn_id,
plan_version, action_id)``.  Domain submissions reuse the same stable key so a
crash-recovered action cannot create a second Work Order or Artifact.

Recovery semantics:

- receipts that already represent a durable outcome (``completed``,
  ``accepted``, ``blocked``, ``rejected``) are reused as-is;
- ``failed`` / ``skipped`` actions are the only ones eligible for a replay;
- partial parallel layers recover per action receipt, never as one layer;
- the store never claims cross-storage exactly-once: when a domain submission
  and the local receipt cannot share a transaction, the caller must supply a
  stable idempotency key and reconcile on lookup.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import src.settings
from src.agents.orchestration.contracts import ActionReceipt

REPLAYABLE_STATUSES = frozenset({"failed", "skipped"})
REUSABLE_STATUSES = frozenset({"completed", "accepted", "blocked", "rejected"})
_ALL_STATUSES = REPLAYABLE_STATUSES | REUSABLE_STATUSES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ActionExecutionScope:
    """Stable identity for one plan execution attempt."""

    tenant_id: str = "default"
    session_generation: int = 0
    turn_id: str = ""
    plan_version: str = "v1"

    def key_for(self, plan_id: str, action_id: str) -> tuple[str, int, str, str, str]:
        version = str(self.plan_version or "v1")
        if plan_id and not version.endswith(f":{plan_id}"):
            version = f"{version}:{plan_id}"
        return (
            str(self.tenant_id or "default"),
            int(self.session_generation),
            str(self.turn_id),
            version,
            str(action_id),
        )


@dataclass(frozen=True)
class StoredActionReceipt:
    tenant_id: str
    session_generation: int
    turn_id: str
    plan_version: str
    action_id: str
    status: str
    job_ref: str
    result_refs: list[str]
    error_code: str
    message: str
    data: dict
    revision: int
    updated_at: str
    idempotency_key: str = ""
    source_plan_id: str = ""

    def to_action_receipt(self) -> ActionReceipt:
        return ActionReceipt(
            action_id=self.action_id,
            status=self.status,
            result_refs=list(self.result_refs),
            error_code=self.error_code,
            message=self.message,
            data=dict(self.data),
        )

    @property
    def replayable(self) -> bool:
        if self.data.get("needs_reconciliation"):
            return False
        return self.status in REPLAYABLE_STATUSES


class ActionReceiptStore:
    """SQLite-backed receipt ledger in the conversation authority database."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or src.settings.AUTH_DB_PATH
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_receipts (
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    session_generation INTEGER NOT NULL DEFAULT 0,
                    turn_id TEXT NOT NULL DEFAULT '',
                    plan_version TEXT NOT NULL DEFAULT 'v1',
                    action_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    job_ref TEXT NOT NULL DEFAULT '',
                    result_refs_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, session_generation, turn_id, plan_version, action_id)
                )
                """
            )
            for column, ddl in (
                ("idempotency_key", "TEXT NOT NULL DEFAULT ''"),
                ("source_plan_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(action_receipts)").fetchall()
                }
                if column not in columns:
                    conn.execute(f"ALTER TABLE action_receipts ADD COLUMN {column} {ddl}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_receipts_job_ref"
                " ON action_receipts(job_ref)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_plans (
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    session_generation INTEGER NOT NULL DEFAULT 0,
                    turn_id TEXT NOT NULL DEFAULT '',
                    plan_version TEXT NOT NULL DEFAULT 'v1',
                    plan_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    ready_at TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, session_generation, turn_id, plan_version, plan_id)
                )
                """
            )
            conn.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _uuid_key() -> str:
        return f"ar-{uuid.uuid4().hex}"

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> StoredActionReceipt:
        try:
            refs = json.loads(row["result_refs_json"] or "[]")
        except (TypeError, ValueError):
            refs = []
        try:
            data = json.loads(row["data_json"] or "{}")
        except (TypeError, ValueError):
            data = {}
        return StoredActionReceipt(
            tenant_id=str(row["tenant_id"]),
            session_generation=int(row["session_generation"] or 0),
            turn_id=str(row["turn_id"]),
            plan_version=str(row["plan_version"]),
            action_id=str(row["action_id"]),
            status=str(row["status"]),
            job_ref=str(row["job_ref"] or ""),
            result_refs=[str(item) for item in refs if str(item)],
            error_code=str(row["error_code"] or ""),
            message=str(row["message"] or ""),
            data=dict(data) if isinstance(data, dict) else {},
            revision=int(row["revision"] or 0),
            updated_at=str(row["updated_at"] or ""),
            idempotency_key=str(row["idempotency_key"] or ""),
            source_plan_id=str(row["source_plan_id"] or ""),
        )

    def get(self, scope: ActionExecutionScope, plan_id: str, action_id: str) -> StoredActionReceipt | None:
        key = scope.key_for(plan_id, action_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM action_receipts
                WHERE tenant_id = ? AND session_generation = ? AND turn_id = ?
                  AND plan_version = ? AND action_id = ?
                """,
                key,
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_for_plan(self, scope: ActionExecutionScope, plan_id: str) -> dict[str, StoredActionReceipt]:
        key = scope.key_for(plan_id, "")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM action_receipts
                WHERE tenant_id = ? AND session_generation = ? AND turn_id = ? AND plan_version = ?
                ORDER BY created_at, action_id
                """,
                key,
            ).fetchall()
        return {str(row["action_id"]): self._row_to_record(row) for row in rows}

    def upsert(
        self,
        scope: ActionExecutionScope,
        plan_id: str,
        receipt: ActionReceipt,
        *,
        job_ref: str = "",
        idempotency_key: str = "",
        expected_revision: int | None = None,
    ) -> int:
        """Insert or conditionally update one receipt; returns the new revision.

        A lower incoming revision is ignored (returns the stored revision), so
        a late writer can never move a receipt backwards.
        """
        key = scope.key_for(plan_id, receipt.action_id)
        now = utc_now()
        refs_json = json.dumps(list(receipt.result_refs or []), ensure_ascii=False)
        data_json = json.dumps(dict(receipt.data or {}), ensure_ascii=False, default=str)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT revision, job_ref FROM action_receipts
                    WHERE tenant_id = ? AND session_generation = ? AND turn_id = ?
                      AND plan_version = ? AND action_id = ?
                    """,
                    key,
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO action_receipts (
                            tenant_id, session_generation, turn_id, plan_version, action_id,
                            status, job_ref, result_refs_json, error_code, message, data_json,
                            revision, created_at, updated_at, idempotency_key, source_plan_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            *key,
                            str(receipt.status),
                            str(job_ref or ""),
                            refs_json,
                            str(receipt.error_code or ""),
                            str(receipt.message or ""),
                            data_json,
                            now,
                            now,
                            str(idempotency_key or ""),
                            str(plan_id or ""),
                        ),
                    )
                    conn.execute("COMMIT")
                    return 1
                current_revision = int(row["revision"] or 0)
                if expected_revision is not None and current_revision != int(expected_revision):
                    conn.execute("COMMIT")
                    return current_revision
                conn.execute(
                    """
                    UPDATE action_receipts
                    SET status = ?, job_ref = ?, result_refs_json = ?, error_code = ?,
                        message = ?, data_json = ?, revision = revision + 1, updated_at = ?,
                        idempotency_key = CASE WHEN ? <> '' THEN ? ELSE idempotency_key END,
                        source_plan_id = CASE WHEN ? <> '' THEN ? ELSE source_plan_id END
                    WHERE tenant_id = ? AND session_generation = ? AND turn_id = ?
                      AND plan_version = ? AND action_id = ?
                    """,
                    (
                        str(receipt.status),
                        str(job_ref or row["job_ref"] or ""),
                        refs_json,
                        str(receipt.error_code or ""),
                        str(receipt.message or ""),
                        data_json,
                        now,
                        str(idempotency_key or ""),
                        str(idempotency_key or ""),
                        str(plan_id or ""),
                        str(plan_id or ""),
                        *key,
                    ),
                )
                conn.execute("COMMIT")
                return current_revision + 1
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_reconciled(self, scope: ActionExecutionScope, plan_id: str, action_id: str, reason: str) -> bool:
        """Record that a write could not be reconciled and needs coordination."""
        receipt = ActionReceipt(
            action_id=action_id,
            status="blocked",
            error_code="pending_reconciliation",
            message=str(reason)[:500],
        )
        self.upsert(scope, plan_id, receipt)
        return True

    # -- plan persistence and domain wake-up (P2-001) ----------------------

    def save_plan(self, scope: ActionExecutionScope, plan: Any, *, status: str = "running") -> int:
        """Persist a plan under the same stable key family as its receipts."""
        key = scope.key_for(getattr(plan, "plan_id", ""), "")
        now = utc_now()
        plan_id = str(getattr(plan, "plan_id", ""))
        plan_json = (
            plan.model_dump_json()
            if hasattr(plan, "model_dump_json")
            else json.dumps(plan, ensure_ascii=False, default=str)
        )
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT revision FROM action_plans
                    WHERE tenant_id = ? AND session_generation = ? AND turn_id = ?
                      AND plan_version = ? AND plan_id = ?
                    """,
                    (*key[:4], plan_id),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO action_plans (
                            tenant_id, session_generation, turn_id, plan_version, plan_id,
                            status, plan_json, ready_at, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, '', 1, ?, ?)
                        """,
                        (*key[:4], plan_id, str(status), plan_json, now, now),
                    )
                    conn.execute("COMMIT")
                    return 1
                conn.execute(
                    """
                    UPDATE action_plans
                    SET status = ?, plan_json = ?, revision = revision + 1, updated_at = ?
                    WHERE tenant_id = ? AND session_generation = ? AND turn_id = ?
                      AND plan_version = ? AND plan_id = ?
                    """,
                    (str(status), plan_json, now, *key[:4], plan_id),
                )
                conn.execute("COMMIT")
                return int(row["revision"] or 0) + 1
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_plan(self, scope: ActionExecutionScope, plan_id: str) -> dict | None:
        key = scope.key_for(plan_id, "")
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM action_plans
                WHERE tenant_id = ? AND session_generation = ? AND turn_id = ?
                  AND plan_version = ? AND plan_id = ?
                """,
                (*key[:4], str(plan_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_plan_status(
        self,
        scope: ActionExecutionScope,
        plan_id: str,
        status: str,
        *,
        ready: bool = False,
    ) -> bool:
        key = scope.key_for(plan_id, "")
        now = utc_now()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE action_plans
                SET status = ?,
                    ready_at = CASE WHEN ? THEN ? ELSE ready_at END,
                    revision = revision + 1, updated_at = ?
                WHERE tenant_id = ? AND session_generation = ? AND turn_id = ?
                  AND plan_version = ? AND plan_id = ?
                """,
                (str(status), 1 if ready else 0, now, now, *key[:4], str(plan_id)),
            )
            return int(cursor.rowcount or 0) > 0

    def find_receipts_by_reference(self, reference: str) -> list[StoredActionReceipt]:
        """All receipts whose job_ref or result refs contain ``reference``."""
        needle = str(reference or "").strip()
        if not needle:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM action_receipts
                WHERE job_ref = ? OR result_refs_json LIKE ?
                ORDER BY updated_at, action_id
                """,
                (needle, f'%"{needle}"%'),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def settle_domain_job(
        self,
        reference: str,
        *,
        status: str,
        result_refs: list[str] | None = None,
        error_code: str = "",
        message: str = "",
    ) -> int:
        """Settle receipts waiting on a domain job and mark their plans ready.

        Idempotent: only ``accepted`` receipts move, so a duplicated completion
        event cannot settle the same action twice.  Execution resumes when the
        owning plan is re-entered; this method never dispatches work itself.
        """
        settled = 0
        for stored in self.find_receipts_by_reference(reference):
            if stored.status != "accepted":
                continue
            plan_id = stored.source_plan_id or stored.plan_version.rsplit(":", 1)[-1]
            if not plan_id:
                continue
            scope = ActionExecutionScope(
                tenant_id=stored.tenant_id,
                session_generation=stored.session_generation,
                turn_id=stored.turn_id,
                plan_version=stored.plan_version,
            )
            receipt = ActionReceipt(
                action_id=stored.action_id,
                status=str(status),
                result_refs=list(result_refs if result_refs is not None else stored.result_refs),
                error_code=str(error_code),
                message=str(message),
            )
            self.upsert(scope, plan_id, receipt)
            self.mark_plan_status(scope, plan_id, "running", ready=True)
            settled += 1
        return settled

    def list_ready_plans(self, *, limit: int = 100) -> list[dict]:
        """Plans whose domain dependency settled and that are ready to resume."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM action_plans
                WHERE ready_at <> '' AND status IN ('running', 'awaiting_domain')
                ORDER BY ready_at, updated_at
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]


def settle_action_receipts_for_job(
    references: list[str],
    *,
    status: str,
    result_refs: list[str] | None = None,
    error_code: str = "",
    message: str = "",
) -> int:
    """Best-effort domain completion hook (never raises into the worker)."""
    try:
        store = ActionReceiptStore()
        settled = 0
        for reference in references:
            if not str(reference or "").strip():
                continue
            settled += store.settle_domain_job(
                str(reference),
                status=status,
                result_refs=result_refs,
                error_code=error_code,
                message=message,
            )
        return settled
    except Exception:
        return 0


__all__ = [
    "ActionExecutionScope",
    "ActionReceiptStore",
    "REPLAYABLE_STATUSES",
    "REUSABLE_STATUSES",
    "StoredActionReceipt",
    "settle_action_receipts_for_job",
]
