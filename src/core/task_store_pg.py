"""Postgres-backed task store (multi-instance deployment).

Mirrors :class:`src.core.task_store.TaskStore` over the control-plane
Postgres database.  Task rows are namespaced as ``agent_tasks`` so they never
collide with control-plane tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.core.task_store import (
    TASK_STATUSES,
    Task,
    TaskArtifact,
    _KIND_BY_SUFFIX,
    _MIME_BY_SUFFIX,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresTaskStore:
    def __init__(
        self,
        dsn: str,
        workspace_root: str | None = None,
    ) -> None:
        self.dsn = str(dsn or "").strip()
        if not self.dsn:
            raise ValueError("PostgresTaskStore requires a DSN")
        import src.settings as settings

        self.workspace_root = os.path.abspath(
            workspace_root
            or getattr(
                settings,
                "TASK_WORKSPACE_ROOT",
                os.path.join(settings.STORAGE_DIR, "workspaces"),
            )
        )
        os.makedirs(self.workspace_root, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=True,
        ) as conn:
            yield conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    session_id BIGINT NOT NULL,
                    kb_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    definition_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    query TEXT NOT NULL,
                    plan_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    verification_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    workspace_dir TEXT NOT NULL DEFAULT '',
                    current_turn_id TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_tasks_user "
                "ON agent_tasks(user_id, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_task_turns (
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'message',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, turn_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_task_artifacts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,
                    sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "ALTER TABLE agent_task_artifacts "
                "ADD COLUMN IF NOT EXISTS turn_id TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_artifacts_task "
                "ON agent_task_artifacts(task_id, created_at)"
            )

    def ensure_workspace(self, task_id: str) -> str:
        root = os.path.join(self.workspace_root, str(task_id))
        os.makedirs(os.path.join(root, "out"), exist_ok=True)
        return root

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_tasks (
                    id, user_id, session_id, kb_name, title, definition_id,
                    status, query, plan_json, verification_json, workspace_dir,
                    current_turn_id, error_message, created_at, updated_at,
                    finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          '', '', %s, %s, NULL)
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
                    Jsonb(task.plan),
                    Jsonb({}),
                    task.workspace_dir,
                    now,
                    now,
                ),
            )
        return task

    def get_task(self, task_id: str, user_id: int | None = None) -> Task | None:
        with self._connect() as conn:
            if user_id is None:
                row = conn.execute(
                    "SELECT * FROM agent_tasks WHERE id = %s", (str(task_id),)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM agent_tasks WHERE id = %s AND user_id = %s",
                    (str(task_id), int(user_id)),
                ).fetchone()
        return self._row_to_task(row) if row else None

    def get_or_create_task_for_session(
        self,
        *,
        user_id: int,
        session_id: int,
        kb_name: str,
        title: str,
        query: str,
        definition_id: str = "",
    ) -> Task:
        existing = self.list_tasks_for_session(user_id, session_id, limit=1)
        if existing:
            return existing[0]
        return self.create_task(
            user_id=user_id,
            session_id=session_id,
            kb_name=kb_name,
            title=title,
            definition_id=definition_id,
            query=query,
        )

    def list_tasks_for_session(
        self, user_id: int, session_id: int, limit: int = 50
    ) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE user_id = %s AND session_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (int(user_id), int(session_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_tasks(self, user_id: int, limit: int = 50) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
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
        terminal = status in {"completed", "failed", "cancelled"}
        if finished is None:
            finished = terminal
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = %s, updated_at = %s,
                    error_message = COALESCE(%s, error_message),
                    finished_at = COALESCE(%s, finished_at)
                WHERE id = %s
                """,
                (
                    status,
                    now,
                    error_message,
                    now if finished else None,
                    str(task_id),
                ),
            )

    def set_current_turn(self, task_id: str, turn_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_tasks SET current_turn_id = %s, updated_at = %s "
                "WHERE id = %s",
                (str(turn_id), _now(), str(task_id)),
            )

    def add_turn(self, task_id: str, turn_id: str, kind: str = "message") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_task_turns (task_id, turn_id, kind, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (str(task_id), str(turn_id), str(kind), _now()),
            )

    def list_turns(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT turn_id, kind, created_at FROM agent_task_turns "
                "WHERE task_id = %s ORDER BY created_at",
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
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_tasks SET plan_json = %s, updated_at = %s WHERE id = %s",
                (Jsonb(plan), _now(), str(task_id)),
            )

    def set_verification(self, task_id: str, verification: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_tasks SET verification_json = %s, updated_at = %s "
                "WHERE id = %s",
                (Jsonb(verification or {}), _now(), str(task_id)),
            )

    def register_artifact(
        self,
        task_id: str,
        path: str,
        *,
        turn_id: str = "",
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
        hasher = hashlib.sha256()
        with open(abs_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        with self._connect() as conn:
            version = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM agent_task_artifacts "
                    "WHERE task_id = %s AND name = %s",
                    (str(task_id), artifact_name),
                ).fetchone()["n"]
            ) + 1
            artifact = TaskArtifact(
                id=uuid.uuid4().hex,
                task_id=str(task_id),
                turn_id=str(turn_id or ""),
                name=artifact_name,
                kind=str(kind or _KIND_BY_SUFFIX.get(suffix, "file")),
                mime=str(
                    mime or _MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
                ),
                path=abs_path,
                size_bytes=size,
                sha256=digest,
                version=version,
                metadata=dict(metadata or {}),
                created_at=_now(),
            )
            conn.execute(
                """
                INSERT INTO agent_task_artifacts (
                    id, task_id, turn_id, name, kind, mime, path, size_bytes,
                    sha256, version, metadata_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    artifact.id,
                    artifact.task_id,
                    artifact.turn_id,
                    artifact.name,
                    artifact.kind,
                    artifact.mime,
                    artifact.path,
                    artifact.size_bytes,
                    artifact.sha256,
                    artifact.version,
                    Jsonb(artifact.metadata),
                    artifact.created_at,
                ),
            )
        return artifact

    def scan_workspace_artifacts(
        self, task_id: str, *, turn_id: str = ""
    ) -> list[TaskArtifact]:
        task = self.get_task(task_id)
        if task is None:
            return []
        out_dir = os.path.join(task.workspace_dir, "out")
        if not os.path.isdir(out_dir):
            return []
        existing = {(item.name, item.sha256) for item in self.list_artifacts(task_id)}
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
                        turn_id=turn_id,
                        name=name,
                        metadata={"size_bytes": size},
                    )
                )
                existing.add((name, digest))
        return registered

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_task_artifacts WHERE task_id = %s "
                "ORDER BY created_at, name",
                (str(task_id),),
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> TaskArtifact | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_task_artifacts WHERE id = %s",
                (str(artifact_id),),
            ).fetchone()
        return self._row_to_artifact(row) if row else None

    @staticmethod
    def _row_to_task(row: dict) -> Task:
        plan = row.get("plan_json") or []
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except ValueError:
                plan = []
        verification = row.get("verification_json") or {}
        if isinstance(verification, str):
            try:
                verification = json.loads(verification)
            except ValueError:
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
    def _row_to_artifact(row: dict) -> TaskArtifact:
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        return TaskArtifact(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            turn_id=str(row.get("turn_id") or ""),
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


__all__ = ["PostgresTaskStore"]
