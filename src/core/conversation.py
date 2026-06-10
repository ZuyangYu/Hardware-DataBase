import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

import config.settings


@dataclass
class ChatSession:
    id: int
    user_id: int
    kb_name: str
    title: str
    created_at: str
    updated_at: str


@dataclass
class ChatMessage:
    id: int
    session_id: int
    role: str
    content: str
    created_at: str


class ConversationService:
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
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_kb
                ON chat_sessions(user_id, kb_name, updated_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_messages_session
                ON chat_messages(session_id, id)
            """)

    def list_sessions(self, user_id: int, kb_name: str | None = None) -> list[ChatSession]:
        with closing(self._connect()) as conn:
            if kb_name:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM chat_sessions
                    WHERE user_id = ? AND kb_name = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (user_id, kb_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM chat_sessions
                    WHERE user_id = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (user_id,),
                ).fetchall()
        return [row_to_session(row) for row in rows]

    def get_session(self, user_id: int, session_id: int) -> ChatSession | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
        return row_to_session(row) if row else None

    def get_or_create_session(self, user_id: int, kb_name: str, session_id: int | None = None) -> ChatSession:
        if session_id is not None:
            session = self.get_session(user_id, session_id)
            if session and session.kb_name == kb_name:
                return session

        sessions = self.list_sessions(user_id, kb_name)
        if sessions:
            return sessions[0]
        return self.create_session(user_id, kb_name)

    def create_session(self, user_id: int, kb_name: str, title: str = "新对话") -> ChatSession:
        now = utc_now()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_sessions (user_id, kb_name, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, kb_name, title, now, now),
            )
            row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return row_to_session(row)

    def add_message(self, user_id: int, session_id: int, role: str, content: str) -> ChatMessage:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("message role must be user, assistant, or system")
        if not self.get_session(user_id, session_id):
            raise PermissionError("chat session does not belong to current user")

        now = utc_now()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, content, now),
            )
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ? AND user_id = ?",
                (now, session_id, user_id),
            )
            if role == "user":
                title = content.strip().replace("\n", " ")[:32] or "新对话"
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET title = ?
                    WHERE id = ? AND user_id = ? AND title = '新对话'
                    """,
                    (title, session_id, user_id),
                )
            row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return row_to_message(row)

    def list_messages(self, user_id: int, session_id: int) -> list[ChatMessage]:
        if not self.get_session(user_id, session_id):
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        return [row_to_message(row) for row in rows]

    def clear_session(self, user_id: int, session_id: int):
        if not self.get_session(user_id, session_id):
            return
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE chat_sessions SET title = '新对话', updated_at = ? WHERE id = ? AND user_id = ?",
                (now, session_id, user_id),
            )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_session(row) -> ChatSession:
    return ChatSession(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        kb_name=row["kb_name"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_message(row) -> ChatMessage:
    return ChatMessage(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
    )
