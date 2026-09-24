"""Per-KB SQLite index + keyword search over external conversations.

Layout mirrors the spreadsheet index service: one ``index.db`` per
``departments/{dept}/kbs/{kb}`` scope. Search is LIKE-based with hit-count
relevance (same approach as the spreadsheet agent tools) and every query is
pinned to a department so same-named KBs never cross.
"""

from __future__ import annotations

import os
import json
import sqlite3
from contextlib import closing

import src.settings
from src.ingestion.kb_paths import safe_child_path, validate_kb_name
from src.external_conversations.models import ExternalConversation


def _require_department_id(department_id: str | int | None, action: str) -> str:
    if department_id in (None, ""):
        raise ValueError(f"department_id is required when {action} external conversation index")
    return str(department_id)


_DOMAIN_QUERY_TERMS = (
    "电压",
    "电流",
    "功耗",
    "频率",
    "接口",
    "引脚",
    "连接",
    "复位",
    "阈值",
    "模块",
    "芯片",
    "型号",
    "压差",
    "波特率",
    "时序",
)
_CJK_STOP_CHARS = frozenset("的是什么有哪些列出如何这该项目前中与和及到从为将或能否可以是否请问多少几了用")
_CJK_NOISY_NGRAMS = frozenset({"电电", "围使", "存容"})


def _tokens(query: str) -> list[str]:
    value = str(query or "")
    lowered = value.casefold()
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        normalized = token.strip().casefold()
        if len(normalized) < 2 or normalized in seen:
            return
        seen.add(normalized)
        tokens.append(normalized)

    # ASCII runs embedded in Chinese text (e.g. "…的token是怎么…") must be
    # extracted as standalone searchable terms; whitespace splitting misses them.
    import re

    for token in re.findall(r"[A-Za-z][A-Za-z0-9._+-]{1,31}", value):
        add(token)
    for token in value.split():
        add(token)
    for term in _DOMAIN_QUERY_TERMS:
        if term.casefold() in lowered:
            add(term)
    for block in _cjk_blocks(value):
        for index in range(0, len(block) - 1):
            token = block[index : index + 2]
            if (
                len(set(token)) > 1
                and token not in _CJK_NOISY_NGRAMS
                and not any(char in _CJK_STOP_CHARS for char in token)
            ):
                add(token)
    return tokens[:24]


def _cjk_blocks(value: str) -> list[str]:
    import re

    return re.findall(r"[\u4e00-\u9fff]{2,}", value)


def _like_clauses(columns: list[str], tokens: list[str]) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for token in tokens:
        token_clauses = [f"LOWER({column}) LIKE ?" for column in columns]
        clauses.append("(" + " OR ".join(token_clauses) + ")")
        params.extend(f"%{token}%" for _ in columns)
    if not clauses:
        return "1=1", []
    return "(" + " OR ".join(clauses) + ")", params


def _token_match_order(columns: list[str], tokens: list[str]) -> tuple[str, list[str]]:
    expressions: list[str] = []
    params: list[str] = []
    for token in tokens:
        expressions.append(
            "(CASE WHEN " + " OR ".join(f"LOWER({column}) LIKE ?" for column in columns) + " THEN 1 ELSE 0 END)"
        )
        params.extend(f"%{token}%" for _ in columns)
    return " + ".join(expressions) or "0", params


_ROLE_WEIGHT_SQL = "(CASE WHEN m.role = 'assistant' THEN 3 WHEN m.role = 'user' THEN 2 ELSE 1 END)"


class ExternalConversationQueryEngine:
    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(src.settings.STORAGE_DIR, "external_conversations")

    # ---- paths / schema -------------------------------------------------
    def db_path(self, department_id: str | int | None, kb_name: str, create: bool = True) -> str:
        dept = _require_department_id(department_id, "opening")
        scope = safe_child_path(
            os.path.abspath(self.root),
            "departments",
            dept,
            "kbs",
            validate_kb_name(kb_name),
            create=create,
        )
        return os.path.join(scope, "index.db")

    def _connect(self, db_path: str, create: bool) -> sqlite3.Connection | None:
        if not create and not os.path.exists(db_path):
            return None
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                department_id TEXT NOT NULL DEFAULT '',
                kb_id INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'upload',
                source_group TEXT NOT NULL DEFAULT '',
                turn_count INTEGER NOT NULL DEFAULT 0,
                block_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'indexed',
                content_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                key_points_json TEXT NOT NULL DEFAULT '[]',
                summary_generated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                department_id TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                turn_index INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL DEFAULT '',
                start_offset INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
            """
        )
        # additive migration for index.db files created before summaries existed
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if "summary" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        if "key_points_json" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN key_points_json TEXT NOT NULL DEFAULT '[]'")
        if "summary_generated_at" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN summary_generated_at TEXT NOT NULL DEFAULT ''")
        return conn

    # ---- indexing --------------------------------------------------------
    def index_conversation(self, conversation: ExternalConversation) -> None:
        db_path = self.db_path(conversation.department_id, conversation.kb_name, create=True)
        with closing(self._connect(db_path, True)) as conn:
            with conn:
                conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation.conversation_id,))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conversations
                    (conversation_id, department_id, kb_id, title, source_file, origin,
                     source_group, turn_count, block_count, status, content_hash, created_at,
                     summary, key_points_json, summary_generated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation.conversation_id,
                        conversation.department_id,
                        conversation.kb_id,
                        conversation.title,
                        conversation.source_file,
                        conversation.origin,
                        conversation.source_group,
                        len(conversation.turns),
                        len(conversation.blocks),
                        "indexed",
                        conversation.content_hash,
                        conversation.created_at,
                        conversation.summary,
                        json.dumps(conversation.key_points, ensure_ascii=False),
                        conversation.summary_generated_at,
                    ),
                )
                rows = [
                    (
                        conversation.conversation_id,
                        conversation.department_id,
                        turn.role,
                        index,
                        turn.content,
                        turn.ts,
                        turn.start_offset,
                    )
                    for index, turn in enumerate(conversation.turns)
                ]
                if not rows:
                    rows = [
                        (
                            conversation.conversation_id,
                            conversation.department_id,
                            "document",
                            index,
                            block,
                            "",
                            0,
                        )
                        for index, block in enumerate(conversation.blocks)
                    ]
                conn.executemany(
                    """
                    INSERT INTO messages
                    (conversation_id, department_id, role, turn_index, content, ts, start_offset)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def rebuild_kb(self, store, department_id: str, kb_name: str) -> dict:
        """Recreate this scope's index from the store's json files (fail-soft)."""
        rebuilt = 0
        failed: list[dict] = []
        for conversation in store.list_conversations(department_id, kb_name):
            try:
                self.index_conversation(conversation)
                rebuilt += 1
            except Exception as exc:  # fail-soft per conversation
                failed.append({"conversation_id": conversation.conversation_id, "error": str(exc)})
        return {"rebuilt": rebuilt, "failed": failed}

    # ---- queries ---------------------------------------------------------
    def search_by_scope(self, department_id: str | int | None, kb_name: str, query: str, top_k: int = 5) -> list[dict]:
        dept = _require_department_id(department_id, "searching")
        try:
            db_path = self.db_path(dept, kb_name, create=False)
        except ValueError:
            return []
        if not os.path.exists(db_path):
            return []
        tokens = _tokens(query)
        if not tokens:
            return []
        where, where_params = _like_clauses(["m.content", "c.title", "c.source_file"], tokens)
        order_sql, order_params = _token_match_order(["m.content", "c.title"], tokens)
        limit = min(200, max(40, max(1, int(top_k)) * 8))
        params = [dept, *where_params, *order_params, limit]
        sql = f"""
            SELECT m.id AS message_id, m.conversation_id, m.role, m.turn_index,
                   m.content, m.ts, m.start_offset,
                   c.title, c.source_file, c.origin, c.source_group
            FROM messages m
            JOIN conversations c ON c.conversation_id = m.conversation_id
                 AND c.department_id = ?
            WHERE {where}
            ORDER BY {order_sql} DESC, {_ROLE_WEIGHT_SQL} DESC, m.id ASC
            LIMIT ?
        """
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return []
        with closing(conn):
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                return []

        def relevance(row) -> tuple:
            searchable = f"{row['content'] or ''}\n{row['title'] or ''}".casefold()
            matched = {t for t in tokens if t in searchable}
            role_weight = {"assistant": 3, "user": 2}.get(row["role"], 1)
            return (len(matched), sum(len(t) for t in matched), role_weight, -int(row["message_id"]))

        ranked = sorted(rows, key=relevance, reverse=True)
        return [dict(row) for row in ranked[: max(1, int(top_k))]]

    def list_conversations(self, department_id: str | int | None, kb_name: str) -> list[dict]:
        dept = _require_department_id(department_id, "listing")
        try:
            db_path = self.db_path(dept, kb_name, create=False)
        except ValueError:
            return []
        if not os.path.exists(db_path):
            return []
        sql = """
            SELECT conversation_id, title, source_file, origin, source_group,
                   turn_count, block_count, status, created_at,
                   summary, key_points_json, summary_generated_at
            FROM conversations WHERE department_id = ? ORDER BY created_at DESC, conversation_id ASC
        """
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return []
        with closing(conn):
            try:
                rows = []
                for raw in conn.execute(sql, (dept,)).fetchall():
                    row = dict(raw)
                    try:
                        row["key_points"] = json.loads(row.pop("key_points_json") or "[]")
                    except (TypeError, json.JSONDecodeError):
                        row["key_points"] = []
                    rows.append(row)
                return rows
            except sqlite3.Error:
                return []

    def update_summary(
        self,
        department_id: str | int | None,
        kb_name: str,
        conversation_id: str,
        summary: str,
        key_points: list[str],
        generated_at: str,
    ) -> bool:
        """Persist AI-derived extraction for one conversation."""
        dept = _require_department_id(department_id, "updating")
        try:
            db_path = self.db_path(dept, kb_name, create=False)
        except ValueError:
            return False
        if not os.path.exists(db_path):
            return False
        with closing(sqlite3.connect(db_path, timeout=30)) as conn:
            with conn:
                cursor = conn.execute(
                    "UPDATE conversations SET summary = ?, key_points_json = ?, summary_generated_at = ? "
                    "WHERE conversation_id = ? AND department_id = ?",
                    (summary, json.dumps(key_points, ensure_ascii=False), generated_at, conversation_id, dept),
                )
                return cursor.rowcount > 0

    def get_conversation(self, department_id: str | int | None, kb_name: str, conversation_id: str) -> dict | None:
        listing = {row["conversation_id"]: row for row in self.list_conversations(department_id, kb_name)}
        meta = listing.get(str(conversation_id))
        return meta

    def delete_conversation(self, department_id: str | int | None, kb_name: str, conversation_id: str) -> bool:
        dept = _require_department_id(department_id, "deleting")
        try:
            db_path = self.db_path(dept, kb_name, create=False)
        except ValueError:
            return False
        if not os.path.exists(db_path):
            return False
        with closing(sqlite3.connect(db_path, timeout=30)) as conn:
            with conn:
                cursor = conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
                removed = conn.execute(
                    "DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,)
                ).rowcount
            return bool(removed or cursor.rowcount)

    def delete_kb(self, department_id: str | int | None, kb_name: str) -> bool:
        try:
            db_path = self.db_path(department_id, kb_name, create=False)
        except ValueError:
            return False
        if os.path.exists(db_path):
            os.remove(db_path)
            return True
        return False
