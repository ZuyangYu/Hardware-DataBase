"""Persistent task layer for the general agent.

A *task* outlives a single chat turn: it owns a workspace, a plan and the
artifacts produced across multiple attempts.  Turns remain the execution
unit; ``chat_turns.trace_context`` carries ``task_id`` so the worker can
rebuild the task context on every attempt.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import src.settings

TASK_STATUSES = {
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
}

_TERMINAL = {"completed", "failed", "cancelled"}

_MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}

_KIND_BY_SUFFIX = {
    ".xlsx": "spreadsheet",
    ".csv": "table",
    ".md": "document",
    ".docx": "document",
    ".pdf": "document",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".svg": "image",
    ".json": "data",
    ".txt": "document",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> str:
    return str(
        getattr(
            src.settings,
            "TASK_DB_PATH",
            os.path.join(src.settings.STORAGE_DIR, "tasks.db"),
        )
    )


def _default_workspace_root() -> str:
    return str(
        getattr(
            src.settings,
            "TASK_WORKSPACE_ROOT",
            os.path.join(src.settings.STORAGE_DIR, "workspaces"),
        )
    )


@dataclass
class Task:
    id: str
    user_id: int
    session_id: int
    kb_name: str
    title: str
    definition_id: str
    status: str
    query: str
    plan: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    workspace_dir: str = ""
    current_turn_id: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None


@dataclass
class TaskArtifact:
    id: str
    task_id: str
    name: str
    kind: str
    mime: str
    path: str
    size_bytes: int
    sha256: str
    version: int
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


class TaskStore:
    """SQLite-backed task/artifact store.

    The control-plane conversation store is still the authority for turns.
    This store owns the task-level state that must survive turns (plan,
    workspace, artifacts).  A later migration can move it onto the control
    plane without changing the callers.
    """

    _INIT_LOCK = threading.Lock()
    _INITIALIZED: set[str] = set()

    def __init__(
        self,
        db_path: str | None = None,
        workspace_root: str | None = None,
    ) -> None:
        self.db_path = os.path.abspath(db_path or _default_db_path())
        self.workspace_root = os.path.abspath(
            workspace_root or _default_workspace_root()
        )
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.workspace_root, exist_ok=True)
        needs_init = self.db_path not in self._INITIALIZED
        if needs_init:
            with self._INIT_LOCK:
                if self.db_path not in self._INITIALIZED:
                    self._init_db()
                    self._INITIALIZED.add(self.db_path)

    # -- connection helpers -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    session_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    definition_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    query TEXT NOT NULL,
                    plan_json TEXT NOT NULL DEFAULT '[]',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    workspace_dir TEXT NOT NULL DEFAULT '',
                    current_turn_id TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_user
                    ON tasks(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS task_turns (
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'message',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, turn_id)
                );

                CREATE TABLE IF NOT EXISTS task_artifacts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_task
                    ON task_artifacts(task_id, created_at);
                """
            )
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")
            }
            if "verification_json" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN verification_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.commit()

    # -- workspace ----------------------------------------------------------
    def ensure_workspace(self, task_id: str) -> str:
        root = os.path.join(self.workspace_root, str(task_id))
        out = os.path.join(root, "out")
        os.makedirs(out, exist_ok=True)
        return root

    # -- task CRUD ----------------------------------------------------------
    def create_task(
        self,
        *,
        user_id: int,
        session_id: int,
        kb_name: str,
        title: str,
        definition_id: str,
        query: str,
        plan: list[dict[str, Any]] | None = None,
    ) -> Task:
        task_id = uuid.uuid4().hex
        workspace_dir = self.ensure_workspace(task_id)
        now = _now()
        task = Task(
            id=task_id,
            user_id=int(user_id),
            session_id=int(session_id),
            kb_name=str(kb_name),
            title=str(title or query[:40] or "未命名任务"),
            definition_id=str(definition_id),
            status="queued",
            query=str(query),
            plan=list(plan or []),
            workspace_dir=workspace_dir,
            created_at=now,
            updated_at=now,
        )
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, user_id, session_id, kb_name, title, definition_id,
                    status, query, plan_json, workspace_dir, current_turn_id,
                    error_message, created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, NULL)
                """,
                (
                    task.id,
                    task.user_id,
                    task.session_id,
                    task.kb_name,
                    task.title,
                    task.definition_id,
                    task.status,
                    task.query,
                    json.dumps(task.plan, ensure_ascii=False),
                    task.workspace_dir,
                    now,
                    now,
                ),
            )
            conn.commit()
        return task

    def get_task(self, task_id: str, user_id: int | None = None) -> Task | None:
        with closing(self._connect()) as conn:
            if user_id is None:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (str(task_id),)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                    (str(task_id), int(user_id)),
                ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, user_id: int, limit: int = 50) -> list[Task]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks WHERE user_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (int(user_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def set_status(
        self,
        task_id: str,
        status: str,
        *,
        error_message: str | None = None,
        finished: bool | None = None,
    ) -> None:
        status = str(status)
        if status not in TASK_STATUSES:
            raise ValueError(f"unsupported task status: {status}")
        now = _now()
        terminal = status in _TERMINAL
        if finished is None:
            finished = terminal
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?,
                    error_message = COALESCE(?, error_message),
                    finished_at = CASE WHEN ? THEN COALESCE(finished_at, ?) ELSE finished_at END
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    error_message,
                    bool(finished),
                    now,
                    str(task_id),
                ),
            )
            conn.commit()

    def set_current_turn(self, task_id: str, turn_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE tasks SET current_turn_id = ?, updated_at = ? WHERE id = ?",
                (str(turn_id), _now(), str(task_id)),
            )
            conn.commit()

    def add_turn(self, task_id: str, turn_id: str, kind: str = "message") -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO task_turns (task_id, turn_id, kind, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(task_id), str(turn_id), str(kind), _now()),
            )
            conn.commit()

    def list_turns(self, task_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT turn_id, kind, created_at FROM task_turns
                WHERE task_id = ? ORDER BY created_at
                """,
                (str(task_id),),
            ).fetchall()
        return [
            {
                "turn_id": str(row["turn_id"]),
                "kind": str(row["kind"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def set_plan(self, task_id: str, plan: list[dict[str, Any]]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE tasks SET plan_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(plan, ensure_ascii=False), _now(), str(task_id)),
            )
            conn.commit()

    def set_verification(self, task_id: str, verification: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE tasks SET verification_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(verification or {}, ensure_ascii=False),
                    _now(),
                    str(task_id),
                ),
            )
            conn.commit()

    # -- artifacts ----------------------------------------------------------
    def register_artifact(
        self,
        task_id: str,
        path: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        mime: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskArtifact:
        abs_path = os.path.abspath(str(path))
        task = self.get_task(task_id)
        if task is None:
            raise KeyError("task not found")
        root = os.path.abspath(task.workspace_dir)
        if not abs_path.startswith(root + os.sep):
            raise ValueError("artifact path escapes the task workspace")
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(abs_path)
        suffix = os.path.splitext(abs_path)[1].lower()
        artifact_name = str(name or os.path.basename(abs_path))
        size = os.path.getsize(abs_path)
        digest = ""
        hasher = hashlib.sha256()
        with open(abs_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        with closing(self._connect()) as conn:
            version = int(
                conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts WHERE task_id = ? AND name = ?",
                    (str(task_id), artifact_name),
                ).fetchone()[0]
            ) + 1
            artifact = TaskArtifact(
                id=uuid.uuid4().hex,
                task_id=str(task_id),
                name=artifact_name,
                kind=str(kind or _KIND_BY_SUFFIX.get(suffix, "file")),
                mime=str(mime or _MIME_BY_SUFFIX.get(suffix, "application/octet-stream")),
                path=abs_path,
                size_bytes=size,
                sha256=digest,
                version=version,
                metadata=dict(metadata or {}),
                created_at=_now(),
            )
            conn.execute(
                """
                INSERT INTO task_artifacts (
                    id, task_id, name, kind, mime, path, size_bytes, sha256,
                    version, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.task_id,
                    artifact.name,
                    artifact.kind,
                    artifact.mime,
                    artifact.path,
                    artifact.size_bytes,
                    artifact.sha256,
                    artifact.version,
                    json.dumps(artifact.metadata, ensure_ascii=False),
                    artifact.created_at,
                ),
            )
            conn.commit()
        return artifact

    def scan_workspace_artifacts(self, task_id: str) -> list[TaskArtifact]:
        """Register new files under ``<workspace>/out`` (idempotent by name)."""
        task = self.get_task(task_id)
        if task is None:
            return []
        out_dir = os.path.join(task.workspace_dir, "out")
        if not os.path.isdir(out_dir):
            return []
        existing = {
            (item.name, item.sha256) for item in self.list_artifacts(task_id)
        }
        registered: list[TaskArtifact] = []
        for current_root, _dirs, files in os.walk(out_dir):
            for filename in files:
                abs_path = os.path.join(current_root, filename)
                try:
                    size = os.path.getsize(abs_path)
                    hasher = hashlib.sha256()
                    with open(abs_path, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
                except OSError:
                    continue
                name = os.path.relpath(abs_path, out_dir).replace(os.sep, "/")
                if (name, digest) in existing:
                    continue
                registered.append(
                    self.register_artifact(
                        task_id,
                        abs_path,
                        name=name,
                        metadata={"size_bytes": size},
                    )
                )
                existing.add((name, digest))
        return registered

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_artifacts WHERE task_id = ?
                ORDER BY created_at, name
                """,
                (str(task_id),),
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> TaskArtifact | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM task_artifacts WHERE id = ?", (str(artifact_id),)
            ).fetchone()
        return self._row_to_artifact(row) if row else None

    # -- row mapping --------------------------------------------------------
    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        try:
            plan = json.loads(row["plan_json"] or "[]")
        except Exception:
            plan = []
        try:
            verification = json.loads(
                row["verification_json"] if "verification_json" in row.keys() else "{}"
            )
        except Exception:
            verification = {}
        return Task(
            id=str(row["id"]),
            user_id=int(row["user_id"]),
            session_id=int(row["session_id"]),
            kb_name=str(row["kb_name"]),
            title=str(row["title"]),
            definition_id=str(row["definition_id"]),
            status=str(row["status"]),
            query=str(row["query"]),
            plan=plan if isinstance(plan, list) else [],
            verification=verification if isinstance(verification, dict) else {},
            workspace_dir=str(row["workspace_dir"]),
            current_turn_id=str(row["current_turn_id"]),
            error_message=str(row["error_message"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
        )

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> TaskArtifact:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        return TaskArtifact(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
            mime=str(row["mime"]),
            path=str(row["path"]),
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            version=int(row["version"]),
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=str(row["created_at"]),
        )


__all__ = ["Task", "TaskArtifact", "TaskStore", "TASK_STATUSES"]
