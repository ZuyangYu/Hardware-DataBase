"""Shared evaluation ownership and fenced artifact writes.

JSONL/report artifacts retain their shared-directory contract.  The execution
ledger is PostgreSQL in production, SQLite for local deployments, and never
uses a process's thread table to decide whether another worker is alive.
"""
from __future__ import annotations

import contextvars
import hashlib
import math
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from src.storage.domain_pg import open_pg_connection, resolve_domain_backend


class EvaluationLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationClaim:
    run_id: str
    owner_id: str
    fencing_token: int


_EXECUTION = contextvars.ContextVar("evaluation_execution", default=None)
_GUARD_OWNER = contextvars.ContextVar("evaluation_write_guard_owner", default=None)


@contextmanager
def execution_context(ledger, claim):
    token = _EXECUTION.set((ledger, claim))
    try:
        yield
    finally:
        _EXECUTION.reset(token)


@contextmanager
def execution_write_guard():
    """Hold the shared ownership row across an artifact's entire publication."""
    context = _EXECUTION.get()
    owner = (os.getpid(), threading.get_ident(), context)
    if context is None or _GUARD_OWNER.get() == owner:
        yield
        return
    ledger, claim = context
    with ledger.fenced(claim):
        token = _GUARD_OWNER.set(owner)
        try:
            yield
        finally:
            _GUARD_OWNER.reset(token)


class EvaluationExecutionLedger:
    def __init__(self, root: str | Path, *, backend=None, dsn=None, lease_seconds=60.0, initialize=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope = hashlib.sha256(str(self.root).encode()).hexdigest()
        configured_backend, configured_dsn = resolve_domain_backend("EVALUATION_STORE")
        self.backend = backend or configured_backend
        self.dsn = dsn or configured_dsn
        if self.backend not in {"sqlite", "postgres"}:
            raise ValueError("evaluation backend must be sqlite or postgres")
        if self.backend == "postgres" and not self.dsn:
            raise ValueError("PostgreSQL evaluation ledger requires a DSN")
        self.path = self.root / ".execution-ledger.db"
        self.lease_seconds = float(lease_seconds)
        if not math.isfinite(self.lease_seconds) or self.lease_seconds <= 0:
            raise ValueError("evaluation lease duration must be positive")
        if initialize is None:
            initialize = self.backend == "sqlite"
        if not initialize:
            with self.transaction() as conn:
                try:
                    row = conn.execute("SELECT version FROM evaluation_execution_schema WHERE component='execution'").fetchone()
                except Exception as exc:
                    raise RuntimeError("evaluation schema missing; run python -m src.storage.bootstrap") from exc
                if row is None or int(row["version"]) != 1:
                    raise RuntimeError("unsupported evaluation execution schema version")
                # A version marker alone cannot make a half-restored DB ready.
                conn.execute("SELECT scope_id, run_id, owner_id, fencing_token, lease_until, requested "
                             "FROM evaluation_execution_owners LIMIT 0")
            return
        with self.transaction() as conn:
            if self.backend == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(1734049155)")
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_execution_schema (
                component TEXT PRIMARY KEY, version BIGINT NOT NULL)""")
            conn.execute("INSERT INTO evaluation_execution_schema (component, version) VALUES ('execution', 1) "
                         "ON CONFLICT (component) DO NOTHING")
            version = conn.execute("SELECT version FROM evaluation_execution_schema WHERE component='execution'").fetchone()
            if int(version["version"]) != 1:
                raise RuntimeError("unsupported evaluation execution schema version")
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_execution_owners (
                scope_id TEXT NOT NULL, run_id TEXT NOT NULL,
                owner_id TEXT NOT NULL DEFAULT '', fencing_token BIGINT NOT NULL DEFAULT 0,
                lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                requested BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (scope_id, run_id))""")

    @contextmanager
    def transaction(self):
        if self.backend == "postgres":
            conn = open_pg_connection(self.dsn)
        else:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now(self, conn):
        sql = (
            "SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now"
            if self.backend == "postgres"
            else "SELECT (julianday('now') - 2440587.5) * 86400.0 AS now"
        )
        return float(conn.execute(sql).fetchone()["now"])

    def _row(self, conn, run_id, *, create=False):
        if create:
            conn.execute(
                "INSERT INTO evaluation_execution_owners (scope_id, run_id) VALUES (?, ?) "
                "ON CONFLICT (scope_id, run_id) DO NOTHING", (self.scope, run_id),
            )
        suffix = " FOR UPDATE" if self.backend == "postgres" else ""
        return conn.execute(
            "SELECT * FROM evaluation_execution_owners WHERE scope_id=? AND run_id=?" + suffix,
            (self.scope, run_id),
        ).fetchone()

    def enqueue(self, run_id):
        with self.transaction() as conn:
            row = self._row(conn, run_id, create=True)
            if row["owner_id"] and row["lease_until"] > self._now(conn):
                raise RuntimeError(f"evaluation run {run_id!r} is already running")
            conn.execute(
                "UPDATE evaluation_execution_owners SET requested=1 WHERE scope_id=? AND run_id=?",
                (self.scope, run_id),
            )

    def claim(self, run_id):
        with self.transaction() as conn:
            row = self._row(conn, run_id, create=True)
            now = self._now(conn)
            if row["owner_id"] and row["lease_until"] > now:
                raise RuntimeError(f"evaluation run {run_id!r} is already running")
            claim = EvaluationClaim(run_id, uuid.uuid4().hex, int(row["fencing_token"]) + 1)
            conn.execute(
                "UPDATE evaluation_execution_owners SET owner_id=?, fencing_token=?, "
                "lease_until=?, requested=1 WHERE scope_id=? AND run_id=?",
                (claim.owner_id, claim.fencing_token, now + self.lease_seconds, self.scope, run_id),
            )
            return claim

    def _matches(self, row, claim, now):
        return bool(row and row["owner_id"] == claim.owner_id
                    and row["fencing_token"] == claim.fencing_token and row["lease_until"] > now)

    def heartbeat(self, claim):
        with self.transaction() as conn:
            row = self._row(conn, claim.run_id)
            now = self._now(conn)
            if not self._matches(row, claim, now):
                return False
            conn.execute(
                "UPDATE evaluation_execution_owners SET lease_until=? WHERE scope_id=? AND run_id=?",
                (now + self.lease_seconds, self.scope, claim.run_id),
            )
            return True

    @contextmanager
    def fenced(self, claim):
        with self.transaction() as conn:
            row = self._row(conn, claim.run_id)
            if not self._matches(row, claim, self._now(conn)):
                raise EvaluationLeaseLost(f"evaluation run {claim.run_id!r} lost execution ownership")
            yield

    def release(self, claim):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE evaluation_execution_owners SET owner_id='', lease_until=0, requested=0 "
                "WHERE scope_id=? AND run_id=? AND owner_id=? AND fencing_token=?",
                (self.scope, claim.run_id, claim.owner_id, claim.fencing_token),
            )

    @contextmanager
    def idle_guard(self, run_id):
        """Serialize recovery/deletion against a concurrent claim."""
        with self.transaction() as conn:
            row = self._row(conn, run_id, create=True)
            if row["owner_id"] and row["lease_until"] > self._now(conn):
                raise RuntimeError(f"evaluation run {run_id!r} is already running")
            conn.execute(
                "UPDATE evaluation_execution_owners SET owner_id='', lease_until=0, "
                "fencing_token=fencing_token+1, requested=0 WHERE scope_id=? AND run_id=?",
                (self.scope, run_id),
            )
            yield

    def pending_ids(self):
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT run_id FROM evaluation_execution_owners WHERE scope_id=? "
                "AND requested=1 AND lease_until<=? ORDER BY run_id", (self.scope, self._now(conn)),
            ).fetchall()
            return [str(row["run_id"]) for row in rows]

    def has_live_owner(self, run_id):
        with self.transaction() as conn:
            row = self._row(conn, run_id)
            return bool(row and row["owner_id"] and row["lease_until"] > self._now(conn))
