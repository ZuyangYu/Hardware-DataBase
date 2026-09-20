"""Single-owner selection for the attachment coordination control loop (P0-004).

The waiting-turn reconciliation sweep must not run from several parse workers
at once, and it must not be delayed by a long parse call.  This module provides:

- ``CoordinatorLeaseStore``: a ``coordinator_leases`` row lock (default lease
  15 s, renewed every 5 s, fencing token incremented on takeover) stored in the
  shared attachments index database;
- ``AttachmentCoordinatorLoop``: a candidate control loop that scans only while
  it holds the lease, plus a background thread wrapper used by the parse role.

The loop performs no parsing, file cleanup or network calls: it only runs the
conditional turn transitions, which re-check the original turn status, so a
stale holder that comes back late cannot duplicate a terminal transition.
"""

from __future__ import annotations

import sqlite3
import time
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

import src.settings

DEFAULT_SCOPE = "attachment-waiting-turns"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_active(expires_at: object, now: datetime) -> bool:
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except (TypeError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > now


class CoordinatorLeaseStore:
    """Row-level leader election in the shared attachments database."""

    def __init__(self, db_path: str | None = None, scope: str = DEFAULT_SCOPE):
        self.db_path = db_path or src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH
        self.scope = str(scope or DEFAULT_SCOPE)
        # Bounded busy retry: several parse workers may initialize the shared
        # lease table at the same moment (plan §6.1).
        deadline = time.monotonic() + 5.0
        while True:
            try:
                self._init_table()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _init_table(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coordinator_leases (
                    scope TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT '',
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    updated_at TEXT NOT NULL
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

    def try_acquire(self, owner_id: str, lease_seconds: int) -> int | None:
        """Acquire or re-enter the lease; returns the fencing token or None.

        A live lease owned by somebody else refuses the acquisition.  The
        fencing token always increases on ownership changes, so every scanner
        can prove which generation of leadership produced a scan.
        """
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM coordinator_leases WHERE scope = ?", (self.scope,)
                ).fetchone()
                if row is not None and str(row["owner_id"] or "") == str(owner_id or ""):
                    conn.execute(
                        """
                        UPDATE coordinator_leases
                        SET lease_expires_at = ?, updated_at = ?
                        WHERE scope = ?
                        """,
                        (expires, now_text, self.scope),
                    )
                    token = int(row["fencing_token"] or 0)
                elif row is not None and _lease_active(row["lease_expires_at"], now):
                    conn.execute("COMMIT")
                    return None
                else:
                    conn.execute(
                        """
                        INSERT INTO coordinator_leases
                            (scope, owner_id, fencing_token, lease_expires_at, updated_at)
                        VALUES (?, ?, COALESCE((
                            SELECT fencing_token FROM coordinator_leases WHERE scope = ?
                        ), 0) + 1, ?, ?)
                        ON CONFLICT(scope) DO UPDATE SET
                            owner_id = excluded.owner_id,
                            fencing_token = coordinator_leases.fencing_token + 1,
                            lease_expires_at = excluded.lease_expires_at,
                            updated_at = excluded.updated_at
                        """,
                        (self.scope, str(owner_id or ""), self.scope, expires, now_text),
                    )
                    current = conn.execute(
                        "SELECT fencing_token FROM coordinator_leases WHERE scope = ?",
                        (self.scope,),
                    ).fetchone()
                    token = int(current["fencing_token"] or 0) if current else None
                conn.execute("COMMIT")
                return token
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def renew(self, owner_id: str, token: int, lease_seconds: int) -> bool:
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE coordinator_leases
                SET lease_expires_at = ?, updated_at = ?
                WHERE scope = ? AND owner_id = ? AND fencing_token = ?
                """,
                (expires, _utc_now(), self.scope, str(owner_id or ""), int(token)),
            )
            return int(cursor.rowcount or 0) > 0

    def release(self, owner_id: str, token: int) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE coordinator_leases
                SET owner_id = '', lease_expires_at = NULL, updated_at = ?
                WHERE scope = ? AND owner_id = ? AND fencing_token = ?
                """,
                (_utc_now(), self.scope, str(owner_id or ""), int(token)),
            )
            return int(cursor.rowcount or 0) > 0

    def snapshot(self) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM coordinator_leases WHERE scope = ?", (self.scope,)
            ).fetchone()
        if row is None:
            return {"scope": self.scope, "owner_id": "", "fencing_token": 0, "lease_expires_at": None}
        return {
            "scope": str(row["scope"]),
            "owner_id": str(row["owner_id"] or ""),
            "fencing_token": int(row["fencing_token"] or 0),
            "lease_expires_at": row["lease_expires_at"],
        }


class AttachmentCoordinatorLoop:
    """Runs one reconciliation/expiry scan per cycle while holding the lease."""

    def __init__(
        self,
        coordinator=None,
        lease_store: CoordinatorLeaseStore | None = None,
        worker_id: str = "",
        lease_seconds: int = 15,
        renew_seconds: int = 5,
        interval_seconds: float | None = None,
    ):
        if coordinator is None:
            from src.attachments.coordinator import AttachmentTurnCoordinator

            coordinator = AttachmentTurnCoordinator()
        self.coordinator = coordinator
        self.lease_store = lease_store or CoordinatorLeaseStore()
        self.worker_id = str(worker_id or f"coordinator-{id(self)}")
        self.lease_seconds = max(5, int(lease_seconds))
        self.renew_seconds = max(1, min(int(renew_seconds), self.lease_seconds - 1))
        self.interval_seconds = float(
            interval_seconds
            if interval_seconds is not None
            else getattr(src.settings, "WAITING_TURN_TIMEOUT_SCAN_SECONDS", 5)
        )
        self._token: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def holding_lease(self) -> bool:
        return self._token is not None

    def run_once(self) -> bool:
        """Scan once when this candidate owns the coordination lease."""
        if self._token is None:
            token = self.lease_store.try_acquire(self.worker_id, self.lease_seconds)
            if token is None:
                return False
            self._token = token
        elif not self.lease_store.renew(self.worker_id, self._token, self.lease_seconds):
            # Leadership was taken over: stop scanning immediately.
            self._token = None
            return False
        did_work = False
        try:
            if self.coordinator.reconcile_waiting_turns() > 0:
                did_work = True
        except Exception:
            pass
        try:
            if self.coordinator.expire_waiting_turns() > 0:
                did_work = True
        except Exception:
            pass
        return did_work

    def run_forever(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception:
                continue

    def start(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            name="attachment-coordinator",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        if self._token is not None:
            try:
                self.lease_store.release(self.worker_id, self._token)
            except Exception:
                pass
            self._token = None
        self._thread = None

    def yield_lease(self) -> None:
        """Release leadership without stopping the loop (rollback/standby)."""
        if self._token is None:
            return
        try:
            self.lease_store.release(self.worker_id, self._token)
        except Exception:
            pass
        self._token = None
