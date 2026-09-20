"""Attempt-scoped agent checkpoint namespaces with a canonical pointer (P0a).

Design (verified by ``tests/test_checkpoint_fork_restore.py``):

- each claimed turn executes under ``<visible>::attempt-<fencing_token>``;
- a new attempt forks the latest canonical checkpoint into its namespace
  (pending writes included);
- only an attempt that still owns the session (business commit already passed
  the generation/token guard) may commit: the attempt checkpoint is copied to
  the visible thread and the monotonic pointer is CAS-updated;
- superseded attempt threads are purged, so a stale worker can only write to
  its own orphan namespace and can never move the canonical pointer backwards.

The visible ``thread_id`` never changes for users; the runner receives the
internal attempt thread.  Only the SQLite backend is supported here; the
PostgreSQL backend requires the same pointer table on PostgreSQL (documented
as remaining work before multi-instance checkpoint rollout).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .fork import copy_checkpoint_namespace, copy_latest_checkpoint

_ATTEMPT_SEPARATOR = "::attempt-"
_STORE_LOCK = threading.Lock()


def _record_metric(name: str, seconds: float, description: str) -> None:
    try:
        from src.observability.metrics import histogram

        histogram(name, max(0.0, float(seconds)), description=description)
    except Exception:
        pass


class StaleAttemptCommit(RuntimeError):
    """Raised when a superseded attempt tries to move the canonical pointer."""

    def __init__(self, visible_thread: str, token: int) -> None:
        super().__init__(f"attempt {token} for {visible_thread!r} is superseded")
        self.visible_thread = visible_thread
        self.token = token


def attempt_thread_id(visible_thread: str, fencing_token: int, attempt_id: str = "") -> str:
    """Internal thread for one attempt; legacy token 0 keeps the visible id.

    ``attempt_id`` is the server-generated execution identity recorded in the
    authoritative execution state; when present it names the namespace so audit
    rows can be traced back to one execution.
    """
    visible = str(visible_thread or "")
    token = int(fencing_token or 0)
    if token <= 0 or not visible:
        return visible
    suffix = str(attempt_id or "").strip() or str(token)
    return f"{visible}{_ATTEMPT_SEPARATOR}{suffix}"


def _config(thread_id: str, checkpoint_id: str = "", checkpoint_ns: str = "") -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": str(thread_id),
        "checkpoint_ns": str(checkpoint_ns or ""),
    }
    if checkpoint_id:
        configurable["checkpoint_id"] = str(checkpoint_id)
    return {"configurable": configurable}


class CheckpointAttemptStore:
    """Pointer/attempt bookkeeping living in the agent checkpoint database."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hdb_checkpoint_attempts (
                    visible_thread TEXT NOT NULL,
                    attempt_thread TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (visible_thread, attempt_thread)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hdb_checkpoint_pointers (
                    visible_thread TEXT PRIMARY KEY,
                    attempt_thread TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL DEFAULT '',
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def record_attempt(self, visible_thread: str, attempt_thread: str, token: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO hdb_checkpoint_attempts
                    (visible_thread, attempt_thread, fencing_token, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (visible_thread, attempt_thread, int(token), datetime.now(timezone.utc).isoformat()),
            )

    def pointer(self, visible_thread: str) -> sqlite3.Row | None:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM hdb_checkpoint_pointers WHERE visible_thread = ?",
                (visible_thread,),
            ).fetchone()

    def cas_pointer(
        self,
        visible_thread: str,
        attempt_thread: str,
        checkpoint_id: str,
        token: int,
    ) -> bool:
        """Move the pointer forward; refuses to go backwards."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT fencing_token FROM hdb_checkpoint_pointers WHERE visible_thread = ?",
                    (visible_thread,),
                ).fetchone()
                if row is not None and int(row["fencing_token"] or 0) >= int(token):
                    conn.execute("COMMIT")
                    return False
                conn.execute(
                    """
                    INSERT INTO hdb_checkpoint_pointers
                        (visible_thread, attempt_thread, checkpoint_id, fencing_token, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(visible_thread) DO UPDATE SET
                        attempt_thread = excluded.attempt_thread,
                        checkpoint_id = excluded.checkpoint_id,
                        fencing_token = excluded.fencing_token,
                        updated_at = excluded.updated_at
                    """,
                    (visible_thread, attempt_thread, checkpoint_id, int(token), now),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_attempts(self, visible_thread: str) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM hdb_checkpoint_attempts WHERE visible_thread = ? ORDER BY fencing_token",
                    (visible_thread,),
                ).fetchall()
            )

    def drop_attempt(self, attempt_thread: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM hdb_checkpoint_attempts WHERE attempt_thread = ?", (attempt_thread,)
            )

    def forget(self, visible_thread: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM hdb_checkpoint_attempts WHERE visible_thread = ?", (visible_thread,)
                )
                conn.execute(
                    "DELETE FROM hdb_checkpoint_pointers WHERE visible_thread = ?", (visible_thread,)
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise


def _store_for(saver: Any, pointer_path: str | None) -> CheckpointAttemptStore:
    if pointer_path:
        return CheckpointAttemptStore(pointer_path)
    import src.settings as settings

    return CheckpointAttemptStore(str(getattr(settings, "AGENT_CHECKPOINT_DB_PATH", "storage/agent_checkpoints.db")))


def prepare_attempt(
    saver: Any,
    visible_thread: str,
    fencing_token: int,
    *,
    attempt_id: str = "",
    resume_ref: dict[str, Any] | None = None,
    pointer_path: str | None = None,
) -> str:
    """Fork the canonical state into a fresh attempt namespace.

    Returns the thread the runner must use.  Legacy token 0 returns the visible
    thread unchanged.  Fork failures are surfaced so the caller can decide
    whether to fall back to the visible thread.

    ``resume_ref`` is the authoritative ``recoverable_checkpoint_ref`` of the
    previous attempt: take-over forks from that exact durable checkpoint
    (pending writes included), never from an unverified "latest" lookup.
    """
    visible = str(visible_thread or "")
    token = int(fencing_token or 0)
    if token <= 0 or not visible:
        return visible
    attempt = attempt_thread_id(visible, token, attempt_id)
    store = _store_for(saver, pointer_path)
    store.record_attempt(visible, attempt, token)
    if saver.get_tuple(_config(attempt)) is not None:
        return attempt
    started = time.monotonic()
    if resume_ref:
        source_thread = str(resume_ref.get("thread_id") or "")
        checkpoint_id = str(resume_ref.get("checkpoint_id") or "")
        checkpoint_ns = str(resume_ref.get("checkpoint_ns") or "")
        if source_thread and checkpoint_id:
            copied = copy_checkpoint_namespace(
                saver,
                _config(source_thread, checkpoint_id, checkpoint_ns),
                attempt,
                target_checkpoint_ns=checkpoint_ns,
            )
            if copied is not None:
                _record_metric(
                    "hdb.checkpoint.fork_latency",
                    time.monotonic() - started,
                    "Agent checkpoint attempt fork latency (recoverable pointer)",
                )
                return attempt
    pointer = store.pointer(visible)
    if pointer is not None and str(pointer["checkpoint_id"] or ""):
        copied = copy_checkpoint_namespace(
            saver,
            _config(visible, str(pointer["checkpoint_id"])),
            attempt,
        )
        if copied is not None:
            _record_metric(
                "hdb.checkpoint.fork_latency",
                time.monotonic() - started,
                "Agent checkpoint attempt fork latency (committed pointer)",
            )
            return attempt
    copy_latest_checkpoint(saver, visible, attempt, copy_pending_writes=True)
    _record_metric(
        "hdb.checkpoint.fork_latency",
        time.monotonic() - started,
        "Agent checkpoint attempt fork latency (latest visible checkpoint)",
    )
    return attempt


def attempt_checkpoint_ref(
    saver: Any,
    attempt_thread: str,
    *,
    fencing_token: int,
    generation: int = 0,
    attempt_id: str = "",
) -> dict[str, Any]:
    """Structure the canonical committed pointer for the authoritative store.

    Returns a JSON-safe reference for ``conversation_execution_state``.  The
    call only reads the saver; the pointer is committed by the conversation
    authority transaction, never here.
    """
    reference: dict[str, Any] = {
        "backend": "sqlite",
        "thread_id": str(attempt_thread or ""),
        "checkpoint_ns": "",
        "checkpoint_id": "",
        "attempt_id": str(attempt_id or ""),
        "fencing_token": int(fencing_token or 0),
        "generation": int(generation or 0),
    }
    try:
        source = saver.get_tuple(_config(str(attempt_thread or "")))
    except Exception:
        source = None
    if source is not None:
        try:
            checkpoint_id = getattr(source, "config", {})
            reference["checkpoint_id"] = str(
                checkpoint_id.get("configurable", {}).get("checkpoint_id") or ""
            )
        except Exception:
            reference["checkpoint_id"] = ""
    return reference


def commit_attempt(
    saver: Any,
    visible_thread: str,
    attempt_thread: str,
    fencing_token: int,
    *,
    pointer_path: str | None = None,
) -> bool:
    """Copy the attempt checkpoint to the visible thread and move the pointer.

    Raises ``StaleAttemptCommit`` when a newer attempt already committed.
    Returns True when this attempt moved the canonical pointer.
    """
    visible = str(visible_thread or "")
    token = int(fencing_token or 0)
    if token <= 0 or not visible or attempt_thread == visible:
        return False
    store = _store_for(saver, pointer_path)
    with _STORE_LOCK:
        pointer = store.pointer(visible)
        if pointer is not None and int(pointer["fencing_token"] or 0) >= token:
            raise StaleAttemptCommit(visible, token)
        started = time.monotonic()
        saved = copy_latest_checkpoint(saver, attempt_thread, visible, copy_pending_writes=True)
        if saved is None:
            return False
        checkpoint_id = ""
        try:
            checkpoint_id = str(saved["configurable"].get("checkpoint_id") or "")
        except Exception:
            checkpoint_id = ""
        if not store.cas_pointer(visible, attempt_thread, checkpoint_id, token):
            raise StaleAttemptCommit(visible, token)
        _record_metric(
            "hdb.checkpoint.commit_latency",
            time.monotonic() - started,
            "Agent checkpoint canonical commit latency",
        )
    purge_superseded_attempts(saver, visible, token, pointer_path=pointer_path)
    return True


def purge_superseded_attempts(
    saver: Any,
    visible_thread: str,
    current_token: int,
    *,
    pointer_path: str | None = None,
) -> int:
    """Delete attempt threads that are older than the committed token."""
    store = _store_for(saver, pointer_path)
    removed = 0
    for row in store.list_attempts(visible_thread):
        token = int(row["fencing_token"] or 0)
        attempt_thread = str(row["attempt_thread"])
        if token >= int(current_token):
            continue
        try:
            saver.delete_thread(attempt_thread)
        except Exception:
            continue
        store.drop_attempt(attempt_thread)
        removed += 1
    return removed


def forget_thread_state(saver: Any, visible_thread: str, *, pointer_path: str | None = None) -> None:
    """Delete the canonical thread, every attempt namespace and the pointer."""
    visible = str(visible_thread or "")
    store = _store_for(saver, pointer_path)
    for row in store.list_attempts(visible):
        try:
            saver.delete_thread(str(row["attempt_thread"]))
        except Exception:
            pass
    store.forget(visible)
    saver.delete_thread(visible)
