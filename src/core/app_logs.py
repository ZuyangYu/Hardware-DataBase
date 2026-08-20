import json
import os
import sqlite3
import hashlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config.settings
from src.core.auth import ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, ROLE_USER, AuthUser


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
    previous_hash: str
    integrity_hash: str
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
    final_top_k: int | None
    latency_ms: int | None
    status: str
    error_message: str
    metadata_json: str
    created_at: str
    otel_trace_id: str = ""
    otel_span_id: str = ""
    turn_id: str = ""


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


QUERY_FAILURE_PREFIXES = (
    "Error:",
    "系统错误:",
    "RAGFlow retrieved relevant context, but answer generation failed:",
)


def query_trace_status(response: object, retrieval_summary: dict[str, Any] | None = None) -> tuple[str, str]:
    summary = retrieval_summary or {}
    status = str(summary.get("status") or "").strip()
    if status in {"success", "failed", "partial", "no_evidence"}:
        if status == "success":
            return "success", ""
        error_message = str(summary.get("error_message") or summary.get("error_stage") or status).strip()
        return "failed", error_message

    text = str(response or "").strip()
    if any(text.startswith(prefix) for prefix in QUERY_FAILURE_PREFIXES):
        return "failed", text
    return "success", ""


class AppLogService:
    # 建表/迁移是幂等的，但每次实例化都重跑一遍（3 张表 + 6 个索引 +
    # PRAGMA table_info + 若干 PRAGMA）代价不小。审计场景每次事件都 new
    # 一个实例（ragflow_backend._audit），靠这个标记让全进程只初始化一次。
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.settings.AUTH_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if self.db_path not in AppLogService._initialized_paths:
            self._init_db()
            AppLogService._initialized_paths.add(self.db_path)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
                    previous_hash TEXT NOT NULL DEFAULT '',
                    integrity_hash TEXT NOT NULL DEFAULT '',
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
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    otel_trace_id TEXT NOT NULL DEFAULT '',
                    otel_span_id TEXT NOT NULL DEFAULT '',
                    turn_id TEXT NOT NULL DEFAULT '',
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
            self._ensure_columns(conn)

    def _ensure_columns(self, conn):
        audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "previous_hash" not in audit_columns:
            conn.execute("ALTER TABLE audit_events ADD COLUMN previous_hash TEXT NOT NULL DEFAULT ''")
        if "integrity_hash" not in audit_columns:
            conn.execute("ALTER TABLE audit_events ADD COLUMN integrity_hash TEXT NOT NULL DEFAULT ''")
        query_columns = {row["name"] for row in conn.execute("PRAGMA table_info(query_traces)").fetchall()}
        if "metadata_json" not in query_columns:
            conn.execute("ALTER TABLE query_traces ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        if "otel_trace_id" not in query_columns:
            conn.execute("ALTER TABLE query_traces ADD COLUMN otel_trace_id TEXT NOT NULL DEFAULT ''")
        if "otel_span_id" not in query_columns:
            conn.execute("ALTER TABLE query_traces ADD COLUMN otel_span_id TEXT NOT NULL DEFAULT ''")
        if "turn_id" not in query_columns:
            conn.execute("ALTER TABLE query_traces ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_query_traces_otel_trace ON query_traces(otel_trace_id)")

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
        department_id: int | None = None,
    ) -> int:
        with closing(self._connect()) as conn:
            created_at = utc_now()
            actor_department_id = actor.department_id if actor else department_id
            metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
            previous_hash = self._last_audit_hash(conn)
            integrity_hash = audit_event_hash(
                previous_hash,
                {
                    "actor_user_id": actor.id if actor else None,
                    "actor_username": actor.username if actor else "",
                    "actor_role": actor.role if actor else "",
                    "department_id": actor_department_id,
                    "action": action,
                    "target_type": target_type,
                    "target_id": str(target_id or ""),
                    "kb_name": kb_name or "",
                    "success": 1 if success else 0,
                    "error_message": error_message or "",
                    "metadata_json": metadata_json,
                    "created_at": created_at,
                },
            )
            cursor = conn.execute(
                """
                INSERT INTO audit_events (
                    actor_user_id, actor_username, actor_role, department_id,
                    action, target_type, target_id, kb_name, success,
                    error_message, metadata_json, previous_hash, integrity_hash, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor.id if actor else None,
                    actor.username if actor else "",
                    actor.role if actor else "",
                    actor_department_id,
                    action,
                    target_type,
                    str(target_id or ""),
                    kb_name or "",
                    1 if success else 0,
                    error_message or "",
                    metadata_json,
                    previous_hash,
                    integrity_hash,
                    created_at,
                ),
            )
        return int(cursor.lastrowid)

    def _last_audit_hash(self, conn) -> str:
        row = conn.execute(
            """
            SELECT integrity_hash
            FROM audit_events
            WHERE integrity_hash != ''
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return row["integrity_hash"] if row else ""

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
        retriever_type: str = "",
        final_top_k: int | None = None,
        latency_ms: int | None = None,
        status: str = "success",
        error_message: str = "",
        metadata: dict[str, Any] | None = None,
        otel_trace_id: str = "",
        otel_span_id: str = "",
        turn_id: str = "",
    ) -> int:
        effective_backend = backend or "ragflow"
        effective_retriever_type = retriever_type or "ragflow_retrieval"

        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO query_traces (
                    user_id, username, department_id, chat_session_id,
                    user_message_id, assistant_message_id, kb_name,
                    original_query, rewritten_query, backend, retriever_type,
                    final_top_k, latency_ms,
                    status, error_message, metadata_json, otel_trace_id, otel_span_id, turn_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    effective_backend,
                    effective_retriever_type,
                    final_top_k,
                    latency_ms,
                    status,
                    error_message or "",
                    json.dumps(metadata or {}, ensure_ascii=False),
                    otel_trace_id or "",
                    otel_span_id or "",
                    turn_id or "",
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def record_retrieved_evidence(self, trace_id: int, evidence: list[dict]) -> None:
        """批量写入一次查询命中的证据行（retrieved_evidence 表）。

        evidence 来自 runner 的 merged_evidence（已去重排序），每项是 agent
        Evidence 的扁平 dict。composite score 进 rerank_score；vector/bm25/rrf
        在合并阶段已丢失，留 NULL。
        """
        if not evidence:
            return
        rows = []
        for rank, item in enumerate(evidence, start=1):
            metadata = item.get("metadata") or {}
            locator = item.get("locator") or {}
            document_id = locator.get("document_id") or metadata.get("ragflow_document_id") or ""
            chunk_id = item.get("id") or locator.get("chunk_id") or ""
            score = item.get("score")
            try:
                rerank_score = float(score) if score is not None else None
            except (TypeError, ValueError):
                rerank_score = None
            rows.append(
                (
                    trace_id,
                    rank,
                    item.get("source_name") or "",
                    str(document_id or ""),
                    str(chunk_id or ""),
                    None,  # vector_score
                    None,  # bm25_score
                    None,  # rrf_score
                    rerank_score,
                    (item.get("content") or "")[:200],
                    json.dumps(
                        {
                            "content_kind": item.get("content_kind") or metadata.get("content_kind") or "",
                            "processor_kind": item.get("processor_kind") or metadata.get("processor_kind") or "",
                            "locator": locator,
                        },
                        ensure_ascii=False,
                    ),
                    utc_now(),
                )
            )
        with closing(self._connect()) as conn:
            conn.executemany(
                """
                INSERT INTO retrieved_evidence (
                    trace_id, rank, file_name, document_id, chunk_id,
                    vector_score, bm25_score, rrf_score, rerank_score,
                    text_preview, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def list_audit_events(
        self,
        viewer: AuthUser,
        action: str | None = None,
        kb_name: str | None = None,
        success: bool | None = None,
        keyword: str | None = None,
        limit: int = 200,
    ) -> list[AuditEvent]:
        where, params = _audit_where(viewer, action, kb_name, success, keyword)
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

    def list_audit_actions(self, viewer: AuthUser, limit: int = 200) -> list[str]:
        where, params = scoped_where(viewer)
        params.append(max(1, min(limit, 1000)))
        sql = f"""
            SELECT DISTINCT action
            FROM audit_events
            WHERE {' AND '.join(where)}
            ORDER BY action
            LIMIT ?
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row["action"] for row in rows if row["action"]]

    def audit_integrity_status(self, limit: int = 5000) -> dict[str, Any]:
        sql = """
            SELECT *
            FROM audit_events
            ORDER BY id ASC
            LIMIT ?
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, (max(1, min(limit, 20000)),)).fetchall()

        previous_hash = ""
        checked = 0
        legacy = 0
        for row in rows:
            current_hash = row["integrity_hash"]
            if not current_hash:
                legacy += 1
                continue
            if row["previous_hash"] != previous_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "legacy": legacy,
                    "broken_id": int(row["id"]),
                    "reason": "previous_hash mismatch",
                }
            expected = audit_event_hash(row["previous_hash"], audit_hash_fields(row))
            if expected != current_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "legacy": legacy,
                    "broken_id": int(row["id"]),
                    "reason": "integrity_hash mismatch",
                }
            checked += 1
            previous_hash = current_hash
        return {"ok": True, "checked": checked, "legacy": legacy, "broken_id": None, "reason": ""}

    def list_query_traces(
        self,
        viewer: AuthUser,
        kb_name: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 200,
    ) -> list[QueryTrace]:
        where, params = _query_where(viewer, kb_name, status, keyword)
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
        traces = [row_to_query_trace(row) for row in rows]
        # 非 owner 全脱敏：管理员看他人查询时原文/改写/错误均隐藏；看自己的不脱敏。
        return [redact_query_trace(t) if t.user_id != viewer.id else t for t in traces]

    def list_evidence(self, viewer: AuthUser, trace_id: int) -> list[RetrievedEvidence]:
        with closing(self._connect()) as conn:
            trace = conn.execute("SELECT * FROM query_traces WHERE id = ?", (trace_id,)).fetchone()
            if not trace or not can_view_row(viewer, trace["user_id"], trace["department_id"]):
                return []
            owner_id = trace["user_id"]
            rows = conn.execute(
                """
                SELECT *
                FROM retrieved_evidence
                WHERE trace_id = ?
                ORDER BY rank
                """,
                (trace_id,),
            ).fetchall()
        evidence = [row_to_evidence(row) for row in rows]
        # 非 owner 只能看到证据的来源/分数，正文 preview 隐藏。
        return [redact_evidence(item) if owner_id != viewer.id else item for item in evidence]

    # ---------- 全量统计 / 聚合（无 LIMIT，均走 _audit_where / _query_where，强制 scoped） ----------

    def count_audit_events(
        self,
        viewer: AuthUser,
        action: str | None = None,
        kb_name: str | None = None,
        success: bool | None = None,
        keyword: str | None = None,
    ) -> int:
        where, params = _audit_where(viewer, action, kb_name, success, keyword)
        sql = f"SELECT COUNT(*) FROM audit_events WHERE {' AND '.join(where)}"
        with closing(self._connect()) as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def audit_breakdown(
        self,
        viewer: AuthUser,
        action: str | None = None,
        kb_name: str | None = None,
        success: bool | None = None,
        keyword: str | None = None,
    ) -> dict[str, int]:
        """当前筛选下按 success 分组：{"success": n, "failed": n}。"""
        where, params = _audit_where(viewer, action, kb_name, success, keyword)
        sql = f"SELECT success, COUNT(*) FROM audit_events WHERE {' AND '.join(where)} GROUP BY success"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        result = {"success": 0, "failed": 0}
        for row in rows:
            if int(row[0]) == 1:
                result["success"] = int(row[1])
            else:
                result["failed"] += int(row[1])
        return result

    def audit_action_breakdown(
        self,
        viewer: AuthUser,
        kb_name: str | None = None,
        success: bool | None = None,
        keyword: str | None = None,
        limit: int = 50,
    ) -> list[tuple[str, int]]:
        """当前筛选下按 action 分组计数（不含 action 过滤本身），按 count 降序。部门管理员只看本部门。"""
        where, params = _audit_where(viewer, None, kb_name, success, keyword)
        params.append(max(1, min(limit, 200)))
        sql = (
            f"SELECT action, COUNT(*) FROM audit_events "
            f"WHERE {' AND '.join(where)} GROUP BY action ORDER BY COUNT(*) DESC, action ASC LIMIT ?"
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(row[0], int(row[1])) for row in rows if row[0]]

    def audit_recent_daily(self, viewer: AuthUser, days: int = 7) -> list[tuple[str, int]]:
        """近 N 日每日审计事件计数（按本地日期分组），部门管理员只看本部门。

        created_at 存的是 UTC ISO 串；用 SQLite 的 substr 取日期前 10 位即可按日聚合。
        """
        where, params = scoped_where(viewer)
        params.append(days)
        sql = (
            f"SELECT substr(created_at, 1, 10) AS day, COUNT(*) "
            f"FROM audit_events WHERE {' AND '.join(where)} "
            f"AND created_at >= datetime('now', '-' || ? || ' days') "
            f"GROUP BY day ORDER BY day ASC"
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(row[0], int(row[1])) for row in rows]

    def count_query_traces(
        self,
        viewer: AuthUser,
        kb_name: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> int:
        where, params = _query_where(viewer, kb_name, status, keyword)
        sql = f"SELECT COUNT(*) FROM query_traces WHERE {' AND '.join(where)}"
        with closing(self._connect()) as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def query_status_breakdown(
        self,
        viewer: AuthUser,
        kb_name: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, int]:
        """当前筛选下按 status 分组：success/failed/partial/no_evidence。"""
        where, params = _query_where(viewer, kb_name, status, keyword)
        sql = f"SELECT status, COUNT(*) FROM query_traces WHERE {' AND '.join(where)} GROUP BY status"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        result = {"success": 0, "failed": 0, "partial": 0, "no_evidence": 0}
        for row in rows:
            key = row[0] if row[0] in result else "failed"
            result[key] = int(row[1])
        return result

    def query_failure_top(
        self,
        viewer: AuthUser,
        kb_name: str | None = None,
        keyword: str | None = None,
        limit: int = 5,
    ) -> list[tuple[str, int]]:
        """失败查询的错误原因 Top-N（error_message 截断后分组），按 count 降序。部门管理员只看本部门。

        固定 status='failed'，不含 status 过滤参数（避免与“只看失败”语义冲突）。
        """
        where, params = _query_where(viewer, kb_name, "failed", keyword)
        params.append(max(1, min(limit, 20)))
        sql = (
            f"SELECT substr(error_message, 1, 120) AS reason, COUNT(*) "
            f"FROM query_traces WHERE {' AND '.join(where)} "
            f"GROUP BY reason ORDER BY COUNT(*) DESC, reason ASC LIMIT ?"
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(row[0] or "(空)", int(row[1])) for row in rows]



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


def _audit_where(
    viewer: AuthUser,
    action: str | None = None,
    kb_name: str | None = None,
    success: bool | None = None,
    keyword: str | None = None,
) -> tuple[list[str], list[Any]]:
    """审计日志的 WHERE 构造，list / count / 聚合共用，保证“看见的行 = 统计的行”。

    第一行强制 scoped_where：任何调用都必须带 viewer，跨范围查询在结构上不可能发生。
    """
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
        where.append(
            "(actor_username LIKE ? OR target_id LIKE ? OR error_message LIKE ? OR metadata_json LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    return where, params


def _query_where(
    viewer: AuthUser,
    kb_name: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> tuple[list[str], list[Any]]:
    """查询日志的 WHERE 构造，list / count / 聚合共用。第一行强制 scoped_where。"""
    where, params = scoped_where(viewer, user_column="user_id")
    if kb_name:
        where.append("kb_name = ?")
        params.append(kb_name)
    if status:
        where.append("status = ?")
        params.append(status)
    if keyword:
        like = f"%{keyword}%"
        if viewer.role == ROLE_USER:
            where.append(
                "(username LIKE ? OR kb_name LIKE ? OR original_query LIKE ? "
                "OR rewritten_query LIKE ? OR error_message LIKE ? OR metadata_json LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])
        else:
            # 管理员能看他人查询状态，但不能用原文/错误/metadata 做侧信道探测。
            where.append("(username LIKE ? OR kb_name LIKE ? OR backend LIKE ? OR retriever_type LIKE ?)")
            params.extend([like, like, like, like])
    return where, params


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
        previous_hash=row["previous_hash"] if "previous_hash" in row.keys() else "",
        integrity_hash=row["integrity_hash"] if "integrity_hash" in row.keys() else "",
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
        final_top_k=row["final_top_k"],
        latency_ms=row["latency_ms"],
        status=row["status"],
        error_message=row["error_message"],
        metadata_json=row["metadata_json"],
        created_at=row["created_at"],
        otel_trace_id=row["otel_trace_id"] if "otel_trace_id" in row.keys() else "",
        otel_span_id=row["otel_span_id"] if "otel_span_id" in row.keys() else "",
        turn_id=row["turn_id"] if "turn_id" in row.keys() else "",
    )


def redact_query_trace(trace: QueryTrace) -> QueryTrace:
    return QueryTrace(
        id=trace.id,
        user_id=trace.user_id,
        username=trace.username,
        department_id=trace.department_id,
        chat_session_id=trace.chat_session_id,
        user_message_id=trace.user_message_id,
        assistant_message_id=trace.assistant_message_id,
        kb_name=trace.kb_name,
        original_query="[redacted]",
        rewritten_query="[redacted]" if trace.rewritten_query else "",
        backend=trace.backend,
        retriever_type=trace.retriever_type,
        final_top_k=trace.final_top_k,
        latency_ms=trace.latency_ms,
        status=trace.status,
        # 失败信息里常带查询上下文片段（如 RAGFlow 把原问题拼进 error），
        # 非 owner 脱敏原文的同时也必须脱敏 error_message，否则等于漏点。
        error_message=trace.error_message if not trace.error_message else "[redacted]",
        metadata_json=redact_query_metadata_json(trace.metadata_json),
        created_at=trace.created_at,
        otel_trace_id=trace.otel_trace_id,
        otel_span_id=trace.otel_span_id,
        turn_id=trace.turn_id,
    )


def redact_query_metadata_json(raw: str) -> str:
    try:
        metadata = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return "{}"
    if not isinstance(metadata, dict):
        return "{}"

    safe_keys = {
        "ragflow_similarity_threshold",
        "ragflow_vector_weight",
        "ragflow_top_k",
        "ragflow_governance_dataset",
        "ragflow_design_dataset",
        "agent_status",
        "error_stage",
        "retrieval_rounds",
        "sufficiency_status",
    }
    redacted = {key: metadata.get(key) for key in safe_keys if key in metadata}

    diagnostics = metadata.get("tool_diagnostics") or []
    if isinstance(diagnostics, list):
        redacted["tool_diagnostics"] = [
            {
                "tool_name": item.get("tool_name") or "",
                "status": item.get("status") or "",
                "hit_count": item.get("hit_count"),
                "top_k": item.get("top_k"),
                "scoped": bool(item.get("filters")),
            }
            for item in diagnostics
            if isinstance(item, dict)
        ]

    trace = metadata.get("agent_trace") or []
    if isinstance(trace, list):
        redacted["agent_trace"] = [
            {
                "node": item.get("node") or "",
                "status": (item.get("metadata") or {}).get("status") if isinstance(item.get("metadata"), dict) else "",
                "round": (item.get("metadata") or {}).get("round") if isinstance(item.get("metadata"), dict) else "",
            }
            for item in trace
            if isinstance(item, dict)
        ]

    if "missing" in metadata:
        redacted["missing_count"] = len(metadata.get("missing") or [])
    if metadata.keys() - redacted.keys():
        redacted["redacted"] = True
    return json.dumps(redacted, ensure_ascii=False)


def audit_hash_fields(row) -> dict[str, Any]:
    return {
        "actor_user_id": row["actor_user_id"],
        "actor_username": row["actor_username"],
        "actor_role": row["actor_role"],
        "department_id": row["department_id"],
        "action": row["action"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "kb_name": row["kb_name"],
        "success": row["success"],
        "error_message": row["error_message"],
        "metadata_json": row["metadata_json"],
        "created_at": row["created_at"],
    }


def audit_event_hash(previous_hash: str, fields: dict[str, Any]) -> str:
    payload = {"previous_hash": previous_hash, **fields}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def redact_evidence(item: RetrievedEvidence) -> RetrievedEvidence:
    return RetrievedEvidence(
        id=item.id,
        trace_id=item.trace_id,
        rank=item.rank,
        file_name=item.file_name,
        document_id=item.document_id,
        chunk_id=item.chunk_id,
        vector_score=item.vector_score,
        bm25_score=item.bm25_score,
        rrf_score=item.rrf_score,
        rerank_score=item.rerank_score,
        text_preview="[redacted]",
        metadata_json=item.metadata_json,
        created_at=item.created_at,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_local_time(created_at: str) -> str:
    """日志 created_at 存的是 UTC ISO 串，转成本地时间显示，避免用户误读时差。"""
    if not created_at:
        return "-"
    text = created_at.strip()
    try:
        # 优先按 ISO 解析（utc_now() 产出带 tz 的 isoformat）
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # 兼容 SQLite CURRENT_TIMESTAMP 产出（"YYYY-MM-DD HH:MM:SS[.ffffff]"，无 tz，按 UTC）
        try:
            naive = datetime.fromisoformat(text.split(".")[0]) if "." in text else datetime.fromisoformat(text)
        except ValueError:
            return text
        dt = naive.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
