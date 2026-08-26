"""Durable worker process registry used by System Status and health checks."""

from __future__ import annotations

import os
import socket
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import config.settings as settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str | None = None):
    path = db_path or settings.AUTH_DB_PATH
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_table(db_path: str | None = None) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_instances (
                worker_id TEXT PRIMARY KEY,
                worker_type TEXT NOT NULL,
                hostname TEXT NOT NULL DEFAULT '',
                pid INTEGER,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                current_task_kind TEXT NOT NULL DEFAULT '',
                current_task_id TEXT NOT NULL DEFAULT '',
                service_version TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_instances_heartbeat ON worker_instances(heartbeat_at)")


def register(worker_id: str, *, worker_type: str = "hardware-worker", db_path: str | None = None) -> None:
    try:
        ensure_table(db_path)
        now = _now()
        with closing(_connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO worker_instances (
                    worker_id, worker_type, hostname, pid, started_at, heartbeat_at, service_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at,
                    hostname=excluded.hostname, pid=excluded.pid
                """,
                (worker_id, worker_type, socket.gethostname(), os.getpid(), now, now, settings.OBS_SERVICE_VERSION),
            )
    except Exception:
        pass


def heartbeat(worker_id: str, *, task_kind: str = "", task_id: str = "", db_path: str | None = None) -> None:
    try:
        ensure_table(db_path)
        with closing(_connect(db_path)) as conn:
            conn.execute(
                """
                UPDATE worker_instances
                SET heartbeat_at = ?, current_task_kind = ?, current_task_id = ?
                WHERE worker_id = ?
                """,
                (_now(), task_kind, task_id, worker_id),
            )
    except Exception:
        pass


def unregister(worker_id: str, db_path: str | None = None) -> None:
    try:
        with closing(_connect(db_path)) as conn:
            conn.execute("DELETE FROM worker_instances WHERE worker_id = ?", (worker_id,))
    except Exception:
        pass


def list_workers(*, stale_after_seconds: int | None = None, db_path: str | None = None) -> list[dict]:
    stale_after_seconds = int(stale_after_seconds or settings.OBS_WORKER_STALE_SECONDS)
    try:
        ensure_table(db_path)
        with closing(_connect(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM worker_instances
                WHERE datetime(heartbeat_at) >= datetime('now', ?)
                ORDER BY heartbeat_at DESC
                """,
                (f"-{max(1, stale_after_seconds)} seconds",),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []
