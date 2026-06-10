import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config.settings
from src.core.auth import ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, AuthUser


@dataclass
class AuditEvent:
    id: int
    actor_user_id: int | None
    actor_username: str
    actor_role: str
    department_id: int | None
    action: str
    target_type: str
    target_id: str
    kb_name: str
    success: bool
    error_message: str
    metadata_json: str
    created_at: str


@dataclass
class QueryTrace:
    id: int
    user_id: int | None
    username: str
    department_id: int | None
    chat_session_id: int | None
    user_message_id: int | None
    assistant_message_id: int | None
    kb_name: str
    original_query: str
    rewritten_query: str
    backend: str
    retriever_type: str
    vector_top_k: int | None
    bm25_top_k: int | None
    final_top_k: int | None
    latency_ms: int | None
    status: str
    error_message: str
    created_at: str


@dataclass
class RetrievedEvidence:
    id: int
    trace_id: int
    rank: int
    file_name: str
    document_id: str
    chunk_id: str
    vector_score: float | None
    bm25_score: float | None
    rrf_score: float | None
    rerank_score: float | None
    text_preview: str
    metadata_json: str
    created_at: str


class AppLogService:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.settings.AUTH_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER,
                    actor_username TEXT NOT NULL DEFAULT '',
                    actor_role TEXT NOT NULL DEFAULT '',
                    department_id INTEGER,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    kb_name TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL DEFAULT 1,
                    error_message TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS query_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT NOT NULL DEFAULT '',
                    department_id INTEGER,
                    chat_session_id INTEGER,
                    user_message_id INTEGER,
                    assistant_message_id INTEGER,
                    kb_name TEXT NOT NULL DEFAULT '',
                    original_query TEXT NOT NULL DEFAULT '',
                    rewritten_query TEXT NOT NULL DEFAULT '',
                    backend TEXT NOT NULL DEFAULT '',
                    retriever_type TEXT NOT NULL DEFAULT '',
                    vector_top_k INTEGER,
                    bm25_top_k INTEGER,
                    final_top_k INTEGER,
                    latency_ms INTEGER,
                    status TEXT NOT NULL DEFAULT 'success',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retrieved_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL,
                    file_name TEXT NOT NULL DEFAULT '',
                    document_id TEXT NOT NULL DEFAULT '',
                    chunk_id TEXT NOT NULL DEFAULT '',
                    vector_score REAL,
                    bm25_score REAL,
                    rrf_score REAL,
                    rerank_score REAL,
                    text_preview TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(trace_id) REFERENCES query_traces(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_actor ON audit_events(actor_user_id, department_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_action ON audit_events(action, success)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_traces_created ON query_traces(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_traces_user ON query_traces(user_id, department_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_traces_kb ON query_traces(kb_name, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieved_evidence_trace ON retrieved_evidence(trace_id, rank)")

    def record_audit(
        self,
        action: str,
        actor: AuthUser | None = None,
        target_type: str = "",
        target_id: str = "",
        kb_name: str = "",
        success: bool = True,
        error_message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_events (
                    actor_user_id, actor_username, actor_role, department_id,
                    action, target_type, target_id, kb_name, success,
                    error_message, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor.id if actor else None,
                    actor.username if actor else "",
                    actor.role if actor else "",
                    actor.department_id if actor else None,
                    action,
                    target_type,
                    str(target_id or ""),
                    kb_name or "",
                    1 if success else 0,
                    error_message or "",
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def record_query_trace(
        self,
        user: AuthUser | None,
        kb_name: str,
        original_query: str,
        chat_session_id: int | None = None,
        user_message_id: int | None = None,
        assistant_message_id: int | None = None,
        rewritten_query: str = "",
        backend: str = "",
        retriever_type: str = "hybrid",
        latency_ms: int | None = None,
        status: str = "success",
        error_message: str = "",
    ) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO query_traces (
                    user_id, username, department_id, chat_session_id,
                    user_message_id, assistant_message_id, kb_name,
                    original_query, rewritten_query, backend, retriever_type,
                    vector_top_k, bm25_top_k, final_top_k, latency_ms,
                    status, error_message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id if user else None,
                    user.username if user else "",
                    user.department_id if user else None,
                    chat_session_id,
                    user_message_id,
                    assistant_message_id,
                    kb_name or "",
                    original_query or "",
                    rewritten_query or "",
                    backend or config.settings.RAG_BACKEND,
                    retriever_type or "",
                    config.settings.VECTOR_TOP_K,
                    config.settings.BM25_TOP_K,
                    config.settings.FINAL_TOP_K,
                    latency_ms,
                    status,
                    error_message or "",
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def list_audit_events(
        self,
        viewer: AuthUser,
        action: str | None = None,
        kb_name: str | None = None,
        success: bool | None = None,
        keyword: str | None = None,
        limit: int = 200,
    ) -> list[AuditEvent]:
        where, params = scoped_where(viewer)
        if action:
            where.append("action = ?")
            params.append(action)
        if kb_name:
            where.append("kb_name = ?")
            params.append(kb_name)
        if success is not None:
            where.append("success = ?")
            params.append(1 if success else 0)
        if keyword:
            where.append("(actor_username LIKE ? OR target_id LIKE ? OR error_message LIKE ? OR metadata_json LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like])

        params.append(max(1, min(limit, 1000)))
        sql = f"""
            SELECT *
            FROM audit_events
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_audit_event(row) for row in rows]

    def list_query_traces(
        self,
        viewer: AuthUser,
        kb_name: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 200,
    ) -> list[QueryTrace]:
        where, params = scoped_where(viewer, user_column="user_id")
        if kb_name:
            where.append("kb_name = ?")
            params.append(kb_name)
        if status:
            where.append("status = ?")
            params.append(status)
        if keyword:
            where.append("(username LIKE ? OR original_query LIKE ? OR error_message LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])

        params.append(max(1, min(limit, 1000)))
        sql = f"""
            SELECT *
            FROM query_traces
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_query_trace(row) for row in rows]

    def list_evidence(self, viewer: AuthUser, trace_id: int) -> list[RetrievedEvidence]:
        with closing(self._connect()) as conn:
            trace = conn.execute("SELECT * FROM query_traces WHERE id = ?", (trace_id,)).fetchone()
            if not trace or not can_view_row(viewer, trace["user_id"], trace["department_id"]):
                return []
            rows = conn.execute(
                """
                SELECT *
                FROM retrieved_evidence
                WHERE trace_id = ?
                ORDER BY rank
                """,
                (trace_id,),
            ).fetchall()
        return [row_to_evidence(row) for row in rows]


def scoped_where(viewer: AuthUser, user_column: str = "actor_user_id") -> tuple[list[str], list[Any]]:
    if viewer.role == ROLE_SYSTEM_ADMIN:
        return ["1 = 1"], []
    if viewer.role == ROLE_DEPT_ADMIN:
        return ["department_id = ?"], [viewer.department_id]
    return [f"{user_column} = ?"], [viewer.id]


def can_view_row(viewer: AuthUser, user_id: int | None, department_id: int | None) -> bool:
    if viewer.role == ROLE_SYSTEM_ADMIN:
        return True
    if viewer.role == ROLE_DEPT_ADMIN:
        return department_id == viewer.department_id
    return user_id == viewer.id


def row_to_audit_event(row) -> AuditEvent:
    return AuditEvent(
        id=int(row["id"]),
        actor_user_id=row["actor_user_id"],
        actor_username=row["actor_username"],
        actor_role=row["actor_role"],
        department_id=row["department_id"],
        action=row["action"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        kb_name=row["kb_name"],
        success=bool(row["success"]),
        error_message=row["error_message"],
        metadata_json=row["metadata_json"],
        created_at=row["created_at"],
    )


def row_to_query_trace(row) -> QueryTrace:
    return QueryTrace(
        id=int(row["id"]),
        user_id=row["user_id"],
        username=row["username"],
        department_id=row["department_id"],
        chat_session_id=row["chat_session_id"],
        user_message_id=row["user_message_id"],
        assistant_message_id=row["assistant_message_id"],
        kb_name=row["kb_name"],
        original_query=row["original_query"],
        rewritten_query=row["rewritten_query"],
        backend=row["backend"],
        retriever_type=row["retriever_type"],
        vector_top_k=row["vector_top_k"],
        bm25_top_k=row["bm25_top_k"],
        final_top_k=row["final_top_k"],
        latency_ms=row["latency_ms"],
        status=row["status"],
        error_message=row["error_message"],
        created_at=row["created_at"],
    )


def row_to_evidence(row) -> RetrievedEvidence:
    return RetrievedEvidence(
        id=int(row["id"]),
        trace_id=int(row["trace_id"]),
        rank=int(row["rank"]),
        file_name=row["file_name"],
        document_id=row["document_id"],
        chunk_id=row["chunk_id"],
        vector_score=row["vector_score"],
        bm25_score=row["bm25_score"],
        rrf_score=row["rrf_score"],
        rerank_score=row["rerank_score"],
        text_preview=row["text_preview"],
        metadata_json=row["metadata_json"],
        created_at=row["created_at"],
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
