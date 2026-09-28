"""Phase 2 write proposals: candidate-artifact mode with user confirmation.

One row per model-initiated write call.  The row never stores argument bodies
(only a digest), and the state machine + ``idempotency_key`` guarantee that a
confirmed proposal is executed at most once even if the worker retries.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import src.settings as settings
from src.agents.mcp.store import McpCredentialError, decrypt_json, encrypt_json

PROPOSAL_STATUSES = frozenset(
    {"proposed", "confirmed", "rejected", "expired", "running", "completed", "failed"}
)

_DDL = """
CREATE TABLE IF NOT EXISTS mcp_write_proposals (
    proposal_id      TEXT PRIMARY KEY,
    user_id          INTEGER NOT NULL,
    slug             TEXT NOT NULL,
    tool_name        TEXT NOT NULL,
    arguments_digest TEXT NOT NULL DEFAULT '',
    arguments_ciphertext TEXT NOT NULL DEFAULT '',
    artifact_hint    TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'proposed',
    idempotency_key  TEXT NOT NULL DEFAULT '',
    artifact_ref     TEXT NOT NULL DEFAULT '',
    artifact_sha256  TEXT NOT NULL DEFAULT '',
    error_code       TEXT NOT NULL DEFAULT '',
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_write_proposals_user
    ON mcp_write_proposals(user_id, status);
CREATE INDEX IF NOT EXISTS idx_mcp_write_proposals_status
    ON mcp_write_proposals(status, created_at);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.Lock()


def _iso(value: datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class McpWriteProposal:
    proposal_id: str
    user_id: int
    slug: str
    tool_name: str
    arguments_digest: str
    artifact_hint: str
    status: str
    idempotency_key: str
    artifact_ref: str
    artifact_sha256: str
    error_code: str
    attempts: int
    created_at: str
    expires_at: str
    updated_at: str
    # Encrypted arguments (never returned by to_dict / audit / UI).
    arguments_ciphertext: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "slug": self.slug,
            "tool_name": self.tool_name,
            "arguments_digest": self.arguments_digest,
            "artifact_hint": self.artifact_hint,
            "status": self.status,
            "artifact_ref": self.artifact_ref,
            "artifact_sha256": self.artifact_sha256,
            "artifact_filename": os.path.basename(self.artifact_ref) if self.artifact_ref else "",
            "error_code": self.error_code,
            "attempts": int(self.attempts),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
        }


def arguments_digest(arguments: dict[str, Any]) -> str:
    """Stable digest of the call arguments; the body itself is never stored."""

    import hashlib

    try:
        payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(arguments)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SqliteMcpWriteProposalStore:
    """SQLite store for write proposals, living in ``AUTH_DB_PATH``."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.AUTH_DB_PATH
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        init_key = os.path.abspath(self.db_path)
        if init_key in _INITIALIZED_PATHS:
            return
        with _INIT_LOCK:
            if init_key in _INITIALIZED_PATHS:
                return
            self._init_db()
            _INITIALIZED_PATHS.add(init_key)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(_DDL)
            # Forward-only column migrations keep early phase-2 databases usable.
            self._ensure_column(
                conn,
                "mcp_write_proposals",
                "arguments_ciphertext",
                "arguments_ciphertext TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn,
                "mcp_write_proposals",
                "attempts",
                "attempts INTEGER NOT NULL DEFAULT 0",
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> McpWriteProposal:
        return McpWriteProposal(
            proposal_id=str(row["proposal_id"]),
            user_id=int(row["user_id"]),
            slug=str(row["slug"]),
            tool_name=str(row["tool_name"]),
            arguments_digest=str(row["arguments_digest"]),
            artifact_hint=str(row["artifact_hint"]),
            status=str(row["status"]),
            idempotency_key=str(row["idempotency_key"]),
            artifact_ref=str(row["artifact_ref"]),
            artifact_sha256=str(row["artifact_sha256"]),
            error_code=str(row["error_code"]),
            attempts=int(row["attempts"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            updated_at=str(row["updated_at"]),
            arguments_ciphertext=str(row["arguments_ciphertext"]),
        )

    # ------------------------------------------------------------------
    def create(
        self,
        *,
        user_id: int,
        slug: str,
        tool_name: str,
        arguments_digest: str,
        arguments: dict[str, Any] | None = None,
        artifact_hint: str = "",
        ttl_seconds: int | None = None,
    ) -> McpWriteProposal:
        proposal_id = uuid.uuid4().hex
        now = _utc_now()
        ttl = int(
            ttl_seconds
            if ttl_seconds is not None
            else getattr(settings, "MCP_WRITE_CONFIRM_TTL_SECONDS", 1800)
        )
        expires_at = now + timedelta(seconds=max(1, ttl))
        # Arguments are stored encrypted only: the worker must replay them
        # after confirmation, while audits/UI must never see the body.
        ciphertext = encrypt_json(arguments or {}) if arguments is not None else ""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO mcp_write_proposals (
                        proposal_id, user_id, slug, tool_name, arguments_digest,
                        arguments_ciphertext, artifact_hint, status, idempotency_key,
                        artifact_ref, artifact_sha256, error_code, attempts,
                        created_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', '', '', '', '', 0, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        int(user_id),
                        str(slug),
                        str(tool_name),
                        str(arguments_digest),
                        ciphertext,
                        str(artifact_hint or "")[:200],
                        _iso(now),
                        _iso(expires_at),
                        _iso(now),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        proposal = self.get(int(user_id), proposal_id)
        if proposal is None:  # pragma: no cover - insert just committed
            raise RuntimeError("write proposal insert lost")
        return proposal

    @staticmethod
    def decrypt_arguments(proposal: McpWriteProposal) -> dict[str, Any]:
        """Replay arguments for the worker; raises ``McpCredentialError``."""

        payload = decrypt_json(proposal.arguments_ciphertext)
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise McpCredentialError("write proposal arguments are not an object")
        return payload

    def get(self, user_id: int, proposal_id: str) -> McpWriteProposal | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM mcp_write_proposals WHERE proposal_id = ? AND user_id = ?",
                (str(proposal_id), int(user_id)),
            ).fetchone()
        return self._row_to_proposal(row) if row else None

    def list_for_user(
        self, user_id: int, *, statuses: list[str] | None = None, limit: int = 50
    ) -> list[McpWriteProposal]:
        clauses = ["user_id = ?"]
        params: list[Any] = [int(user_id)]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(str(item) for item in statuses)
        params.append(max(1, int(limit)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM mcp_write_proposals WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM mcp_write_proposals GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    # ------------------------------------------------------------------
    def _decide(
        self, user_id: int, proposal_id: str, *, target: str
    ) -> tuple[McpWriteProposal | None, str]:
        now = _utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM mcp_write_proposals WHERE proposal_id = ? AND user_id = ?",
                    (str(proposal_id), int(user_id)),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return None, "not_found"
                proposal = self._row_to_proposal(row)
                if proposal.status in {"proposed", "confirmed"} and proposal.expires_at <= _iso(now):
                    conn.execute(
                        "UPDATE mcp_write_proposals SET status = 'expired', updated_at = ? "
                        "WHERE proposal_id = ?",
                        (_iso(now), proposal.proposal_id),
                    )
                    conn.execute("COMMIT")
                    return self.get(int(user_id), proposal.proposal_id), "expired"
                if proposal.status != "proposed":
                    conn.execute("ROLLBACK")
                    return proposal, "invalid_state"
                conn.execute(
                    "UPDATE mcp_write_proposals SET status = ?, updated_at = ? "
                    "WHERE proposal_id = ? AND status = 'proposed'",
                    (target, _iso(now), proposal.proposal_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get(int(user_id), proposal.proposal_id), "ok"

    def confirm(self, user_id: int, proposal_id: str) -> tuple[McpWriteProposal | None, str]:
        return self._decide(user_id, proposal_id, target="confirmed")

    def reject(self, user_id: int, proposal_id: str) -> tuple[McpWriteProposal | None, str]:
        return self._decide(user_id, proposal_id, target="rejected")

    def expire_overdue(self) -> int:
        now = _iso(_utc_now())
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE mcp_write_proposals SET status = 'expired', updated_at = ? "
                "WHERE status IN ('proposed', 'confirmed') AND expires_at <= ?",
                (now, now),
            )
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    def claim(self, worker_id: str, *, limit: int = 1) -> list[McpWriteProposal]:
        """Atomically claim confirmed proposals; sets the idempotency key."""

        now = _iso(_utc_now())
        claimed: list[McpWriteProposal] = []
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT proposal_id FROM mcp_write_proposals "
                    "WHERE status = 'confirmed' AND expires_at > ? "
                    "ORDER BY created_at LIMIT ?",
                    (now, max(1, int(limit))),
                ).fetchall()
                for row in rows:
                    proposal_id = str(row["proposal_id"])
                    cursor = conn.execute(
                        """
                        UPDATE mcp_write_proposals SET
                            status = 'running',
                            attempts = attempts + 1,
                            idempotency_key = CASE WHEN idempotency_key = '' THEN ? ELSE idempotency_key END,
                            updated_at = ?
                        WHERE proposal_id = ? AND status = 'confirmed'
                        """,
                        (uuid.uuid4().hex, now, proposal_id),
                    )
                    if cursor.rowcount:
                        claimed_row = conn.execute(
                            "SELECT * FROM mcp_write_proposals WHERE proposal_id = ?",
                            (proposal_id,),
                        ).fetchone()
                        if claimed_row is not None:
                            claimed.append(self._row_to_proposal(claimed_row))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return claimed

    def complete(
        self,
        proposal_id: str,
        idempotency_key: str,
        *,
        artifact_ref: str,
        artifact_sha256: str,
    ) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE mcp_write_proposals SET
                    status = 'completed', artifact_ref = ?, artifact_sha256 = ?,
                    error_code = '', updated_at = ?
                WHERE proposal_id = ? AND status = 'running' AND idempotency_key = ?
                """,
                (
                    str(artifact_ref),
                    str(artifact_sha256),
                    _iso(_utc_now()),
                    str(proposal_id),
                    str(idempotency_key),
                ),
            )
        return bool(cursor.rowcount)

    def fail(
        self,
        proposal_id: str,
        idempotency_key: str,
        *,
        error_code: str,
        retryable: bool,
    ) -> str:
        """Record a failed attempt; returns the resulting status."""

        max_attempts = max(1, int(getattr(settings, "MCP_WRITE_MAX_ATTEMPTS", 3)))
        now = _iso(_utc_now())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM mcp_write_proposals WHERE proposal_id = ?",
                    (str(proposal_id),),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return "not_found"
                proposal = self._row_to_proposal(row)
                if proposal.status != "running" or proposal.idempotency_key != str(
                    idempotency_key
                ):
                    conn.execute("ROLLBACK")
                    return proposal.status
                next_status = (
                    "confirmed"
                    if retryable and proposal.attempts < max_attempts
                    else "failed"
                )
                conn.execute(
                    "UPDATE mcp_write_proposals SET status = ?, error_code = ?, updated_at = ? "
                    "WHERE proposal_id = ?",
                    (next_status, str(error_code)[:100], now, proposal.proposal_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return next_status


def build_write_proposal_store(settings_module: Any | None = None) -> SqliteMcpWriteProposalStore:
    """Build the proposal store; like the server store it lives in AUTH_DB_PATH."""

    if settings_module is None:
        import src.settings as settings_module  # type: ignore[no-redef]

    return SqliteMcpWriteProposalStore(getattr(settings_module, "AUTH_DB_PATH", None))


__all__ = [
    "McpWriteProposal",
    "PROPOSAL_STATUSES",
    "SqliteMcpWriteProposalStore",
    "arguments_digest",
    "build_write_proposal_store",
]
