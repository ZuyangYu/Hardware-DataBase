"""SQLite decision store for conflict cases and optimistic human choices."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .models import ConflictCase, ConflictDecision


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConflictDecisionStore:
    """Persist conflict cases independently from model or browser state."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS authoring_conflict_cases (
                    conflict_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    work_order_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_authoring_conflicts_work_order
                    ON authoring_conflict_cases(tenant_id, work_order_id, status);
                CREATE TABLE IF NOT EXISTS authoring_conflict_decisions (
                    decision_id TEXT PRIMARY KEY,
                    conflict_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(conflict_id, idempotency_key),
                    FOREIGN KEY(conflict_id) REFERENCES authoring_conflict_cases(conflict_id)
                );
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save_case(self, case: ConflictCase, *, tenant_id: str, work_order_id: str) -> ConflictCase:
        now = _now()
        payload = case.model_dump(mode="json")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT fingerprint, payload_json FROM authoring_conflict_cases WHERE conflict_id = ?",
                    (case.conflict_id,),
                ).fetchone()
                if row is not None:
                    if str(row["fingerprint"]) != case.fingerprint:
                        raise ValueError("conflict_case_fingerprint_changed")
                    conn.execute("COMMIT")
                    return ConflictCase.model_validate(json.loads(row["payload_json"]))
                conn.execute(
                    """INSERT INTO authoring_conflict_cases
                       (conflict_id, tenant_id, work_order_id, fingerprint, status, payload_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        case.conflict_id,
                        str(tenant_id),
                        str(work_order_id),
                        case.fingerprint,
                        case.status,
                        self._json(payload),
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT")
                return case
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_case(self, conflict_id: str, *, tenant_id: str | None = None) -> ConflictCase | None:
        with closing(self._connect()) as conn:
            if tenant_id is None:
                row = conn.execute(
                    "SELECT payload_json FROM authoring_conflict_cases WHERE conflict_id = ?",
                    (str(conflict_id),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT payload_json FROM authoring_conflict_cases WHERE conflict_id = ? AND tenant_id = ?",
                    (str(conflict_id), str(tenant_id)),
                ).fetchone()
        return ConflictCase.model_validate(json.loads(row["payload_json"])) if row else None

    def submit_decision(
        self,
        conflict_id: str,
        *,
        tenant_id: str,
        actor_id: str,
        expected_fingerprint: str,
        expected_snapshot_hash: str,
        expected_evidence_set_hash: str,
        decision: ConflictDecision,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("conflict_decision_idempotency_key_required")
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload_json FROM authoring_conflict_decisions WHERE conflict_id = ? AND idempotency_key = ?",
                    (str(conflict_id), key),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return json.loads(existing["payload_json"])
                row = conn.execute(
                    "SELECT tenant_id, payload_json, status FROM authoring_conflict_cases WHERE conflict_id = ?",
                    (str(conflict_id),),
                ).fetchone()
                if row is None:
                    raise KeyError("conflict_case_not_found")
                if str(row["tenant_id"]) != str(tenant_id):
                    raise PermissionError("conflict_case_tenant_mismatch")
                case = ConflictCase.model_validate(json.loads(row["payload_json"]))
                if case.fingerprint != expected_fingerprint:
                    raise ValueError("conflict_case_changed")
                if case.snapshot_hash != expected_snapshot_hash:
                    raise ValueError("source_snapshot_changed")
                if case.evidence_set_hash != expected_evidence_set_hash:
                    raise ValueError("evidence_set_changed")
                if case.policy_version != decision.policy_version:
                    raise ValueError("conflict_policy_changed")
                if decision.conflict_id != case.conflict_id:
                    raise ValueError("conflict_decision_case_mismatch")
                claim_ids = {claim.claim_id for claim in case.claims}
                if decision.selected_claim_id and decision.selected_claim_id not in claim_ids:
                    raise ValueError("conflict_decision_claim_not_found")
                if decision.action == "choose_claim" and not decision.selected_claim_id:
                    raise ValueError("conflict_decision_claim_required")
                payload = {
                    "decision_id": f"conflict-decision-{uuid.uuid4().hex}",
                    "conflict_id": case.conflict_id,
                    "tenant_id": str(tenant_id),
                    "actor_id": str(actor_id)[:200],
                    "idempotency_key": key,
                    "decision": decision.model_dump(mode="json"),
                    "created_at": now,
                }
                conn.execute(
                    """INSERT INTO authoring_conflict_decisions
                       (decision_id, conflict_id, tenant_id, actor_id, idempotency_key, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["decision_id"],
                        case.conflict_id,
                        str(tenant_id),
                        str(actor_id)[:200],
                        key,
                        self._json(payload),
                        now,
                    ),
                )
                updated = case.model_copy(update={"status": "resolved"})
                conn.execute(
                    "UPDATE authoring_conflict_cases SET status = ?, payload_json = ?, updated_at = ? WHERE conflict_id = ?",
                    (updated.status, self._json(updated.model_dump(mode="json")), now, case.conflict_id),
                )
                conn.execute("COMMIT")
                return payload
            except Exception:
                conn.execute("ROLLBACK")
                raise


__all__ = ["ConflictDecisionStore"]
