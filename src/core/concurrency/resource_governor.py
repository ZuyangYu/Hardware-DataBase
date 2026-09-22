"""Resource governor: bounded concurrency for downstream calls (P0b).

Two scopes:

- ``process``: per-process semaphores; interactive callers keep a reserved
  slice that background callers can never occupy.
- ``deployment``: a shared quota table with lease rows so every API/Worker
  process shares one budget — SQLite (short ``BEGIN IMMEDIATE`` transactions)
  on one host, or PostgreSQL (advisory-lock serialized short transactions)
  across hosts.

Every permit carries a lease in deployment scope; a crashed process leaves
``uncertain`` rows instead of silently freeing phantom capacity.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

try:  # PostgreSQL is optional for sqlite-only developer installs.
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover - exercised only without psycopg installed
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    # resource: (capacity, reserved_interactive)
    "llm": (4, 2),
    "rag": (8, 4),
    "tool": (16, 0),
    "chat": (4, 0),
    "heavy_job": (4, 1),
    "ocr": (1, 0),
    "export": (4, 0),
    "memory": (1, 0),
    # External MCP plugins: slow third-party calls must not occupy the shared
    # local-tool budget; interactive callers keep one reserved slot.
    "mcp": (4, 1),
}


class GovernorRejected(RuntimeError):
    """Raised when a resource cannot be admitted before its deadline."""

    def __init__(self, resource: str, reason: str) -> None:
        super().__init__(f"resource {resource!r} rejected: {reason}")
        self.resource = resource
        self.reason = reason


class GovernorPermit:
    """A held resource slot; always released via ``release()``/context exit."""

    def __init__(
        self,
        governor: "ResourceGovernor",
        permit_id: str,
        resource: str,
        interactive: bool,
        deadline_monotonic: float,
    ) -> None:
        self.governor = governor
        self.permit_id = permit_id
        self.resource = resource
        self.interactive = interactive
        self.deadline_monotonic = deadline_monotonic
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self.governor._release(self)

    @property
    def released(self) -> bool:
        return self._released

    def renew(self, *, lease_seconds: int | None = None) -> bool:
        return self.governor._renew(self, lease_seconds=lease_seconds)

    def finish(self, *, outcome: str = "completed", evidence: str = "", reason: str = "") -> bool:
        """Explicit end confirmation; see ``SqliteQuotaStore.finish``."""
        return self.governor._finish(self, outcome=outcome, evidence=evidence, reason=reason)

    def reconcile(self, *, evidence: str, reason: str = "", operator: str = "") -> bool:
        """Evidence-backed release of this uncertain permit (if any)."""
        return self.governor._reconcile(
            self.permit_id, evidence=evidence, reason=reason, operator=operator
        )

    def __enter__(self) -> "GovernorPermit":
        return self

    def __exit__(self, *_args) -> None:
        self.release()


def _lease_active(expires_at: Any, now: datetime) -> bool:
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except (TypeError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > now


class SqliteQuotaStore:
    """Deployment-scope quota table with lease rows and an audit trail.

    Identity is pinned by ``deployment_id`` / ``schema_version`` / quota
    ``config_revision`` rows: a wrong mount, an incompatible schema or a
    production process without a pre-provisioned database fails fast instead
    of silently creating a same-named empty store (plan §6.3).
    """

    SCHEMA_VERSION = 1
    VALID_RELEASE_EVIDENCE = {
        "downstream_completed",
        "downstream_cancelled",
        "server_deadline_passed",
        "operator_verified_restart",
        "operator_verified_cancel",
    }

    def __init__(
        self,
        path: str,
        *,
        lease_seconds: int = 300,
        deployment_id: str | None = None,
        config_revision: str = "",
        require_preconfigured: bool = False,
    ) -> None:
        self.path = os.path.abspath(str(path))
        self.lease_seconds = max(30, int(lease_seconds))
        self.deployment_id = str(deployment_id or "").strip() or "local"
        self.config_revision = str(config_revision or "")
        exists = os.path.isfile(self.path)
        if require_preconfigured and not exists:
            raise RuntimeError(
                f"resource governor requires a pre-provisioned quota database: {self.path}"
            )
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        self._require_preconfigured = bool(require_preconfigured)
        # Multi-process startup runs this schema/identity init concurrently;
        # retry bounded busy errors instead of failing before the process can
        # even mark itself ready (plan §6.1).
        self._run_with_busy_retry(self._init_once, deadline_seconds=5.0)

    def _init_once(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._init_schema(conn)
                self._check_identity(
                    conn, require_preconfigured=self._require_preconfigured
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_permits (
                permit_id TEXT PRIMARY KEY,
                resource TEXT NOT NULL,
                interactive INTEGER NOT NULL DEFAULT 0,
                owner_id TEXT NOT NULL DEFAULT '',
                attempt_id TEXT NOT NULL DEFAULT '',
                weight INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        for column, ddl in (
            ("started_at", "TEXT NOT NULL DEFAULT ''"),
            ("hard_deadline_at", "TEXT NOT NULL DEFAULT ''"),
            ("provider_request_id", "TEXT NOT NULL DEFAULT ''"),
            ("uncertain_since", "TEXT NOT NULL DEFAULT ''"),
            ("escalated_at", "TEXT NOT NULL DEFAULT ''"),
            ("recovery_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            self._ensure_column(conn, "resource_permits", column, ddl)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_permits_state"
            " ON resource_permits(resource, state)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_permit_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                permit_id TEXT NOT NULL,
                resource TEXT NOT NULL,
                action TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                operator TEXT NOT NULL DEFAULT '',
                previous_state TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_governor_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_maintenance_lease (
                scope TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT '',
                fencing_token INTEGER NOT NULL DEFAULT 0,
                lease_expires_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_quotas (
                resource TEXT PRIMARY KEY,
                capacity INTEGER NOT NULL,
                reserved_interactive INTEGER NOT NULL DEFAULT 0,
                config_revision TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _check_identity(self, conn, *, require_preconfigured: bool) -> None:
        rows = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM resource_governor_meta").fetchall()
        }
        if not rows:
            if require_preconfigured:
                raise RuntimeError(
                    f"quota database {self.path} has no identity rows; provision it first"
                )
            now = datetime.now(timezone.utc).isoformat()
            for key, value in (
                ("deployment_id", self.deployment_id),
                ("schema_version", str(self.SCHEMA_VERSION)),
                ("config_revision", self.config_revision),
            ):
                conn.execute(
                    "INSERT INTO resource_governor_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
            conn.execute(
                "UPDATE resource_governor_meta SET value = ? WHERE key = 'updated_at'",
                (now,),
            )
            return
        stored_deployment = rows.get("deployment_id", "")
        if stored_deployment != self.deployment_id:
            raise RuntimeError(
                f"quota database deployment_id mismatch: file={stored_deployment!r} "
                f"process={self.deployment_id!r}"
            )
        stored_version = rows.get("schema_version", "")
        if stored_version != str(self.SCHEMA_VERSION):
            raise RuntimeError(
                f"quota database schema_version mismatch: file={stored_version!r} "
                f"code={self.SCHEMA_VERSION!r}"
            )

    def ensure_quotas(self, defaults: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
        """Seed/return the deployment capacities stored in the quota database.

        Stored values are authoritative for every process; per-worker env
        values never multiply the deployment budget.
        """
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = {
                    str(row["resource"]): (
                        int(row["capacity"]),
                        int(row["reserved_interactive"]),
                    )
                    for row in conn.execute(
                        "SELECT resource, capacity, reserved_interactive FROM resource_quotas"
                    ).fetchall()
                }
                for resource, (capacity, reserved) in defaults.items():
                    if resource not in existing:
                        conn.execute(
                            """
                            INSERT INTO resource_quotas
                                (resource, capacity, reserved_interactive, config_revision, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (resource, max(1, int(capacity)), max(0, int(reserved)), self.config_revision, now),
                        )
                        existing[resource] = (max(1, int(capacity)), max(0, int(reserved)))
                conn.execute("COMMIT")
                return existing
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update_quota(self, resource: str, *, capacity: int, reserved_interactive: int, operator: str = "") -> None:
        """Audited capacity change; callers must have validated the new value."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO resource_quotas
                        (resource, capacity, reserved_interactive, config_revision, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(resource) DO UPDATE SET
                        capacity = excluded.capacity,
                        reserved_interactive = excluded.reserved_interactive,
                        config_revision = excluded.config_revision,
                        updated_at = excluded.updated_at
                    """,
                    (str(resource), max(1, int(capacity)), max(0, int(reserved_interactive)), self.config_revision, now),
                )
                conn.execute(
                    """
                    INSERT INTO resource_permit_audit
                        (permit_id, resource, action, evidence, reason, operator, previous_state, recorded_at)
                    VALUES ('', ?, 'quota_update', 'config_revision', ?, ?, '', ?)
                    """,
                    (str(resource), f"capacity={capacity} reserved={reserved_interactive}", str(operator), now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Short quota transactions only; waiters must not sleep inside a lock.
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def reap_expired(self, conn) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            UPDATE resource_permits
            SET state = 'uncertain',
                uncertain_since = CASE WHEN uncertain_since = '' THEN ? ELSE uncertain_since END
            WHERE state = 'active' AND datetime(expires_at) < datetime('now')
            """,
            (now,),
        )
        return int(cursor.rowcount or 0)

    def maintain(self, escalate_seconds: int) -> dict[str, int]:
        """Reap expired permits and escalate long-isolated uncertain ones.

        Escalation never releases capacity: it raises an operations alert and
        blocks new admissions for that resource until an evidence-backed
        recovery resets it (plan §6.3.1).
        """
        threshold = max(60, int(escalate_seconds))
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                reaped = self.reap_expired(conn)
                cursor = conn.execute(
                    """
                    UPDATE resource_permits
                    SET escalated_at = ?
                    WHERE state = 'uncertain' AND escalated_at = ''
                      AND uncertain_since <> ''
                      AND datetime(uncertain_since) < datetime('now', ?)
                    """,
                    (now.isoformat(), f"-{threshold} seconds"),
                )
                escalated = int(cursor.rowcount or 0)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if escalated:
            self._record_escalation(escalated)
        if reaped or escalated:
            self._publish_uncertain_gauge()
        return {"reaped": reaped, "escalated": escalated}

    @staticmethod
    def _record_escalation(count: int) -> None:
        try:
            from src.core.logger import warn
            from src.observability.metrics import counter

            counter(
                "hdb.resource.uncertain_escalated",
                attributes={"resource": "deployment"},
                value=int(count),
                description="Uncertain permits escalated to operations attention",
            )
            warn(
                "resource governor: isolated permits exceeded the escalation window; "
                "new admissions paused until an evidence-backed recovery runs"
            )
        except Exception:
            pass

    def _publish_uncertain_gauge(self) -> None:
        try:
            from src.observability.metrics import set_resource_state

            snapshot = self.snapshot()
            set_resource_state(
                active={key: value.get("active", 0) for key, value in snapshot.items()},
                uncertain={key: value.get("uncertain", 0) for key, value in snapshot.items()},
            )
        except Exception:
            pass

    def escalated_resources(self) -> set[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT resource FROM resource_permits"
                " WHERE state = 'uncertain' AND escalated_at <> ''"
            ).fetchall()
        return {str(row["resource"]) for row in rows}

    def uncertain_permits(self, resource: str | None = None) -> list[dict]:
        query = "SELECT * FROM resource_permits WHERE state = 'uncertain'"
        params: list[object] = []
        if resource:
            query += " AND resource = ?"
            params.append(str(resource))
        query += " ORDER BY uncertain_since, permit_id"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def release_uncertain(
        self,
        permit_id: str,
        *,
        evidence: str,
        reason: str = "",
        operator: str = "",
    ) -> bool:
        """Evidence-backed CAS release of one uncertain permit (idempotent)."""
        evidence = str(evidence or "").strip()
        if evidence not in self.VALID_RELEASE_EVIDENCE:
            raise ValueError(
                "release requires a recognized downstream end evidence; "
                f"got {evidence or '<empty>'!r}"
            )
        now = datetime.now(timezone.utc).isoformat()

        def _op() -> bool:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM resource_permits WHERE permit_id = ?", (str(permit_id),)
                    ).fetchone()
                    if row is None:
                        conn.execute("COMMIT")
                        return False
                    previous = str(row["state"])
                    if previous == "released":
                        conn.execute("COMMIT")
                        return False
                    if previous != "uncertain":
                        conn.execute("COMMIT")
                        return False
                    conn.execute(
                        """
                        UPDATE resource_permits
                        SET state = 'released', recovery_reason = ?
                        WHERE permit_id = ? AND state = 'uncertain'
                        """,
                        (str(reason)[:500], str(permit_id)),
                    )
                    conn.execute(
                        """
                        INSERT INTO resource_permit_audit
                            (permit_id, resource, action, evidence, reason, operator, previous_state, recorded_at)
                        VALUES (?, ?, 'release_uncertain', ?, ?, ?, ?, ?)
                        """,
                        (
                            str(permit_id),
                            str(row["resource"]),
                            evidence,
                            str(reason)[:500],
                            str(operator),
                            previous,
                            now,
                        ),
                    )
                    conn.execute("COMMIT")
                    return True
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

        return bool(self._run_with_busy_retry(_op))

    def audit_entries(self, permit_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM resource_permit_audit"
        params: list[object] = []
        if permit_id:
            query += " WHERE permit_id = ?"
            params.append(str(permit_id))
        query += " ORDER BY audit_id"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
        text = str(exc).lower()
        return "locked" in text or "busy" in text

    def _run_with_busy_retry(self, operation, *, deadline_seconds: float = 2.0):
        """Bounded retry for short quota transactions (plan §6.3).

        Busy errors are retried without sleeping inside a lock; the caller's
        request deadline bounds the total wait.
        """
        deadline = time.monotonic() + max(0.0, float(deadline_seconds))
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not self._is_busy_error(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    MAINTENANCE_SCOPE = "governor-maintenance"

    def try_acquire_maintenance(self, owner_id: str, *, lease_seconds: int = 15) -> int | None:
        """Leader election for the uncertain-permit maintenance loop (P0-006).

        Only the lease holder scans; takeover increments the fencing token so a
        paused maintainer cannot keep acting as leader.
        """
        owner = str(owner_id or "")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()

        def _op() -> int | None:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM resource_maintenance_lease WHERE scope = ?",
                        (self.MAINTENANCE_SCOPE,),
                    ).fetchone()
                    if row is not None and str(row["owner_id"] or "") == owner:
                        conn.execute(
                            """
                            UPDATE resource_maintenance_lease
                            SET lease_expires_at = ?, updated_at = ?
                            WHERE scope = ?
                            """,
                            (expires, now_text, self.MAINTENANCE_SCOPE),
                        )
                        token = int(row["fencing_token"] or 0)
                    elif row is not None and _lease_active(row["lease_expires_at"], now):
                        conn.execute("COMMIT")
                        return None
                    else:
                        conn.execute(
                            """
                            INSERT INTO resource_maintenance_lease
                                (scope, owner_id, fencing_token, lease_expires_at, updated_at)
                            VALUES (
                                ?, ?, COALESCE((
                                    SELECT fencing_token FROM resource_maintenance_lease WHERE scope = ?
                                ), 0) + 1, ?, ?
                            )
                            ON CONFLICT(scope) DO UPDATE SET
                                owner_id = excluded.owner_id,
                                fencing_token = resource_maintenance_lease.fencing_token + 1,
                                lease_expires_at = excluded.lease_expires_at,
                                updated_at = excluded.updated_at
                            """,
                            (self.MAINTENANCE_SCOPE, owner, self.MAINTENANCE_SCOPE, expires, now_text),
                        )
                        current = conn.execute(
                            "SELECT fencing_token FROM resource_maintenance_lease WHERE scope = ?",
                            (self.MAINTENANCE_SCOPE,),
                        ).fetchone()
                        token = int(current["fencing_token"] or 0) if current else None
                    conn.execute("COMMIT")
                    return token
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

        return self._run_with_busy_retry(_op)

    def renew_maintenance(self, owner_id: str, token: int, *, lease_seconds: int = 15) -> bool:
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat()

        def _op() -> bool:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """
                    UPDATE resource_maintenance_lease
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE scope = ? AND owner_id = ? AND fencing_token = ?
                    """,
                    (expires, datetime.now(timezone.utc).isoformat(), self.MAINTENANCE_SCOPE, str(owner_id), int(token)),
                )
            return int(cursor.rowcount or 0) > 0

        return bool(self._run_with_busy_retry(_op))

    def release_maintenance(self, owner_id: str, token: int) -> bool:
        def _op() -> bool:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """
                    UPDATE resource_maintenance_lease
                    SET owner_id = '', lease_expires_at = NULL, updated_at = ?
                    WHERE scope = ? AND owner_id = ? AND fencing_token = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), self.MAINTENANCE_SCOPE, str(owner_id), int(token)),
                )
            return int(cursor.rowcount or 0) > 0

        return bool(self._run_with_busy_retry(_op))

    def maintenance_snapshot(self) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM resource_maintenance_lease WHERE scope = ?",
                (self.MAINTENANCE_SCOPE,),
            ).fetchone()
        if row is None:
            return {"owner_id": "", "fencing_token": 0, "lease_expires_at": None}
        return {
            "owner_id": str(row["owner_id"] or ""),
            "fencing_token": int(row["fencing_token"] or 0),
            "lease_expires_at": row["lease_expires_at"],
        }

    def try_acquire(
        self,
        resource: str,
        *,
        capacity: int,
        reserved_interactive: int,
        interactive: bool,
        owner_id: str,
        attempt_id: str,
    ) -> str | None:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # Another process holds the write lock: report "not admitted
                # now"; the governor's bounded loop retries until its deadline.
                if self._is_busy_error(exc):
                    return None
                raise
            try:
                self.reap_expired(conn)
                escalated = conn.execute(
                    """
                    SELECT 1 FROM resource_permits
                    WHERE resource = ? AND state = 'uncertain' AND escalated_at <> ''
                    LIMIT 1
                    """,
                    (resource,),
                ).fetchone()
                if escalated is not None:
                    # Isolation exceeded the escalation window: refuse new
                    # admissions until an evidence-backed recovery releases it.
                    conn.execute("COMMIT")
                    return None
                counts = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(weight), 0) AS active,
                        COALESCE(SUM(CASE WHEN interactive = 0 THEN weight ELSE 0 END), 0) AS background
                    FROM resource_permits
                    WHERE resource = ? AND state = 'active'
                    """,
                    (resource,),
                ).fetchone()
                active = int(counts["active"] or 0)
                background = int(counts["background"] or 0)
                background_capacity = max(0, int(capacity) - int(reserved_interactive))
                if active >= int(capacity) or (not interactive and background >= background_capacity):
                    conn.execute("COMMIT")
                    return None
                permit_id = str(uuid.uuid4())
                expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat()
                conn.execute(
                    """
                    INSERT INTO resource_permits (
                        permit_id, resource, interactive, owner_id, attempt_id, weight,
                        state, expires_at, created_at, started_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?, ?)
                    """,
                    (
                        permit_id,
                        resource,
                        1 if interactive else 0,
                        str(owner_id),
                        str(attempt_id),
                        expires_at,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                conn.execute("COMMIT")
                return permit_id
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def release(self, permit_id: str) -> None:
        def _op() -> None:
            with closing(self._connect()) as conn:
                conn.execute(
                    "UPDATE resource_permits SET state = 'released' WHERE permit_id = ? AND state IN ('active', 'uncertain')",
                    (permit_id,),
                )

        self._run_with_busy_retry(_op)

    def renew(self, permit_id: str) -> bool:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)
        ).isoformat()

        def _op() -> bool:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    "UPDATE resource_permits SET expires_at = ? WHERE permit_id = ? AND state = 'active'",
                    (expires_at, permit_id),
                )
            return int(cursor.rowcount or 0) > 0

        return bool(self._run_with_busy_retry(_op))

    def finish(
        self,
        permit_id: str,
        *,
        outcome: str = "completed",
        evidence: str = "",
        reason: str = "",
    ) -> bool:
        """Explicit end confirmation for one permit (plan §6.1.1).

        ``completed`` / ``cancelled`` release the permit and require a
        server-recognized downstream end evidence; a client-side exception is
        never evidence.  ``uncertain`` keeps the permit isolated (uncertain)
        for the maintenance loop and evidence-backed reconciliation.  Repeated
        confirmations are idempotent and return True.
        """
        outcome = str(outcome or "").strip().lower()
        if outcome not in {"completed", "cancelled", "uncertain"}:
            raise ValueError(f"unsupported finish outcome: {outcome!r}")
        if outcome != "uncertain":
            evidence = str(evidence or "").strip()
            if evidence not in self.VALID_RELEASE_EVIDENCE:
                raise ValueError(
                    "finish requires a recognized downstream end evidence; "
                    f"got {evidence or '<empty>'!r}"
                )
        now = datetime.now(timezone.utc).isoformat()

        def _op() -> bool:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM resource_permits WHERE permit_id = ?", (str(permit_id),)
                    ).fetchone()
                    if row is None:
                        conn.execute("COMMIT")
                        return False
                    state = str(row["state"])
                    if state == "released":
                        conn.execute("COMMIT")
                        return True
                    if outcome == "uncertain":
                        conn.execute(
                            """
                            UPDATE resource_permits
                            SET state = 'uncertain',
                                uncertain_since = CASE WHEN uncertain_since = '' THEN ? ELSE uncertain_since END,
                                recovery_reason = ?
                            WHERE permit_id = ? AND state IN ('active', 'uncertain')
                            """,
                            (now, str(reason)[:500], str(permit_id)),
                        )
                        action = "finish_uncertain"
                    else:
                        conn.execute(
                            """
                            UPDATE resource_permits
                            SET state = 'released', recovery_reason = ?
                            WHERE permit_id = ? AND state IN ('active', 'uncertain')
                            """,
                            (str(reason)[:500], str(permit_id)),
                        )
                        action = "finish"
                    conn.execute(
                        """
                        INSERT INTO resource_permit_audit
                            (permit_id, resource, action, evidence, reason, operator, previous_state, recorded_at)
                        VALUES (?, ?, ?, ?, ?, '', ?, ?)
                        """,
                        (
                            str(permit_id),
                            str(row["resource"]),
                            action,
                            evidence,
                            str(reason)[:500],
                            state,
                            now,
                        ),
                    )
                    conn.execute("COMMIT")
                    return True
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

        return bool(self._run_with_busy_retry(_op))

    def snapshot(self) -> dict[str, dict[str, int]]:
        with closing(self._connect()) as conn:
            self.reap_expired(conn)
            rows = conn.execute(
                """
                SELECT resource,
                       COALESCE(SUM(CASE WHEN state = 'active' THEN weight ELSE 0 END), 0) AS active,
                       COALESCE(SUM(CASE WHEN state = 'uncertain' THEN weight ELSE 0 END), 0) AS uncertain
                FROM resource_permits GROUP BY resource
                """
            ).fetchall()
        return {
            str(row["resource"]): {"active": int(row["active"] or 0), "uncertain": int(row["uncertain"] or 0)}
            for row in rows
        }

    def active_counts(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            self.reap_expired(conn)
            rows = conn.execute(
                "SELECT resource, COALESCE(SUM(weight), 0) AS active FROM resource_permits WHERE state = 'active' GROUP BY resource"
            ).fetchall()
        return {str(row["resource"]): int(row["active"] or 0) for row in rows}


class PostgresQuotaStore:
    """Cross-host deployment quota store (PostgreSQL, G3).

    Same contract as :class:`SqliteQuotaStore` (leases, uncertain isolation,
    audited reconciliation, maintenance election), but shared by API/Worker
    replicas on different hosts.  Short transactions and one advisory
    transaction lock per resource keep admissions race-free without long
    table locks; a crashed process leaves ``uncertain`` rows instead of
    silently freeing capacity.
    """

    SCHEMA_VERSION = 1
    VALID_RELEASE_EVIDENCE = SqliteQuotaStore.VALID_RELEASE_EVIDENCE
    MAINTENANCE_SCOPE = SqliteQuotaStore.MAINTENANCE_SCOPE
    _CONNECT_OPTIONS = "-c lock_timeout=2000 -c statement_timeout=30000"

    def __init__(
        self,
        dsn: str,
        *,
        lease_seconds: int = 300,
        deployment_id: str | None = None,
        config_revision: str = "",
        require_preconfigured: bool = False,
    ) -> None:
        if psycopg is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL quota store requires psycopg[binary]")
        self.dsn = str(dsn)
        self.lease_seconds = max(30, int(lease_seconds))
        self.deployment_id = str(deployment_id or "").strip() or "local"
        self.config_revision = str(config_revision or "")
        self._require_preconfigured = bool(require_preconfigured)
        # Multiple replicas run this schema/identity init concurrently.
        self._run_with_retry(self._init_once, deadline_seconds=10.0)

    # -- connection helpers -------------------------------------------------

    def _connect(self):
        return psycopg.connect(
            self.dsn, options=self._CONNECT_OPTIONS, row_factory=dict_row
        )

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            with conn.transaction():
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        if psycopg is None:  # pragma: no cover
            return False
        return isinstance(
            exc,
            (
                psycopg.errors.SerializationFailure,
                psycopg.errors.DeadlockDetected,
                psycopg.errors.LockNotAvailable,
            ),
        )

    def _run_with_retry(self, operation, *, deadline_seconds: float = 2.0):
        """Bounded retry for transient PostgreSQL contention errors."""
        deadline = time.monotonic() + max(0.0, float(deadline_seconds))
        while True:
            try:
                return operation()
            except Exception as exc:
                if not self._is_retryable(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    @staticmethod
    def _advisory_lock(conn, scope: str) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(scope),)
        )

    # -- schema / identity --------------------------------------------------

    def _init_once(self) -> None:
        with self._transaction() as conn:
            self._advisory_lock(conn, "resource-governor:init")
            self._init_schema(conn)
            self._check_identity(conn, require_preconfigured=self._require_preconfigured)

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_permits (
                permit_id TEXT PRIMARY KEY,
                resource TEXT NOT NULL,
                interactive BOOLEAN NOT NULL DEFAULT false,
                owner_id TEXT NOT NULL DEFAULT '',
                attempt_id TEXT NOT NULL DEFAULT '',
                weight INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'active',
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                started_at TIMESTAMPTZ,
                hard_deadline_at TIMESTAMPTZ,
                provider_request_id TEXT NOT NULL DEFAULT '',
                uncertain_since TIMESTAMPTZ,
                escalated_at TIMESTAMPTZ,
                recovery_reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_permits_state"
            " ON resource_permits(resource, state)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_permit_audit (
                audit_id BIGSERIAL PRIMARY KEY,
                permit_id TEXT NOT NULL,
                resource TEXT NOT NULL,
                action TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                operator TEXT NOT NULL DEFAULT '',
                previous_state TEXT NOT NULL DEFAULT '',
                recorded_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_governor_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_maintenance_lease (
                scope TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT '',
                fencing_token BIGINT NOT NULL DEFAULT 0,
                lease_expires_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_quotas (
                resource TEXT PRIMARY KEY,
                capacity INTEGER NOT NULL,
                reserved_interactive INTEGER NOT NULL DEFAULT 0,
                config_revision TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )

    def _check_identity(self, conn, *, require_preconfigured: bool) -> None:
        rows = {
            str(row["key"]): str(row["value"])
            for row in conn.execute(
                "SELECT key, value FROM resource_governor_meta"
            ).fetchall()
        }
        if not rows:
            if require_preconfigured:
                raise RuntimeError(
                    "quota database has no identity rows; provision it first"
                )
            for key, value in (
                ("deployment_id", self.deployment_id),
                ("schema_version", str(self.SCHEMA_VERSION)),
                ("config_revision", self.config_revision),
            ):
                conn.execute(
                    "INSERT INTO resource_governor_meta (key, value) VALUES (%s, %s)",
                    (key, value),
                )
            return
        stored_deployment = rows.get("deployment_id", "")
        if stored_deployment != self.deployment_id:
            raise RuntimeError(
                f"quota database deployment_id mismatch: stored={stored_deployment!r} "
                f"process={self.deployment_id!r}"
            )
        stored_version = rows.get("schema_version", "")
        if stored_version != str(self.SCHEMA_VERSION):
            raise RuntimeError(
                f"quota database schema_version mismatch: stored={stored_version!r} "
                f"code={self.SCHEMA_VERSION!r}"
            )

    # -- quotas -------------------------------------------------------------

    def ensure_quotas(self, defaults: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
        now = datetime.now(timezone.utc)
        with self._transaction() as conn:
            for resource, (capacity, reserved) in defaults.items():
                conn.execute(
                    """
                    INSERT INTO resource_quotas
                        (resource, capacity, reserved_interactive, config_revision, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (resource) DO NOTHING
                    """,
                    (
                        str(resource),
                        max(1, int(capacity)),
                        max(0, int(reserved)),
                        self.config_revision,
                        now,
                    ),
                )
            rows = conn.execute(
                "SELECT resource, capacity, reserved_interactive FROM resource_quotas"
            ).fetchall()
        return {
            str(row["resource"]): (
                int(row["capacity"]),
                int(row["reserved_interactive"]),
            )
            for row in rows
        }

    def update_quota(
        self, resource: str, *, capacity: int, reserved_interactive: int, operator: str = ""
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO resource_quotas
                    (resource, capacity, reserved_interactive, config_revision, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (resource) DO UPDATE SET
                    capacity = EXCLUDED.capacity,
                    reserved_interactive = EXCLUDED.reserved_interactive,
                    config_revision = EXCLUDED.config_revision,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(resource),
                    max(1, int(capacity)),
                    max(0, int(reserved_interactive)),
                    self.config_revision,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO resource_permit_audit
                    (permit_id, resource, action, evidence, reason, operator, previous_state, recorded_at)
                VALUES ('', %s, 'quota_update', 'config_revision', %s, %s, '', %s)
                """,
                (
                    str(resource),
                    f"capacity={capacity} reserved={reserved_interactive}",
                    str(operator),
                    now,
                ),
            )

    # -- permits ------------------------------------------------------------

    def reap_expired(self, conn) -> int:
        cursor = conn.execute(
            """
            UPDATE resource_permits
            SET state = 'uncertain',
                uncertain_since = COALESCE(uncertain_since, now())
            WHERE state = 'active' AND expires_at < now()
            """
        )
        return int(cursor.rowcount or 0)

    def maintain(self, escalate_seconds: int) -> dict[str, int]:
        threshold = max(60, int(escalate_seconds))
        with self._transaction() as conn:
            self._advisory_lock(conn, "resource-governor:maintain")
            reaped = self.reap_expired(conn)
            cursor = conn.execute(
                """
                UPDATE resource_permits
                SET escalated_at = now()
                WHERE state = 'uncertain' AND escalated_at IS NULL
                  AND uncertain_since IS NOT NULL
                  AND uncertain_since < now() - make_interval(secs => %s)
                """,
                (threshold,),
            )
            escalated = int(cursor.rowcount or 0)
        if escalated:
            SqliteQuotaStore._record_escalation(escalated)
        if reaped or escalated:
            SqliteQuotaStore._publish_uncertain_gauge(self)
        return {"reaped": reaped, "escalated": escalated}

    def escalated_resources(self) -> set[str]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT resource FROM resource_permits"
                " WHERE state = 'uncertain' AND escalated_at IS NOT NULL"
            ).fetchall()
        return {str(row["resource"]) for row in rows}

    def uncertain_permits(self, resource: str | None = None) -> list[dict]:
        query = "SELECT * FROM resource_permits WHERE state = 'uncertain'"
        params: list[object] = []
        if resource:
            query += " AND resource = %s"
            params.append(str(resource))
        query += " ORDER BY uncertain_since NULLS LAST, permit_id"
        with self._transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def release_uncertain(
        self,
        permit_id: str,
        *,
        evidence: str,
        reason: str = "",
        operator: str = "",
    ) -> bool:
        evidence = str(evidence or "").strip()
        if evidence not in self.VALID_RELEASE_EVIDENCE:
            raise ValueError(
                "release requires a recognized downstream end evidence; "
                f"got {evidence or '<empty>'!r}"
            )
        now = datetime.now(timezone.utc)

        def _op() -> bool:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM resource_permits WHERE permit_id = %s FOR UPDATE",
                    (str(permit_id),),
                ).fetchone()
                if row is None or str(row["state"]) != "uncertain":
                    return False
                cursor = conn.execute(
                    """
                    UPDATE resource_permits
                    SET state = 'released', recovery_reason = %s
                    WHERE permit_id = %s AND state = 'uncertain'
                    """,
                    (str(reason)[:500], str(permit_id)),
                )
                if int(cursor.rowcount or 0) == 0:
                    return False
                conn.execute(
                    """
                    INSERT INTO resource_permit_audit
                        (permit_id, resource, action, evidence, reason, operator, previous_state, recorded_at)
                    VALUES (%s, %s, 'release_uncertain', %s, %s, %s, %s, %s)
                    """,
                    (
                        str(permit_id),
                        str(row["resource"]),
                        evidence,
                        str(reason)[:500],
                        str(operator),
                        "uncertain",
                        now,
                    ),
                )
                return True

        return bool(self._run_with_retry(_op))

    def audit_entries(self, permit_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM resource_permit_audit"
        params: list[object] = []
        if permit_id:
            query += " WHERE permit_id = %s"
            params.append(str(permit_id))
        query += " ORDER BY audit_id"
        with self._transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # -- maintenance election ----------------------------------------------

    def try_acquire_maintenance(self, owner_id: str, *, lease_seconds: int = 15) -> int | None:
        owner = str(owner_id or "")
        lease = max(1, int(lease_seconds))

        def _op() -> int | None:
            with self._transaction() as conn:
                row = conn.execute(
                    """
                    SELECT *, (lease_expires_at IS NOT NULL AND lease_expires_at > now())
                        AS lease_active
                    FROM resource_maintenance_lease WHERE scope = %s FOR UPDATE
                    """,
                    (self.MAINTENANCE_SCOPE,),
                ).fetchone()
                if row is not None and str(row["owner_id"] or "") == owner:
                    conn.execute(
                        """
                        UPDATE resource_maintenance_lease
                        SET lease_expires_at = now() + make_interval(secs => %s),
                            updated_at = now()
                        WHERE scope = %s
                        """,
                        (lease, self.MAINTENANCE_SCOPE),
                    )
                    return int(row["fencing_token"] or 0)
                if row is not None and bool(row["lease_active"]):
                    return None
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO resource_maintenance_lease
                            (scope, owner_id, fencing_token, lease_expires_at, updated_at)
                        VALUES (%s, %s, 1, now() + make_interval(secs => %s), now())
                        """,
                        (self.MAINTENANCE_SCOPE, owner, lease),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE resource_maintenance_lease
                        SET owner_id = %s,
                            fencing_token = fencing_token + 1,
                            lease_expires_at = now() + make_interval(secs => %s),
                            updated_at = now()
                        WHERE scope = %s
                        """,
                        (owner, lease, self.MAINTENANCE_SCOPE),
                    )
                current = conn.execute(
                    "SELECT fencing_token FROM resource_maintenance_lease WHERE scope = %s",
                    (self.MAINTENANCE_SCOPE,),
                ).fetchone()
                return int(current["fencing_token"] or 0) if current else None

        return self._run_with_retry(_op)

    def renew_maintenance(self, owner_id: str, token: int, *, lease_seconds: int = 15) -> bool:
        lease = max(1, int(lease_seconds))

        def _op() -> bool:
            with self._transaction() as conn:
                cursor = conn.execute(
                    """
                    UPDATE resource_maintenance_lease
                    SET lease_expires_at = now() + make_interval(secs => %s),
                        updated_at = now()
                    WHERE scope = %s AND owner_id = %s AND fencing_token = %s
                    """,
                    (
                        lease,
                        self.MAINTENANCE_SCOPE,
                        str(owner_id),
                        int(token),
                    ),
                )
                return int(cursor.rowcount or 0) > 0

        return bool(self._run_with_retry(_op))

    def release_maintenance(self, owner_id: str, token: int) -> bool:
        def _op() -> bool:
            with self._transaction() as conn:
                cursor = conn.execute(
                    """
                    UPDATE resource_maintenance_lease
                    SET owner_id = '', lease_expires_at = NULL, updated_at = now()
                    WHERE scope = %s AND owner_id = %s AND fencing_token = %s
                    """,
                    (
                        self.MAINTENANCE_SCOPE,
                        str(owner_id),
                        int(token),
                    ),
                )
                return int(cursor.rowcount or 0) > 0

        return bool(self._run_with_retry(_op))

    def maintenance_snapshot(self) -> dict:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM resource_maintenance_lease WHERE scope = %s",
                (self.MAINTENANCE_SCOPE,),
            ).fetchone()
        if row is None:
            return {"owner_id": "", "fencing_token": 0, "lease_expires_at": None}
        return {
            "owner_id": str(row["owner_id"] or ""),
            "fencing_token": int(row["fencing_token"] or 0),
            "lease_expires_at": row["lease_expires_at"],
        }

    # -- admission ----------------------------------------------------------

    def try_acquire(
        self,
        resource: str,
        *,
        capacity: int,
        reserved_interactive: int,
        interactive: bool,
        owner_id: str,
        attempt_id: str,
    ) -> str | None:
        permit_id = str(uuid.uuid4())

        def _op() -> str | None:
            conn = self._connect()
            try:
                with conn.transaction():
                    # Waiting inside the resource lock is fine for correctness
                    # but the caller's deadline is handled by the governor
                    # loop; a short lock timeout reports "not admitted now".
                    conn.execute("SET LOCAL lock_timeout = '1000ms'")
                    try:
                        self._advisory_lock(conn, f"resource:{resource}")
                    except psycopg.errors.LockNotAvailable:
                        return None
                    self.reap_expired(conn)
                    escalated = conn.execute(
                        """
                        SELECT 1 FROM resource_permits
                        WHERE resource = %s AND state = 'uncertain' AND escalated_at IS NOT NULL
                        LIMIT 1
                        """,
                        (resource,),
                    ).fetchone()
                    if escalated is not None:
                        return None
                    counts = conn.execute(
                        """
                        SELECT
                            COALESCE(SUM(weight), 0) AS active,
                            COALESCE(SUM(CASE WHEN interactive = false THEN weight ELSE 0 END), 0) AS background
                        FROM resource_permits
                        WHERE resource = %s AND state = 'active'
                        """,
                        (resource,),
                    ).fetchone()
                    active = int(counts["active"] or 0)
                    background = int(counts["background"] or 0)
                    background_capacity = max(0, int(capacity) - int(reserved_interactive))
                    if active >= int(capacity) or (
                        not interactive and background >= background_capacity
                    ):
                        return None
                    conn.execute(
                        """
                        INSERT INTO resource_permits (
                            permit_id, resource, interactive, owner_id, attempt_id, weight,
                            state, expires_at, created_at, started_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, 1, 'active',
                            now() + make_interval(secs => %s), now(), now()
                        )
                        """,
                        (
                            permit_id,
                            resource,
                            bool(interactive),
                            str(owner_id),
                            str(attempt_id),
                            self.lease_seconds,
                        ),
                    )
                    return permit_id
            finally:
                conn.close()

        return self._run_with_retry(_op)

    def release(self, permit_id: str) -> None:
        def _op() -> None:
            with self._transaction() as conn:
                conn.execute(
                    """
                    UPDATE resource_permits
                    SET state = 'released'
                    WHERE permit_id = %s AND state IN ('active', 'uncertain')
                    """,
                    (permit_id,),
                )

        self._run_with_retry(_op)

    def renew(self, permit_id: str) -> bool:
        def _op() -> bool:
            with self._transaction() as conn:
                cursor = conn.execute(
                    """
                    UPDATE resource_permits
                    SET expires_at = now() + make_interval(secs => %s)
                    WHERE permit_id = %s AND state = 'active'
                    """,
                    (self.lease_seconds, permit_id),
                )
                return int(cursor.rowcount or 0) > 0

        return bool(self._run_with_retry(_op))

    def finish(
        self,
        permit_id: str,
        *,
        outcome: str = "completed",
        evidence: str = "",
        reason: str = "",
    ) -> bool:
        outcome = str(outcome or "").strip().lower()
        if outcome not in {"completed", "cancelled", "uncertain"}:
            raise ValueError(f"unsupported finish outcome: {outcome!r}")
        if outcome != "uncertain":
            evidence = str(evidence or "").strip()
            if evidence not in self.VALID_RELEASE_EVIDENCE:
                raise ValueError(
                    "finish requires a recognized downstream end evidence; "
                    f"got {evidence or '<empty>'!r}"
                )
        now = datetime.now(timezone.utc)

        def _op() -> bool:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM resource_permits WHERE permit_id = %s FOR UPDATE",
                    (str(permit_id),),
                ).fetchone()
                if row is None:
                    return False
                state = str(row["state"])
                if state == "released":
                    return True
                if outcome == "uncertain":
                    conn.execute(
                        """
                        UPDATE resource_permits
                        SET state = 'uncertain',
                            uncertain_since = COALESCE(uncertain_since, %s),
                            recovery_reason = %s
                        WHERE permit_id = %s AND state IN ('active', 'uncertain')
                        """,
                        (now, str(reason)[:500], str(permit_id)),
                    )
                    action = "finish_uncertain"
                else:
                    conn.execute(
                        """
                        UPDATE resource_permits
                        SET state = 'released', recovery_reason = %s
                        WHERE permit_id = %s AND state IN ('active', 'uncertain')
                        """,
                        (str(reason)[:500], str(permit_id)),
                    )
                    action = "finish"
                conn.execute(
                    """
                    INSERT INTO resource_permit_audit
                        (permit_id, resource, action, evidence, reason, operator, previous_state, recorded_at)
                    VALUES (%s, %s, %s, %s, %s, '', %s, %s)
                    """,
                    (
                        str(permit_id),
                        str(row["resource"]),
                        action,
                        evidence,
                        str(reason)[:500],
                        state,
                        now,
                    ),
                )
                return True

        return bool(self._run_with_retry(_op))

    # -- observability ------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._transaction() as conn:
            self.reap_expired(conn)
            rows = conn.execute(
                """
                SELECT resource,
                       COALESCE(SUM(CASE WHEN state = 'active' THEN weight ELSE 0 END), 0) AS active,
                       COALESCE(SUM(CASE WHEN state = 'uncertain' THEN weight ELSE 0 END), 0) AS uncertain
                FROM resource_permits GROUP BY resource
                """
            ).fetchall()
        return {
            str(row["resource"]): {
                "active": int(row["active"] or 0),
                "uncertain": int(row["uncertain"] or 0),
            }
            for row in rows
        }

    def active_counts(self) -> dict[str, int]:
        with self._transaction() as conn:
            self.reap_expired(conn)
            rows = conn.execute(
                """
                SELECT resource, COALESCE(SUM(weight), 0) AS active
                FROM resource_permits WHERE state = 'active' GROUP BY resource
                """
            ).fetchall()
        return {str(row["resource"]): int(row["active"] or 0) for row in rows}


class ResourceGovernor:
    def __init__(
        self,
        *,
        scope: str = "process",
        limits: dict[str, tuple[int, int]] | None = None,
        store: SqliteQuotaStore | PostgresQuotaStore | None = None,
        queue_max_wait_seconds: float = 30.0,
        queue_max_depth: int = 256,
    ) -> None:
        self.scope = "deployment" if str(scope).lower() == "deployment" else "process"
        self.limits = dict(limits or DEFAULT_LIMITS)
        self.store = store
        if self.scope == "deployment" and self.store is not None:
            # Stored, versioned quotas are authoritative: per-worker env values
            # must never multiply the deployment budget (plan §6.1.1).
            stored = self.store.ensure_quotas(self.limits)
            self.limits.update(stored)
        self.queue_max_wait_seconds = max(0.05, float(queue_max_wait_seconds))
        self.queue_max_depth = max(1, int(queue_max_depth))
        self._lock = threading.Lock()
        self._total: dict[str, threading.BoundedSemaphore] = {}
        self._background: dict[str, threading.BoundedSemaphore] = {}
        self._waiters: dict[str, int] = {}
        self._active: dict[str, int] = {}
        # In-process cancellation handles for queued acquisitions; cross-process
        # waiters are bounded by their deadline (documented in entry-inventory).
        self._wait_cancels: dict[str, threading.Event] = {}
        for resource in self.limits:
            self._ensure_process_locks(resource)

    # -- configuration ------------------------------------------------------

    def _ensure_process_locks(self, resource: str) -> None:
        if resource in self._total:
            return
        capacity, reserved = self.limits.get(resource, (1, 0))
        self._total[resource] = threading.BoundedSemaphore(max(1, int(capacity)))
        self._background[resource] = threading.BoundedSemaphore(max(0, int(capacity) - int(reserved)))
        self._waiters.setdefault(resource, 0)
        self._active.setdefault(resource, 0)

    def limits_for(self, resource: str) -> tuple[int, int]:
        return self.limits.get(resource, DEFAULT_LIMITS.get(resource, (1, 0)))

    # -- acquisition --------------------------------------------------------

    def acquire(
        self,
        resource: str,
        *,
        interactive: bool = False,
        timeout: float | None = None,
        owner_id: str = "",
        attempt_id: str = "",
        request_id: str = "",
    ) -> GovernorPermit:
        resource = str(resource).strip().lower()
        wait_budget = self.queue_max_wait_seconds if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait_budget
        started = time.monotonic()
        cancel_event = None
        if request_id:
            cancel_event = threading.Event()
            with self._lock:
                self._wait_cancels[str(request_id)] = cancel_event
        try:
            if self.scope == "deployment" and self.store is not None:
                permit = self._acquire_deployment(
                    resource,
                    interactive=interactive,
                    deadline=deadline,
                    owner_id=owner_id,
                    attempt_id=attempt_id,
                    cancel_event=cancel_event,
                )
            else:
                permit = self._acquire_process(
                    resource, interactive=interactive, deadline=deadline, cancel_event=cancel_event
                )
        finally:
            if request_id:
                with self._lock:
                    self._wait_cancels.pop(str(request_id), None)
        self._record_wait(resource, time.monotonic() - started, status="granted")
        return permit

    def cancel_wait(self, request_id: str) -> bool:
        """Cancel a queued (not yet admitted) acquisition in this process.

        Cross-process waiters are not addressable without persistent queue
        rows; they are bounded by their admission deadline instead.
        """
        with self._lock:
            event = self._wait_cancels.get(str(request_id or ""))
        if event is None:
            return False
        event.set()
        return True

    @staticmethod
    def _check_wait_cancelled(
        resource: str, cancel_event: threading.Event | None
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise GovernorRejected(resource, "cancelled")

    def _acquire_process(
        self,
        resource: str,
        *,
        interactive: bool,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> GovernorPermit:
        self._ensure_process_locks(resource)
        with self._lock:
            if self._waiters[resource] >= self.queue_max_depth:
                self._record_wait(resource, 0.0, status="rejected")
                raise GovernorRejected(resource, "queue_full")
            self._waiters[resource] += 1
        acquired_background = False
        acquired_total = False
        try:
            self._check_wait_cancelled(resource, cancel_event)
            if not interactive:
                acquired_background = self._acquire_semaphore(
                    self._background[resource], resource, deadline, cancel_event
                )
                if not acquired_background:
                    raise GovernorRejected(resource, "deadline")
            acquired_total = self._acquire_semaphore(
                self._total[resource], resource, deadline, cancel_event
            )
            if not acquired_total:
                raise GovernorRejected(resource, "deadline")
            with self._lock:
                self._active[resource] = self._active.get(resource, 0) + 1
            return GovernorPermit(self, str(uuid.uuid4()), resource, interactive, deadline)
        except GovernorRejected:
            if acquired_background:
                self._background[resource].release()
            raise
        except Exception:
            if acquired_total:
                self._total[resource].release()
            if acquired_background:
                self._background[resource].release()
            raise
        finally:
            with self._lock:
                self._waiters[resource] = max(0, self._waiters.get(resource, 0) - 1)
                self._publish_state()

    def _acquire_semaphore(
        self,
        semaphore: threading.BoundedSemaphore,
        resource: str,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> bool:
        """Bounded, cancellable semaphore wait; False on deadline."""
        while True:
            self._check_wait_cancelled(resource, cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if semaphore.acquire(timeout=min(0.1, remaining)):
                return True

    def _acquire_deployment(
        self,
        resource: str,
        *,
        interactive: bool,
        deadline: float,
        owner_id: str,
        attempt_id: str,
        cancel_event: threading.Event | None = None,
    ) -> GovernorPermit:
        capacity, reserved = self.limits_for(resource)
        if resource in self.store.escalated_resources():
            self._record_wait(resource, 0.0, status="rejected")
            raise GovernorRejected(resource, "uncertain_escalated")
        while True:
            self._check_wait_cancelled(resource, cancel_event)
            permit_id = self.store.try_acquire(
                resource,
                capacity=capacity,
                reserved_interactive=reserved,
                interactive=interactive,
                owner_id=owner_id,
                attempt_id=attempt_id,
            )
            if permit_id is not None:
                return GovernorPermit(self, permit_id, resource, interactive, deadline)
            if time.monotonic() >= deadline:
                self._record_wait(resource, 0.0, status="rejected")
                raise GovernorRejected(resource, "deadline")
            time.sleep(0.05)

    def _release(self, permit: GovernorPermit) -> None:
        resource = permit.resource
        if self.scope == "deployment" and self.store is not None:
            self.store.release(permit.permit_id)
        else:
            self._ensure_process_locks(resource)
            with self._lock:
                self._active[resource] = max(0, self._active.get(resource, 0) - 1)
                self._publish_state()
            # Release in reverse acquisition order: total first, then the
            # background limiter, so a waiting interactive caller can use the
            # slot immediately.
            self._total[resource].release()
            if not permit.interactive:
                try:
                    self._background[resource].release()
                except ValueError:
                    pass

    def _renew(self, permit: GovernorPermit, *, lease_seconds: int | None = None) -> bool:
        if self.scope == "deployment" and self.store is not None:
            return self.store.renew(permit.permit_id)
        return True

    def _finish(
        self,
        permit: GovernorPermit,
        *,
        outcome: str = "completed",
        evidence: str = "",
        reason: str = "",
    ) -> bool:
        if self.scope == "deployment" and self.store is not None:
            return self.store.finish(
                permit.permit_id, outcome=outcome, evidence=evidence, reason=reason
            )
        # Process permits are not persisted: a confirmed end releases the slot;
        # uncertain has no meaning without a lease row.
        if str(outcome).lower() == "uncertain":
            return False
        permit.release()
        return True

    def _reconcile(
        self,
        permit_id: str,
        *,
        evidence: str,
        reason: str = "",
        operator: str = "",
    ) -> bool:
        if self.scope == "deployment" and self.store is not None:
            return self.store.release_uncertain(
                permit_id, evidence=evidence, reason=reason, operator=operator
            )
        return False

    def reconcile(
        self,
        permit_id: str,
        *,
        evidence: str,
        reason: str = "",
        operator: str = "",
    ) -> bool:
        """Operational alias for evidence-backed release of an uncertain permit."""
        return self._reconcile(permit_id, evidence=evidence, reason=reason, operator=operator)

    # -- observability ------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Per-resource live counts (deployment store or process counters)."""
        if self.scope == "deployment" and self.store is not None:
            return self.store.snapshot()
        with self._lock:
            active = dict(self._active)
            waiting = dict(self._waiters)
        return {
            key: {
                "active": int(active.get(key, 0)),
                "waiting": int(waiting.get(key, 0)),
            }
            for key in set(active) | set(waiting)
        }

    def _publish_state(self) -> None:
        try:
            from src.observability.metrics import set_resource_state

            active = dict(self._active)
            uncertain: dict[str, int] = {}
            if self.scope == "deployment" and self.store is not None:
                try:
                    snapshot = self.store.snapshot()
                    active = {key: value.get("active", 0) for key, value in snapshot.items()}
                    uncertain = {key: value.get("uncertain", 0) for key, value in snapshot.items()}
                except Exception:
                    pass
            set_resource_state(
                active=active,
                waiting=dict(self._waiters),
                capacities={key: value[0] for key, value in self.limits.items()},
                reserved={key: value[1] for key, value in self.limits.items()},
                uncertain=uncertain,
            )
        except Exception:
            pass

    @staticmethod
    def _record_wait(resource: str, seconds: float, *, status: str) -> None:
        try:
            from src.observability.metrics import histogram, counter

            histogram(
                "hdb.resource.queue.wait",
                seconds,
                attributes={"resource": resource, "status": status},
                description="Time spent waiting for a resource permit",
            )
            if status == "rejected":
                counter(
                    "hdb.resource.rejected",
                    attributes={"resource": resource, "reason": "queue_full_or_deadline"},
                    description="Resource admissions rejected",
                )
        except Exception:
            pass


_GOVERNOR: ResourceGovernor | None = None
_GOVERNOR_LOCK = threading.Lock()


def _limits_from_settings() -> dict[str, tuple[int, int]]:
    import src.settings as settings

    def _pair(capacity_key: str, reserved_key: str, default: tuple[int, int]) -> tuple[int, int]:
        capacity = int(getattr(settings, capacity_key, default[0]))
        reserved = int(getattr(settings, reserved_key, default[1]))
        return max(1, capacity), max(0, min(reserved, capacity))

    return {
        "llm": _pair("RESOURCE_LLM_MAX_CONCURRENT", "RESOURCE_LLM_RESERVED_INTERACTIVE", DEFAULT_LIMITS["llm"]),
        "rag": _pair("RESOURCE_RAG_MAX_CONCURRENT", "RESOURCE_RAG_RESERVED_INTERACTIVE", DEFAULT_LIMITS["rag"]),
        "tool": _pair("RESOURCE_TOOL_MAX_CONCURRENT", "RESOURCE_TOOL_RESERVED_INTERACTIVE", DEFAULT_LIMITS["tool"]),
        "chat": _pair("RESOURCE_CHAT_MAX_CONCURRENT", "RESOURCE_CHAT_RESERVED_INTERACTIVE", DEFAULT_LIMITS["chat"]),
        "heavy_job": _pair("RESOURCE_HEAVY_JOB_MAX_CONCURRENT", "RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE", DEFAULT_LIMITS["heavy_job"]),
        "ocr": _pair("RESOURCE_OCR_MAX_CONCURRENT", "RESOURCE_OCR_RESERVED_INTERACTIVE", DEFAULT_LIMITS["ocr"]),
        "export": _pair("RESOURCE_EXPORT_MAX_CONCURRENT", "RESOURCE_EXPORT_RESERVED_INTERACTIVE", DEFAULT_LIMITS["export"]),
        "memory": _pair("RESOURCE_MEMORY_MAX_CONCURRENT", "RESOURCE_MEMORY_RESERVED_INTERACTIVE", DEFAULT_LIMITS["memory"]),
        "mcp": _pair("RESOURCE_MCP_MAX_CONCURRENT", "RESOURCE_MCP_RESERVED_INTERACTIVE", DEFAULT_LIMITS["mcp"]),
    }


def _config_revision(limits: dict[str, tuple[int, int]]) -> str:
    import hashlib
    import json

    payload = json.dumps(
        {key: [int(value[0]), int(value[1])] for key, value in sorted(limits.items())},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get_governor() -> ResourceGovernor:
    global _GOVERNOR
    with _GOVERNOR_LOCK:
        if _GOVERNOR is None:
            import src.settings as settings

            limits = _limits_from_settings()
            scope = str(getattr(settings, "RESOURCE_GOVERNOR_SCOPE", "process") or "process").lower()
            store: SqliteQuotaStore | PostgresQuotaStore | None = None
            if scope == "deployment":
                lease_seconds = int(getattr(settings, "RESOURCE_PERMIT_LEASE_SECONDS", 300))
                deployment_id = str(getattr(settings, "RESOURCE_GOVERNOR_DEPLOYMENT_ID", "local"))
                require_preconfigured = bool(
                    getattr(settings, "RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED", False)
                )
                backend = str(
                    getattr(settings, "RESOURCE_GOVERNOR_BACKEND", "sqlite") or "sqlite"
                ).lower()
                if backend == "postgres":
                    database_url = str(
                        getattr(settings, "RESOURCE_GOVERNOR_DATABASE_URL", "") or ""
                    ).strip()
                    if not database_url:
                        raise RuntimeError(
                            "RESOURCE_GOVERNOR_BACKEND=postgres requires "
                            "RESOURCE_GOVERNOR_DATABASE_URL"
                        )
                    store = PostgresQuotaStore(
                        database_url,
                        lease_seconds=lease_seconds,
                        deployment_id=deployment_id,
                        config_revision=_config_revision(limits),
                        require_preconfigured=require_preconfigured,
                    )
                else:
                    store = SqliteQuotaStore(
                        str(getattr(settings, "RESOURCE_GOVERNOR_DB_PATH", "storage/resource_quotas.db")),
                        lease_seconds=lease_seconds,
                        deployment_id=deployment_id,
                        config_revision=_config_revision(limits),
                        require_preconfigured=require_preconfigured,
                    )
            _GOVERNOR = ResourceGovernor(
                scope=scope,
                limits=limits,
                store=store,
                queue_max_wait_seconds=float(getattr(settings, "RESOURCE_QUEUE_MAX_WAIT_SECONDS", 30)),
                queue_max_depth=int(getattr(settings, "RESOURCE_QUEUE_MAX_DEPTH", 256)),
            )
        return _GOVERNOR


def reset_governor() -> None:
    global _GOVERNOR
    with _GOVERNOR_LOCK:
        _GOVERNOR = None


_MAINTENANCE_THREAD: threading.Thread | None = None
_MAINTENANCE_STOP = threading.Event()


def start_governor_maintenance() -> threading.Thread | None:
    """Start the once-per-process uncertain-permit maintenance loop (P0-006).

    Only deployment scope has isolated permits to reconcile; the loop reaps
    expired leases, escalates long-isolated resources and refreshes gauges.
    """
    global _MAINTENANCE_THREAD
    import src.settings as settings

    if str(getattr(settings, "RESOURCE_GOVERNOR_SCOPE", "process")).lower() != "deployment":
        return None
    if not bool(getattr(settings, "RESOURCE_GOVERNOR_ENABLED", False)):
        return None
    # Build the governor BEFORE taking ``_GOVERNOR_LOCK``: ``get_governor()``
    # acquires the same non-reentrant lock and would self-deadlock here.
    governor = get_governor()
    if governor.store is None:
        return None
    with _GOVERNOR_LOCK:
        if _MAINTENANCE_THREAD is not None and _MAINTENANCE_THREAD.is_alive():
            return _MAINTENANCE_THREAD
        escalate = int(getattr(settings, "RESOURCE_UNCERTAIN_ESCALATE_SECONDS", 900))
        interval = max(1.0, float(getattr(settings, "RESOURCE_GOVERNOR_MAINTENANCE_SECONDS", 5)))
        _MAINTENANCE_STOP.clear()
        owner = f"governor-maint-{os.getpid()}"

        def _loop() -> None:
            token: int | None = None
            while not _MAINTENANCE_STOP.wait(interval):
                try:
                    # Multi-instance election (plan §6.3.1): only the lease
                    # holder scans; takeover bumps the fencing token.
                    if token is None:
                        token = governor.store.try_acquire_maintenance(
                            owner, lease_seconds=15
                        )
                        if token is None:
                            continue
                    elif not governor.store.renew_maintenance(owner, token, lease_seconds=15):
                        token = None
                        continue
                    governor.store.maintain(escalate)
                except Exception:
                    continue
            if token is not None:
                try:
                    governor.store.release_maintenance(owner, token)
                except Exception:
                    pass

        _MAINTENANCE_THREAD = threading.Thread(
            target=_loop, name="resource-governor-maintenance", daemon=True
        )
        _MAINTENANCE_THREAD.start()
        return _MAINTENANCE_THREAD


def stop_governor_maintenance() -> None:
    global _MAINTENANCE_THREAD
    _MAINTENANCE_STOP.set()
    thread = _MAINTENANCE_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    _MAINTENANCE_THREAD = None


@contextmanager
def governor_acquire(
    resource: str,
    *,
    interactive: bool = False,
    timeout: float | None = None,
    owner_id: str = "",
    attempt_id: str = "",
):
    """Context manager: yields a permit, or a no-op when governance is off."""
    import src.settings as settings

    if not bool(getattr(settings, "RESOURCE_GOVERNOR_ENABLED", True)):
        yield None
        return
    permit = get_governor().acquire(
        resource,
        interactive=interactive,
        timeout=timeout,
        owner_id=owner_id,
        attempt_id=attempt_id,
    )
    try:
        yield permit
    finally:
        permit.release()
