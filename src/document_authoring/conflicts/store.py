"""Durable conflict cases and optimistic human decisions.

The store can either open the historical authoring SQLite database directly or
share a ``DocumentAuthoringStore`` connection. Sharing the latter is required
for the PostgreSQL authoring backend: conflict rows must live beside work
orders, runs, and audit events instead of silently falling back to an in-memory
SQLite database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .models import ConflictCase, ConflictDecision


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ConflictDecisionStore:
    """Persist conflict cases with idempotency and optimistic concurrency."""

    def __init__(self, db_path: str | None = None, *, backend_store: Any | None = None):
        if backend_store is None and not db_path:
            raise ValueError("conflict store requires db_path or backend_store")
        self._backend_store = backend_store
        self.db_path = str(db_path or getattr(backend_store, "db_path", "") or "")
        self._backend = str(getattr(backend_store, "_backend", "sqlite") or "sqlite")
        self._init_db()

    def _connect(self):
        if self._backend_store is not None:
            return self._backend_store._connect()
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS authoring_conflict_cases (
                conflict_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                work_order_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                source_snapshot_hash TEXT NOT NULL DEFAULT '',
                evidence_set_hash TEXT NOT NULL DEFAULT '',
                row_version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS authoring_conflict_decisions (
                decision_id TEXT PRIMARY KEY,
                conflict_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(conflict_id, idempotency_key),
                FOREIGN KEY(conflict_id) REFERENCES authoring_conflict_cases(conflict_id)
            )""",
        )
        with closing(self._connect()) as conn:
            for statement in statements:
                conn.execute(statement)
            # Existing SQLite installations predate the R3 columns. Keep this
            # additive migration harmless when the columns already exist.
            if self._backend != "postgres":
                for statement in (
                    "ALTER TABLE authoring_conflict_cases ADD COLUMN source_snapshot_hash TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE authoring_conflict_cases ADD COLUMN evidence_set_hash TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE authoring_conflict_cases ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1",
                    "ALTER TABLE authoring_conflict_decisions ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''",
                ):
                    try:
                        conn.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).casefold():
                            raise
            else:
                for statement in (
                    "ALTER TABLE authoring_conflict_cases ADD COLUMN IF NOT EXISTS source_snapshot_hash TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE authoring_conflict_cases ADD COLUMN IF NOT EXISTS evidence_set_hash TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE authoring_conflict_cases ADD COLUMN IF NOT EXISTS row_version BIGINT NOT NULL DEFAULT 1",
                    "ALTER TABLE authoring_conflict_decisions ADD COLUMN IF NOT EXISTS request_fingerprint TEXT NOT NULL DEFAULT ''",
                ):
                    conn.execute(statement)
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_authoring_conflicts_work_order
                   ON authoring_conflict_cases(tenant_id, work_order_id, status)"""
            )
            # Legacy rows already bind stamps in JSON. Backfill them before
            # adding uniqueness, so distinct historical snapshots do not
            # collide under the new column's empty default.
            legacy_rows = conn.execute(
                """SELECT conflict_id, payload_json, source_snapshot_hash, evidence_set_hash
                   FROM authoring_conflict_cases
                   WHERE source_snapshot_hash = '' OR evidence_set_hash = ''"""
            ).fetchall()
            for row in legacy_rows:
                case = ConflictCase.model_validate(json.loads(row['payload_json']))
                if (not row['source_snapshot_hash'] and case.snapshot_hash) or (not row['evidence_set_hash'] and case.evidence_set_hash):
                    conn.execute(
                        'UPDATE authoring_conflict_cases SET source_snapshot_hash = ?, evidence_set_hash = ? WHERE conflict_id = ?',
                        (case.snapshot_hash, case.evidence_set_hash, case.conflict_id),
                    )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_authoring_conflicts_fingerprint
                   ON authoring_conflict_cases(tenant_id, work_order_id, fingerprint, source_snapshot_hash)"""
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save_case(self, case: ConflictCase, *, tenant_id: str, work_order_id: str) -> ConflictCase:
        now = _now()
        payload = case.model_dump(mode="json")
        tenant = str(tenant_id)
        work_order = str(work_order_id)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT fingerprint, payload_json FROM authoring_conflict_cases
                       WHERE conflict_id = ?""",
                    (case.conflict_id,),
                ).fetchone()
                if row is not None:
                    if str(row["fingerprint"]) != case.fingerprint:
                        raise ValueError("conflict_case_fingerprint_changed")
                    conn.execute("COMMIT")
                    return ConflictCase.model_validate(json.loads(row["payload_json"]))
                row = conn.execute(
                    """SELECT payload_json FROM authoring_conflict_cases
                       WHERE tenant_id = ? AND work_order_id = ? AND fingerprint = ?
                         AND source_snapshot_hash = ?""",
                    (tenant, work_order, case.fingerprint, case.snapshot_hash),
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return ConflictCase.model_validate(json.loads(row["payload_json"]))
                conn.execute(
                    """INSERT INTO authoring_conflict_cases
                       (conflict_id, tenant_id, work_order_id, fingerprint,
                        source_snapshot_hash, evidence_set_hash, row_version,
                        status, payload_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        case.conflict_id, tenant, work_order, case.fingerprint,
                        case.snapshot_hash, case.evidence_set_hash, case.row_version,
                        case.status, self._json(payload), now, now,
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

    def get_case_context(self, conflict_id: str) -> tuple[ConflictCase, str, str] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT tenant_id, work_order_id, payload_json
                   FROM authoring_conflict_cases WHERE conflict_id = ?""",
                (str(conflict_id),),
            ).fetchone()
        if row is None:
            return None
        return (
            ConflictCase.model_validate(json.loads(row["payload_json"])),
            str(row["tenant_id"]),
            str(row["work_order_id"]),
        )

    def list_cases(
        self,
        *,
        tenant_id: str,
        work_order_id: str,
        statuses: tuple[str, ...] = ("open", "blocked", "deferred", "approval_pending"),
    ) -> list[ConflictCase]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = [str(tenant_id), str(work_order_id), *statuses]
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT payload_json FROM authoring_conflict_cases
                    WHERE tenant_id = ? AND work_order_id = ? AND status IN ({placeholders})
                    ORDER BY created_at, conflict_id""",
                params,
            ).fetchall()
        return [ConflictCase.model_validate(json.loads(row["payload_json"])) for row in rows]

    def list_decisions(
        self,
        conflict_id: str,
        *,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT payload_json FROM authoring_conflict_decisions "
            "WHERE conflict_id = ?"
        )
        params: list[Any] = [str(conflict_id)]
        if tenant_id is not None:
            query += " AND tenant_id = ?"
            params.append(str(tenant_id))
        query += " ORDER BY created_at, decision_id"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

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
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("conflict_decision_idempotency_key_required")
        request_fingerprint = _fingerprint({
            "conflict_id": str(conflict_id),
            "expected_fingerprint": expected_fingerprint,
            "expected_snapshot_hash": expected_snapshot_hash,
            "expected_evidence_set_hash": expected_evidence_set_hash,
            "decision": decision.model_dump(mode="json"),
        })
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Serialize all decisions for one case on PostgreSQL too.
                # Read idempotency only after acquiring the row lock, so a
                # concurrent replay sees the first committed response.
                query = """SELECT tenant_id, payload_json, status, row_version,
                                  source_snapshot_hash, evidence_set_hash
                           FROM authoring_conflict_cases WHERE conflict_id = ?"""
                if self._backend == "postgres":
                    query += " FOR UPDATE"
                row = conn.execute(query, (str(conflict_id),)).fetchone()
                if row is None:
                    raise KeyError("conflict_case_not_found")
                if str(row["tenant_id"]) != str(tenant_id):
                    raise PermissionError("conflict_case_tenant_mismatch")
                existing = conn.execute(
                    """SELECT payload_json, request_fingerprint
                       FROM authoring_conflict_decisions
                       WHERE conflict_id = ? AND idempotency_key = ?""",
                    (str(conflict_id), key),
                ).fetchone()
                if existing is not None:
                    stored_fp = str(existing["request_fingerprint"] or "")
                    if not stored_fp:
                        stored = json.loads(existing["payload_json"])
                        legacy_fp = _fingerprint({
                            "conflict_id": stored.get("conflict_id"),
                            "decision": ConflictDecision.model_validate(stored.get('decision')).model_dump(mode='json'),
                        })
                        same_legacy_request = legacy_fp == _fingerprint({
                            "conflict_id": str(conflict_id),
                            "decision": decision.model_dump(mode="json"),
                        })
                    else:
                        same_legacy_request = False
                    if (
                        (stored_fp and stored_fp != request_fingerprint)
                        or (not stored_fp and not same_legacy_request)
                    ):
                        raise ValueError("idempotency_conflict")
                    conn.execute("COMMIT")
                    return json.loads(existing["payload_json"])
                case = ConflictCase.model_validate(json.loads(row["payload_json"]))
                current_version = int(row["row_version"] or case.row_version or 1)
                if expected_version is not None and current_version != int(expected_version):
                    raise ValueError("conflict_decision_stale")
                if case.status in {"resolved", "dismissed"}:
                    raise ValueError("conflict_case_already_resolved")
                if case.fingerprint != expected_fingerprint:
                    raise ValueError("conflict_case_changed")
                if str(row["source_snapshot_hash"] or case.snapshot_hash) != str(expected_snapshot_hash):
                    raise ValueError("source_snapshot_changed")
                if str(row["evidence_set_hash"] or case.evidence_set_hash) != str(expected_evidence_set_hash):
                    raise ValueError("evidence_set_changed")
                if case.policy_version != decision.policy_version:
                    raise ValueError("conflict_policy_changed")
                if decision.conflict_id != case.conflict_id:
                    raise ValueError("conflict_decision_case_mismatch")
                if case.allowed_actions and decision.action not in set(case.allowed_actions):
                    raise ValueError("conflict_decision_action_not_allowed")
                claim_ids = {claim.claim_id for claim in case.claims}
                if decision.selected_claim_id and decision.selected_claim_id not in claim_ids:
                    raise ValueError("conflict_decision_claim_not_found")
                if decision.action in {
                    "choose_claim", "accept_authoritative_source", "choose_design_candidate",
                } and not decision.selected_claim_id and decision.proposed_value is None:
                    raise ValueError("conflict_decision_claim_required")
                status = {
                    "defer": "deferred",
                    "request_change_approval": "approval_pending",
                    "choose_design_candidate": "approval_pending",
                    "integrity_block": "blocked",
                }.get(decision.action, "resolved")
                next_version = current_version + 1
                payload = {
                    "decision_id": f"conflict-decision-{uuid.uuid4().hex}",
                    "conflict_id": case.conflict_id,
                    "tenant_id": str(tenant_id),
                    "actor_id": str(actor_id)[:200],
                    "idempotency_key": key,
                    "request_fingerprint": request_fingerprint,
                    "decision": decision.model_dump(mode="json"),
                    "status": status,
                    "created_at": now,
                }
                conn.execute(
                    """INSERT INTO authoring_conflict_decisions
                       (decision_id, conflict_id, tenant_id, actor_id, idempotency_key,
                        request_fingerprint, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["decision_id"], case.conflict_id, str(tenant_id),
                        str(actor_id)[:200], key, request_fingerprint, self._json(payload), now,
                    ),
                )
                updated = case.model_copy(update={"status": status, "row_version": next_version})
                updated_cursor = conn.execute(
                    """UPDATE authoring_conflict_cases
                       SET status = ?, row_version = ?, payload_json = ?, updated_at = ?
                       WHERE conflict_id = ? AND row_version = ?""",
                    (
                        updated.status, next_version, self._json(updated.model_dump(mode="json")),
                        now, case.conflict_id, current_version,
                    ),
                )
                if int(getattr(updated_cursor, "rowcount", 1) or 0) != 1:
                    raise ValueError("conflict_decision_stale")
                conn.execute("COMMIT")
                return payload
            except Exception:
                conn.execute("ROLLBACK")
                raise


__all__ = ["ConflictDecisionStore"]
