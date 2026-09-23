"""Server-owned sandbox capability grants and immutable authorization snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Iterable

import src.settings as settings

from .models import sha256_text


SANDBOX_CAPABILITIES = frozenset(
    {
        "sandbox.execute",
        "sandbox.network",
        "sandbox.internal_data",
        "sandbox.write_artifact",
    }
)
GATEWAY_CAPABILITIES = frozenset({"external_tool.invoke", "external_tool.write"})
AUTHORIZED_CAPABILITIES = SANDBOX_CAPABILITIES | GATEWAY_CAPABILITIES
PRINCIPAL_TYPES = frozenset({"user", "department"})


class SandboxAuthorizationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SandboxAuthorizationStore:
    """SQLite projection of administrator-managed capability grants.

    ``SANDBOX_AUTHORIZED_CAPABILITIES`` is the deployment upper bound.  When a
    principal has explicit rows, those rows narrow the upper bound for that
    principal; absent explicit rows preserve the deployment default.  A request
    body can never add a capability.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS sandbox_capability_grants (
        principal_type TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        capability TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_by TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(principal_type, principal_id, capability)
    );
    CREATE INDEX IF NOT EXISTS idx_sandbox_capability_grants_principal
        ON sandbox_capability_grants(principal_type, principal_id, enabled);
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = os.path.abspath(str(db_path or settings.SANDBOX_TASK_DB_PATH))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, closing(self._connect()) as conn:
            conn.executescript(self._DDL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _principal(principal_type: str, principal_id: Any) -> tuple[str, str]:
        kind = str(principal_type or "").strip().lower()
        value = str(principal_id or "").strip()
        if kind not in PRINCIPAL_TYPES or not value or len(value) > 128 or any(char in value for char in "/\\\x00"):
            raise SandboxAuthorizationError("principal type or id is invalid")
        return kind, value

    @staticmethod
    def _capabilities(values: Iterable[Any]) -> tuple[str, ...]:
        normalized = tuple(sorted({str(value).strip() for value in values if str(value).strip()}))
        unknown = set(normalized) - AUTHORIZED_CAPABILITIES
        if unknown:
            raise SandboxAuthorizationError("unsupported sandbox capabilities: " + ", ".join(sorted(unknown)))
        return normalized

    def grant(self, principal_type: str, principal_id: Any, capabilities: Iterable[Any], *, actor: str = "") -> list[str]:
        kind, value = self._principal(principal_type, principal_id)
        caps = self._capabilities(capabilities)
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for capability in caps:
                    conn.execute(
                        "INSERT INTO sandbox_capability_grants(principal_type, principal_id, capability, enabled, updated_by, updated_at) "
                        "VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(principal_type, principal_id, capability) DO UPDATE SET enabled=1, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                        (kind, value, capability, str(actor or "")[:128], now),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return list(self.list_for(kind, value))

    def revoke(self, principal_type: str, principal_id: Any, capabilities: Iterable[Any], *, actor: str = "") -> list[str]:
        kind, value = self._principal(principal_type, principal_id)
        caps = self._capabilities(capabilities)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for capability in caps:
                    conn.execute(
                        "UPDATE sandbox_capability_grants SET enabled=0, updated_by=?, updated_at=? WHERE principal_type=? AND principal_id=? AND capability=?",
                        (str(actor or "")[:128], _now(), kind, value, capability),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return list(self.list_for(kind, value))

    def list_for(self, principal_type: str, principal_id: Any) -> tuple[str, ...]:
        kind, value = self._principal(principal_type, principal_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT capability FROM sandbox_capability_grants WHERE principal_type=? AND principal_id=? AND enabled=1 ORDER BY capability",
                (kind, value),
            ).fetchall()
        return tuple(str(row["capability"]) for row in rows)

    def has_explicit_rows(self, principal_type: str, principal_id: Any) -> bool:
        kind, value = self._principal(principal_type, principal_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM sandbox_capability_grants WHERE principal_type=? AND principal_id=? LIMIT 1",
                (kind, value),
            ).fetchone()
        return row is not None

    def effective_capabilities(
        self,
        *,
        user_id: Any,
        department_id: Any,
        context_capabilities: Iterable[Any] = (),
    ) -> set[str]:
        deployment = {
            str(value).strip()
            for value in getattr(settings, "SANDBOX_AUTHORIZED_CAPABILITIES", ())
            if str(value).strip()
        }
        deployment &= set(AUTHORIZED_CAPABILITIES)
        user = str(user_id or "").strip()
        department = str(department_id or "").strip()
        explicit = False
        granted: set[str] = set()
        if user:
            explicit = explicit or self.has_explicit_rows("user", user)
            granted.update(self.list_for("user", user))
        if department:
            explicit = explicit or self.has_explicit_rows("department", department)
            granted.update(self.list_for("department", department))
        if explicit:
            result = deployment & granted
        else:
            # Context metadata is server-owned, but still cannot exceed the
            # deployment upper bound.  Empty metadata means the deployment
            # default applies.
            context = {str(value).strip() for value in context_capabilities if str(value).strip()}
            result = deployment if not context else deployment & context
        return result


def capability_snapshot_hash(
    *, user_id: Any, department_id: Any, capabilities: Iterable[Any], policy_snapshot_hash: str
) -> str:
    payload = {
        "user_id": str(user_id or ""),
        "department_id": str(department_id or ""),
        "capabilities": sorted({str(value).strip() for value in capabilities if str(value).strip()}),
        "policy_snapshot_hash": str(policy_snapshot_hash or ""),
    }
    return sha256_text(_json(payload))


__all__ = [
    "PRINCIPAL_TYPES",
    "AUTHORIZED_CAPABILITIES",
    "GATEWAY_CAPABILITIES",
    "SANDBOX_CAPABILITIES",
    "SandboxAuthorizationError",
    "SandboxAuthorizationStore",
    "capability_snapshot_hash",
]
