"""Workspace-first persistence for the general agent.

A workspace is the durable project container: it owns a hidden directory and
one or more sessions.  A session is a conversation; a turn/run is one agent
execution inside that session.  This deliberately replaces the old
"every chat auto-creates a task" model.

Storage backends mirror the task store: SQLite is the development/default
backend, PostgreSQL is used when ``TASK_STORE_BACKEND=postgres`` and a DSN is
configured.  The tables live in the same database as the task store but are
independent; callers should depend on this module, not on task tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import src.settings as src_settings


WORKSPACE_KINDS = frozenset({"general", "knowledge"})
_KIND_BY_SUFFIX = {
    ".md": "document",
    ".txt": "document",
    ".docx": "document",
    ".pdf": "document",
    ".xlsx": "spreadsheet",
    ".xlsm": "spreadsheet",
    ".csv": "table",
    ".json": "data",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".svg": "image",
}
_MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Workspace:
    id: str
    tenant_id: str
    user_id: int
    kind: str
    name: str
    kb_name: str
    kb_id: int | None
    workspace_dir: str
    created_at: str
    updated_at: str


@dataclass
class WorkspaceArtifact:
    id: str
    tenant_id: str
    workspace_id: str
    session_id: int
    turn_id: str
    name: str
    kind: str
    mime: str
    path: str
    size_bytes: int
    sha256: str
    version: int
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


class WorkspaceStore:
    """SQLite/PostgreSQL workspace and artifact store."""

    _INIT_LOCK = threading.Lock()
    _INITIALIZED: set[str] = set()

    def __init__(
        self,
        *,
        db_path: str | None = None,
        dsn: str | None = None,
        workspace_root: str | None = None,
        backend: str | None = None,
    ) -> None:
        configured = str(
            backend
            or os.getenv("WORKSPACE_STORE_BACKEND", "")
            or os.getenv("TASK_STORE_BACKEND", "")
            or getattr(src_settings, "WORKSPACE_STORE_BACKEND", "")
            or (
                "postgres"
                if str(getattr(src_settings, "CONTROL_PLANE_BACKEND", "")).lower()
                == "postgres"
                and not os.getenv("TASK_STORE_BACKEND", "")
                else ""
            )
            or getattr(src_settings, "TASK_STORE_BACKEND", "sqlite")
            or "sqlite"
        ).strip().lower()
        self.backend = "postgres" if configured == "postgres" else "sqlite"
        self.dsn = str(
            dsn
            or os.getenv("WORKSPACE_DB_URL", "")
            or getattr(src_settings, "WORKSPACE_DB_URL", "")
            or getattr(src_settings, "TASK_DB_URL", "")
            or getattr(src_settings, "CONTROL_PLANE_DATABASE_URL", "")
            or ""
        ).strip()
        if self.backend == "postgres" and not self.dsn:
            raise RuntimeError(
                "workspace store backend=postgres requires WORKSPACE_DB_URL, "
                "TASK_DB_URL or CONTROL_PLANE_DATABASE_URL"
            )
        self.db_path = (
            None
            if self.backend == "postgres"
            else os.path.abspath(
                str(
                    db_path
                    or os.getenv("WORKSPACE_DB_PATH", "")
                    or getattr(src_settings, "WORKSPACE_DB_PATH", "")
                    or getattr(src_settings, "TASK_DB_PATH", "")
                    or ""
                )
            )
        )
        if self.db_path:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.workspace_root = os.path.abspath(
            str(
                workspace_root
                or os.getenv("WORKSPACE_ROOT", "")
                or getattr(src_settings, "WORKSPACE_ROOT", "")
                or getattr(src_settings, "TASK_WORKSPACE_ROOT", "")
                or os.path.join(src_settings.STORAGE_DIR, "workspaces")
            )
        )
        os.makedirs(self.workspace_root, exist_ok=True)
        init_key = (
            f"postgres:{self.dsn}"
            if self.backend == "postgres"
            else f"sqlite:{self.db_path}"
        )
        if init_key not in self._INITIALIZED:
            with self._INIT_LOCK:
                if init_key not in self._INITIALIZED:
                    self._init_db()
                    self._INITIALIZED.add(init_key)

    # -- connection ---------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self.backend == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=True,
            ) as conn:
                yield conn
            return
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def _execute(self, conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if self.backend == "postgres":
            return conn.execute(sql, params)
        return conn.execute(sql.replace("%s", "?"), params)

    def _fetchone(self, conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        cursor = self._execute(conn, sql, params)
        return cursor.fetchone()

    def _fetchall(self, conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        cursor = self._execute(conn, sql, params)
        return list(cursor.fetchall())

    def _init_db(self) -> None:
        with self._connect() as conn:
            self._execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS agent_workspaces (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    user_id BIGINT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kb_name TEXT NOT NULL DEFAULT '',
                    kb_id BIGINT,
                    workspace_dir TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
            )
            self._execute(
                conn,
                "CREATE INDEX IF NOT EXISTS idx_agent_workspaces_user "
                "ON agent_workspaces(user_id, updated_at)",
            )
            self._execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS agent_workspace_sessions (
                    workspace_id TEXT NOT NULL,
                    session_id BIGINT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, session_id),
                    UNIQUE (session_id)
                )
                """,
            )
            self._execute(
                conn,
                "CREATE INDEX IF NOT EXISTS idx_agent_workspace_sessions_ws "
                "ON agent_workspace_sessions(workspace_id)",
            )
            self._execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS agent_workspace_artifacts (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    workspace_id TEXT NOT NULL,
                    session_id BIGINT NOT NULL DEFAULT 0,
                    turn_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,
                    sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """,
            )
            self._execute(
                conn,
                "CREATE INDEX IF NOT EXISTS idx_agent_workspace_artifacts_ws "
                "ON agent_workspace_artifacts(workspace_id, created_at)",
            )

    # -- workspaces ---------------------------------------------------------
    def create_workspace(
        self,
        *,
        user_id: int,
        kind: str,
        name: str,
        kb_name: str = "",
        kb_id: int | None = None,
        tenant_id: str = "default",
    ) -> Workspace:
        normalized_kind = str(kind or "general").strip().lower()
        if normalized_kind not in WORKSPACE_KINDS:
            raise ValueError(f"unsupported workspace kind: {kind}")
        workspace_id = uuid.uuid4().hex
        workspace_dir = os.path.join(self.workspace_root, workspace_id)
        os.makedirs(os.path.join(workspace_dir, "out"), exist_ok=True)
        now = _now()
        with self._connect() as conn:
            self._execute(
                conn,
                """
                INSERT INTO agent_workspaces (
                    id, tenant_id, user_id, kind, name, kb_name, kb_id,
                    workspace_dir, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    workspace_id,
                    str(tenant_id or "default"),
                    int(user_id),
                    normalized_kind,
                    str(name or "未命名工作区"),
                    str(kb_name or ""),
                    int(kb_id) if kb_id not in (None, "") else None,
                    workspace_dir,
                    now,
                    now,
                ),
            )
        return Workspace(
            id=workspace_id,
            tenant_id=str(tenant_id or "default"),
            user_id=int(user_id),
            kind=normalized_kind,
            name=str(name or "未命名工作区"),
            kb_name=str(kb_name or ""),
            kb_id=int(kb_id) if kb_id not in (None, "") else None,
            workspace_dir=workspace_dir,
            created_at=now,
            updated_at=now,
        )

    def get_or_create_for_user(
        self,
        *,
        user_id: int,
        kind: str,
        kb_name: str = "",
        kb_id: int | None = None,
        tenant_id: str = "default",
    ) -> Workspace:
        normalized_kind = "knowledge" if str(kind) == "knowledge" else "general"
        with self._connect() as conn:
            row = self._fetchone(
                conn,
                """
                SELECT * FROM agent_workspaces
                WHERE tenant_id = %s AND user_id = %s AND kind = %s AND kb_name = %s
                ORDER BY created_at LIMIT 1
                """,
                (
                    str(tenant_id or "default"),
                    int(user_id),
                    normalized_kind,
                    str(kb_name or ""),
                ),
            )
        if row is not None:
            return self._row_to_workspace(row)
        name = (
            f"{kb_name} 知识库工作区"
            if normalized_kind == "knowledge" and kb_name
            else "通用工作区"
        )
        return self.create_workspace(
            user_id=user_id,
            kind=normalized_kind,
            name=name,
            kb_name=kb_name,
            kb_id=kb_id,
            tenant_id=tenant_id,
        )

    def get_workspace(
        self,
        workspace_id: str,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> Workspace | None:
        clauses = ["id = %s"]
        params: list[Any] = [str(workspace_id)]
        if user_id is not None:
            clauses.append("user_id = %s")
            params.append(int(user_id))
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(str(tenant_id))
        with self._connect() as conn:
            row = self._fetchone(
                conn,
                "SELECT * FROM agent_workspaces WHERE " + " AND ".join(clauses),
                tuple(params),
            )
        return self._row_to_workspace(row) if row is not None else None

    def list_workspaces(
        self, user_id: int, limit: int = 100, tenant_id: str | None = None
    ) -> list[Workspace]:
        with self._connect() as conn:
            if tenant_id:
                rows = self._fetchall(
                    conn,
                    """
                    SELECT * FROM agent_workspaces
                    WHERE tenant_id = %s AND user_id = %s
                    ORDER BY updated_at DESC, id DESC
                    LIMIT %s
                    """,
                    (
                        str(tenant_id),
                        int(user_id),
                        max(1, min(int(limit), 200)),
                    ),
                )
            else:
                rows = self._fetchall(
                    conn,
                    """
                    SELECT * FROM agent_workspaces
                    WHERE user_id = %s
                    ORDER BY updated_at DESC, id DESC
                    LIMIT %s
                    """,
                    (int(user_id), max(1, min(int(limit), 200))),
                )
        return [self._row_to_workspace(row) for row in rows]

    def attach_session(self, workspace_id: str, session_id: int) -> None:
        now = _now()
        with self._connect() as conn:
            if self.backend == "postgres":
                self._execute(
                    conn,
                    """
                    INSERT INTO agent_workspace_sessions (workspace_id, session_id, created_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE
                    SET workspace_id = EXCLUDED.workspace_id
                    """,
                    (str(workspace_id), int(session_id), now),
                )
            else:
                self._execute(
                    conn,
                    """
                    INSERT INTO agent_workspace_sessions (workspace_id, session_id, created_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE
                    SET workspace_id = excluded.workspace_id
                    """,
                    (str(workspace_id), int(session_id), now),
                )

    def get_workspace_for_session(self, session_id: int) -> Workspace | None:
        with self._connect() as conn:
            row = self._fetchone(
                conn,
                """
                SELECT w.* FROM agent_workspaces w
                JOIN agent_workspace_sessions s ON s.workspace_id = w.id
                WHERE s.session_id = %s
                """,
                (int(session_id),),
            )
        return self._row_to_workspace(row) if row is not None else None

    def list_session_ids(self, workspace_id: str) -> list[int]:
        with self._connect() as conn:
            rows = self._fetchall(
                conn,
                """
                SELECT session_id FROM agent_workspace_sessions
                WHERE workspace_id = %s ORDER BY created_at
                """,
                (str(workspace_id),),
            )
        return [int(row["session_id"]) for row in rows]

    # -- artifacts ----------------------------------------------------------
    def register_artifact(
        self,
        workspace_id: str,
        path: str,
        *,
        session_id: int = 0,
        turn_id: str = "",
        name: str | None = None,
        kind: str | None = None,
        metadata: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> WorkspaceArtifact:
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise KeyError("workspace not found")
        abs_path = os.path.abspath(str(path))
        root = os.path.abspath(workspace.workspace_dir)
        if not abs_path.startswith(root + os.sep):
            raise ValueError("artifact path escapes the workspace")
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
            row = self._fetchone(
                conn,
                """
                SELECT COUNT(*) AS count FROM agent_workspace_artifacts
                WHERE workspace_id = %s AND name = %s
                """,
                (str(workspace_id), artifact_name),
            )
            version = int((row["count"] if row is not None else 0) or 0) + 1
            artifact = WorkspaceArtifact(
                id=uuid.uuid4().hex,
                tenant_id=str(tenant_id or workspace.tenant_id or "default"),
                workspace_id=str(workspace_id),
                session_id=int(session_id or 0),
                turn_id=str(turn_id or ""),
                name=artifact_name,
                kind=str(kind or _KIND_BY_SUFFIX.get(suffix, "file")),
                mime=str(_MIME_BY_SUFFIX.get(suffix, "application/octet-stream")),
                path=abs_path,
                size_bytes=size,
                sha256=digest,
                version=version,
                metadata=dict(metadata or {}),
                created_at=_now(),
            )
            self._execute(
                conn,
                """
                INSERT INTO agent_workspace_artifacts (
                    id, tenant_id, workspace_id, session_id, turn_id, name,
                    kind, mime, path, size_bytes, sha256, version,
                    metadata_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    artifact.id,
                    artifact.tenant_id,
                    artifact.workspace_id,
                    artifact.session_id,
                    artifact.turn_id,
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
        return artifact

    def scan_workspace_artifacts(
        self, workspace_id: str, *, session_id: int = 0, turn_id: str = ""
    ) -> list[WorkspaceArtifact]:
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            return []
        out_dir = os.path.join(workspace.workspace_dir, "out")
        if not os.path.isdir(out_dir):
            return []
        existing = {
            (item.name, item.sha256) for item in self.list_artifacts(workspace_id)
        }
        registered: list[WorkspaceArtifact] = []
        for current_root, _dirs, files in os.walk(out_dir):
            for filename in files:
                abs_path = os.path.join(current_root, filename)
                try:
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
                        workspace_id,
                        abs_path,
                        session_id=session_id,
                        turn_id=turn_id,
                        name=name,
                        metadata={"size_bytes": os.path.getsize(abs_path)},
                    )
                )
                existing.add((name, digest))
        return registered

    def list_artifacts(
        self, workspace_id: str, *, session_id: int | None = None
    ) -> list[WorkspaceArtifact]:
        with self._connect() as conn:
            if session_id is None:
                rows = self._fetchall(
                    conn,
                    """
                    SELECT * FROM agent_workspace_artifacts
                    WHERE workspace_id = %s ORDER BY created_at, name
                    """,
                    (str(workspace_id),),
                )
            else:
                rows = self._fetchall(
                    conn,
                    """
                    SELECT * FROM agent_workspace_artifacts
                    WHERE workspace_id = %s AND session_id = %s
                    ORDER BY created_at, name
                    """,
                    (str(workspace_id), int(session_id)),
                )
        return [self._row_to_artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> WorkspaceArtifact | None:
        with self._connect() as conn:
            row = self._fetchone(
                conn,
                "SELECT * FROM agent_workspace_artifacts WHERE id = %s",
                (str(artifact_id),),
            )
        return self._row_to_artifact(row) if row is not None else None

    # -- row mapping --------------------------------------------------------
    def _row_to_workspace(self, row: Any) -> Workspace:
        return Workspace(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"] or "default"),
            user_id=int(row["user_id"]),
            kind=str(row["kind"]),
            name=str(row["name"]),
            kb_name=str(row["kb_name"] or ""),
            kb_id=int(row["kb_id"]) if row["kb_id"] not in (None, "") else None,
            workspace_dir=str(row["workspace_dir"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_artifact(self, row: Any) -> WorkspaceArtifact:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        return WorkspaceArtifact(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"] or "default"),
            workspace_id=str(row["workspace_id"]),
            session_id=int(row["session_id"] or 0),
            turn_id=str(row["turn_id"] or ""),
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


def get_workspace_store() -> WorkspaceStore:
    return WorkspaceStore()


__all__ = [
    "Workspace",
    "WorkspaceArtifact",
    "WorkspaceStore",
    "WORKSPACE_KINDS",
    "get_workspace_store",
]
