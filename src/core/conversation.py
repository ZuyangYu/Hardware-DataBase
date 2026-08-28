import os
import json
import hashlib
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import config.settings


@dataclass
class ChatSession:
    id: int
    user_id: int
    kb_name: str
    department_id: int | None
    kb_id: int | None
    title: str
    created_at: str
    updated_at: str


@dataclass
class ChatMessage:
    id: int
    session_id: int
    role: str
    content: str
    footer: str
    created_at: str
    edited_at: str | None = None
    redacted: bool = False
    memory_context: list[dict] = field(default_factory=list)


@dataclass
class ChatTurn:
    id: str
    session_id: int
    user_message_id: int
    assistant_message_id: int
    kb_name: str
    department_id: int | None
    kb_id: int | None
    query: str
    query_mode: str
    status: str
    client_request_id: str | None
    cancel_requested: bool
    last_event_seq: int
    answer: str
    summary: dict
    footer: str
    metrics: dict
    trace_context: dict[str, str]
    error_message: str
    worker_id: str
    worker_heartbeat_at: str | None
    retry_count: int
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass
class ChatTurnEvent:
    turn_id: str
    seq: int
    event_type: str
    payload: dict
    created_at: str


class ConversationService:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.settings.AUTH_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

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
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL,
                    department_id INTEGER,
                    kb_id INTEGER,
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_turns (
                    id TEXT PRIMARY KEY,
                    session_id INTEGER NOT NULL,
                    user_message_id INTEGER NOT NULL,
                    assistant_message_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL,
                    department_id INTEGER,
                    kb_id INTEGER,
                    query TEXT NOT NULL,
                    query_mode TEXT NOT NULL DEFAULT 'fast',
                    status TEXT NOT NULL,
                    client_request_id TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    answer TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    footer TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    trace_context_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    worker_heartbeat_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
                    FOREIGN KEY(assistant_message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_turns_session_request
                ON chat_turns(session_id, client_request_id)
                WHERE client_request_id IS NOT NULL
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_turns_session_status
                ON chat_turns(session_id, status, created_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_turn_events (
                    turn_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(turn_id, seq),
                    FOREIGN KEY(turn_id) REFERENCES chat_turns(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_message_edits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    editor_user_id INTEGER NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('edit', 'redact')),
                    previous_content TEXT,
                    previous_content_hash TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
                )
            """)
            self._ensure_column(conn, "chat_sessions", "department_id", "INTEGER")
            self._ensure_column(conn, "chat_sessions", "kb_id", "INTEGER")
            self._ensure_column(conn, "chat_turns", "department_id", "INTEGER")
            self._ensure_column(conn, "chat_turns", "kb_id", "INTEGER")
            self._ensure_column(conn, "chat_turns", "worker_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "chat_turns", "worker_heartbeat_at", "TEXT")
            self._ensure_column(conn, "chat_turns", "retry_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_turns", "query_mode", "TEXT NOT NULL DEFAULT 'fast'")
            self._ensure_column(conn, "chat_turns", "metrics_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "chat_turns", "trace_context_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "chat_messages", "edited_at", "TEXT")
            self._ensure_column(conn, "chat_messages", "redacted", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_sessions", "auto_memory", "INTEGER NOT NULL DEFAULT 1")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_status_created ON chat_turns(status, created_at)")
            has_users = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ).fetchone()
            if has_users:
                # Backfill historical chats from their owning user. Once populated,
                # every read also enforces this department scope.
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET department_id = (SELECT department_id FROM users WHERE users.id = chat_sessions.user_id)
                    WHERE department_id IS NULL
                    """
                )
                conn.execute(
                    """
                    UPDATE chat_turns
                    SET department_id = (SELECT department_id FROM chat_sessions WHERE chat_sessions.id = chat_turns.session_id),
                        kb_id = (SELECT kb_id FROM chat_sessions WHERE chat_sessions.id = chat_turns.session_id)
                    WHERE department_id IS NULL
                    """
                )
            # Long-term memory control-plane tables share this SQLite
            # connection boundary with Conversation.  The import is deferred
            # to keep the auth/conversation modules independently importable.
            from src.memory.catalog import ensure_memory_schema

            ensure_memory_schema(conn)

    @staticmethod
    def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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
                """
                SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?
                """,
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

    def create_session(
        self,
        user_id: int,
        kb_name: str,
        title: str = "新对话",
        department_id: int | None = None,
        kb_id: int | None = None,
    ) -> ChatSession:
        now = utc_now()
        with closing(self._connect()) as conn:
            if department_id is None:
                owner = conn.execute("SELECT department_id FROM users WHERE id = ?", (user_id,)).fetchone()
                department_id = owner["department_id"] if owner else None
            cursor = conn.execute(
                """
                INSERT INTO chat_sessions (user_id, kb_name, department_id, kb_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, kb_name, department_id, kb_id, title, now, now),
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
                SELECT m.*, COALESCE(t.footer, '') AS footer,
                       COALESCE(t.summary_json, '{}') AS turn_summary
                FROM chat_messages m
                LEFT JOIN chat_turns t ON t.assistant_message_id = m.id
                WHERE m.session_id = ?
                ORDER BY m.id
                """,
                (session_id,),
            ).fetchall()
        return [row_to_message(row) for row in rows]

    def history_before_turn(self, user_id: int, turn_id: str, limit: int = 5) -> list[tuple[str, str]]:
        """Return canonical completed dialogue turns before a request turn."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT t.session_id, t.user_message_id FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = ? AND s.user_id = ?
                """,
                (turn_id, user_id),
            ).fetchone()
            if row is None:
                return []
            rows = conn.execute(
                """
                SELECT u.content AS user_content, a.content AS assistant_content
                FROM chat_turns previous
                JOIN chat_messages u ON u.id = previous.user_message_id
                JOIN chat_messages a ON a.id = previous.assistant_message_id
                WHERE previous.session_id = ? AND previous.user_message_id < ?
                  AND previous.status = 'completed'
                ORDER BY previous.user_message_id DESC
                LIMIT ?
                """,
                (row["session_id"], row["user_message_id"], max(1, min(limit, 20))),
            ).fetchall()
        return [(item["user_content"], item["assistant_content"]) for item in reversed(rows)]

    def clear_session(self, user_id: int, session_id: int):
        if not self.get_session(user_id, session_id):
            return
        now = utc_now()
        with closing(self._connect()) as conn:
            from src.memory.jobs import invalidate_session_memory_in_connection

            conn.execute("BEGIN IMMEDIATE")
            try:
                invalidate_session_memory_in_connection(conn, session_id, reason="session_cleared")
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
                conn.execute(
                    "UPDATE chat_sessions SET title = '新对话', updated_at = ? WHERE id = ? AND user_id = ?",
                    (now, session_id, user_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def delete_session(self, user_id: int, session_id: int) -> bool:
        """Hard-delete a chat session (messages cascade via FK).

        Returned bool tells the caller whether the row actually existed —
        used by the UI's "clear current chat" path so we don't carry the
        orphaned id around in session_state.
        """
        if not self.get_session(user_id, session_id):
            return False
        with closing(self._connect()) as conn:
            from src.memory.jobs import invalidate_session_memory_in_connection

            conn.execute("BEGIN IMMEDIATE")
            try:
                # Invalidate sources and freeze deletion outbox before the
                # chat rows cascade; memory_sources intentionally has no FK
                # cascade to raw messages.
                invalidate_session_memory_in_connection(conn, session_id, reason="session_deleted")
                conn.execute(
                    "DELETE FROM chat_sessions WHERE id = ? AND user_id = ?",
                    (session_id, user_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return True

    def edit_message(
        self,
        user_id: int,
        session_id: int,
        message_id: int,
        *,
        content: str | None = None,
        redact: bool = False,
        reason: str = "",
        request_id: str = "",
    ) -> ChatMessage:
        """Edit or redact a raw message with memory provenance protection.

        Per LangMem V2 §43 the old source must be invalidated and its hash
        frozen in the same transaction that writes the new content; derived
        memories queue rebuild/deletion before the new text is visible.
        """
        if not redact and not (content and content.strip()):
            raise ValueError("edit_message requires new content or redact=True")
        if redact:
            # Redaction removes user personal data; the replacement keeps the
            # message anchor without retaining the original payload.
            content = "[已脱敏]"

        from src.memory.jobs import invalidate_message_memory_in_connection
        from src.memory.catalog import ensure_memory_schema

        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ensure_memory_schema(conn)
                session = conn.execute(
                    "SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?",
                    (session_id, user_id),
                ).fetchone()
                if session is None:
                    raise PermissionError("chat session does not belong to current user")
                message = conn.execute(
                    "SELECT * FROM chat_messages WHERE id = ? AND session_id = ?",
                    (message_id, int(session["id"])),
                ).fetchone()
                if message is None:
                    raise KeyError("message not found")

                invalidate_message_memory_in_connection(
                    conn,
                    int(session["id"]),
                    int(message_id),
                    reason=reason or ("message_redacted" if redact else "message_edited"),
                )
                conn.execute(
                    """
                    INSERT INTO chat_message_edits
                        (message_id, editor_user_id, action, previous_content,
                         previous_content_hash, reason, request_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(message_id),
                        int(user_id),
                        "redact" if redact else "edit",
                        None if redact else str(message["content"]),
                        hashlib.sha256(str(message["content"]).encode("utf-8")).hexdigest(),
                        reason or "",
                        request_id or "",
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE chat_messages SET content = ?, edited_at = ?, redacted = ? WHERE id = ?
                    """,
                    (content, now, 1 if redact else 0, int(message_id)),
                )
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                    (now, int(session["id"])),
                )
                if int(session["auto_memory"] or 0) == 1:
                    self._enqueue_project_reflection_guarded(conn, session=session)
                row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (int(message_id),)).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return row_to_message(row)

    def _enqueue_project_reflection_guarded(
        self,
        conn: sqlite3.Connection,
        *,
        session: sqlite3.Row,
    ) -> None:
        """Enqueue a project reflection Job for the latest completed Turn.

        Used by ``edit_message`` so invalidated memories are rebuilt from the
        remaining valid sources without waiting for further chat activity.
        The caller owns the surrounding BEGIN IMMEDIATE transaction and the
        chat_sessions row must carry non-empty department/kb scope.
        """
        if not getattr(config.settings, "MEMORY_ENABLED", True) or not getattr(
            config.settings, "MEMORY_EXTRACTION_ENABLED", True
        ):
            return
        department_id = session["department_id"]
        kb_id = session["kb_id"]
        if department_id in (None, "") or kb_id in (None, ""):
            return
        turn_row = conn.execute(
            """
            SELECT id, assistant_message_id FROM chat_turns
            WHERE session_id = ? AND status = 'completed'
            ORDER BY finished_at DESC, created_at DESC LIMIT 1
            """,
            (int(session["id"]),),
        ).fetchone()
        if turn_row is None:
            return
        from src.memory.catalog import scope_fingerprint
        from src.memory.jobs import enqueue_project_reflection_in_connection

        debounce = max(0, int(getattr(config.settings, "MEMORY_DEBOUNCE_SECONDS", 300)))
        available_at = (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=debounce)
        ).isoformat()
        enqueue_project_reflection_in_connection(
            conn,
            session_id=int(session["id"]),
            scope_fingerprint=scope_fingerprint(scope="project", department_id=department_id, kb_id=kb_id),
            target_turn_id=str(turn_row["id"]),
            target_message_id=int(turn_row["assistant_message_id"]),
            available_at=available_at,
            force=True,
        )

    def get_session_memory_settings(self, user_id: int, session_id: int) -> dict[str, object]:
        if not self.get_session(user_id, session_id):
            raise PermissionError("chat session does not belong to current user")
        with closing(self._connect()) as conn:
            extracted = conn.execute(
                """
                SELECT COUNT(DISTINCT ms.memory_id) AS count
                FROM memory_sources ms
                JOIN memory_records mr ON mr.memory_id = ms.memory_id
                WHERE ms.session_id = ? AND ms.source_valid = 1
                  AND mr.status IN ('candidate', 'verified')
                """,
                (session_id,),
            ).fetchone()["count"]
            auto = conn.execute(
                "SELECT auto_memory FROM chat_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()["auto_memory"]
        return {"auto_extract_enabled": bool(auto), "extracted_memories": int(extracted)}

    def set_session_auto_extract(self, user_id: int, session_id: int, enabled: bool) -> dict[str, object]:
        """Toggle per-session automatic Project Memory extraction.

        Disabling cancels pending reflection work in the same transaction so
        no Job created under auto-memory survives an explicit opt-out.
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = conn.execute(
                    "SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?",
                    (session_id, user_id),
                ).fetchone()
                if session is None:
                    raise PermissionError("chat session does not belong to current user")
                conn.execute(
                    "UPDATE chat_sessions SET auto_memory = ?, updated_at = ? WHERE id = ?",
                    (1 if enabled else 0, utc_now(), int(session["id"])),
                )
                if not enabled:
                    conn.execute(
                        """UPDATE memory_jobs SET status = 'cancelled', generation = generation + 1,
                            last_error = 'session_auto_extract_disabled',
                            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                        WHERE session_id = ? AND job_kind = 'project_reflection'
                          AND status IN ('pending', 'retrying', 'running')""",
                        (int(session["id"]),),
                    )
                    audit_now = utc_now()
                    conn.execute(
                        "INSERT INTO memory_audit_events (audit_event_id, event_type, metadata_json, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            "session_auto_extract_disabled",
                            json.dumps({"session_id": int(session["id"])}),
                            audit_now,
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_session_memory_settings(user_id, session_id)

    def create_turn(
        self,
        user_id: int,
        session_id: int,
        query: str,
        client_request_id: str | None = None,
        query_mode: str = "fast",
        trace_context: dict[str, str] | None = None,
    ) -> ChatTurn:
        """Persist one user request and its assistant placeholder atomically.

        The idempotency key makes browser retries safe: the same request returns
        the original turn instead of incurring a second model invocation.
        """
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        request_id = (client_request_id or "").strip()[:128] or None
        query_mode = query_mode if query_mode in {"fast", "deep"} else "fast"
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = conn.execute(
                    """
                    SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if session is None:
                    raise PermissionError("chat session does not belong to current user")
                if request_id:
                    existing = conn.execute(
                        "SELECT * FROM chat_turns WHERE session_id = ? AND client_request_id = ?",
                        (session_id, request_id),
                    ).fetchone()
                    if existing is not None:
                        conn.execute("COMMIT")
                        return row_to_turn(existing)
                user_cursor = conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                    (session_id, query, now),
                )
                assistant_cursor = conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, 'assistant', '', ?)",
                    (session_id, now),
                )
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = ?, title = CASE WHEN title = '新对话' THEN ? ELSE title END WHERE id = ?",
                    (now, query.replace("\n", " ")[:32] or "新对话", session_id),
                )
                turn_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO chat_turns (
                        id, session_id, user_message_id, assistant_message_id, kb_name, query, query_mode,
                        department_id, kb_id, status, client_request_id, trace_context_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        turn_id,
                        session_id,
                        user_cursor.lastrowid,
                        assistant_cursor.lastrowid,
                        session["kb_name"],
                        query,
                        query_mode,
                        session["department_id"],
                        session["kb_id"],
                        request_id,
                        json.dumps(trace_context or {}, ensure_ascii=False),
                        now,
                    ),
                )
                row = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                conn.execute("COMMIT")
                return row_to_turn(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_turn(self, user_id: int, turn_id: str) -> ChatTurn | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = ? AND s.user_id = ?
                """,
                (turn_id, user_id),
            ).fetchone()
        return row_to_turn(row) if row else None

    def list_active_turns(self, user_id: int, session_id: int) -> list[ChatTurn]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.session_id = ? AND s.user_id = ?
                  AND t.status IN ('pending', 'streaming', 'cancelling')
                ORDER BY t.created_at
                """,
                (session_id, user_id),
            ).fetchall()
        return [row_to_turn(row) for row in rows]

    def requeue_stale_turns(self, stale_after_seconds: int | None = None) -> int:
        """Make abandoned worker claims available to an independent worker again."""
        stale_after_seconds = max(30, int(stale_after_seconds or config.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS))
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE chat_turns
                SET status = 'pending', worker_id = '', worker_heartbeat_at = NULL
                WHERE status IN ('streaming', 'cancelling')
                  AND worker_heartbeat_at IS NOT NULL
                  AND datetime(worker_heartbeat_at) < datetime('now', ?)
                  AND retry_count < 3
                """,
                (f"-{stale_after_seconds} seconds",),
            )
        return int(cursor.rowcount or 0)

    def list_pending_turn_work(self, limit: int = 8) -> list[tuple[ChatTurn, int]]:
        """Return queued work without claiming it; ``claim_turn`` remains atomic."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT t.*, s.user_id
                FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                JOIN users u ON u.id = s.user_id
                WHERE t.status = 'pending' AND t.cancel_requested = 0
                  AND u.is_active = 1
                ORDER BY t.created_at, t.id
                LIMIT ?
                """,
                (max(1, min(int(limit), 64)),),
            ).fetchall()
        return [(row_to_turn(row), int(row["user_id"])) for row in rows]

    def pending_turn_queue_state(self) -> tuple[int, float]:
        """Return durable chat queue depth and oldest-item age in seconds."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS depth, MIN(created_at) AS oldest_at
                FROM chat_turns
                WHERE status = 'pending' AND cancel_requested = 0
                """
            ).fetchone()
        depth = int(row["depth"] or 0)
        oldest_age = 0.0
        if row["oldest_at"]:
            try:
                oldest = datetime.fromisoformat(str(row["oldest_at"]))
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                oldest_age = max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds())
            except (TypeError, ValueError):
                oldest_age = 0.0
        return depth, oldest_age

    def claim_turn(
        self,
        user_id: int,
        turn_id: str,
        worker_id: str = "",
        stale_after_seconds: int | None = None,
    ) -> ChatTurn | None:
        """Claim a pending turn once; concurrent start requests are harmless."""
        now = utc_now()
        stale_after_seconds = max(30, int(stale_after_seconds or config.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT t.* FROM chat_turns t JOIN chat_sessions s ON s.id = t.session_id
                    WHERE t.id = ? AND s.user_id = ?
                    """,
                    (turn_id, user_id),
                ).fetchone()
                # A process may have died after claiming the turn. Requeue only
                # after a conservative heartbeat timeout; a live worker keeps
                # touching the row as it emits stages and deltas.
                conn.execute(
                    """
                    UPDATE chat_turns SET status = 'pending', worker_id = '', worker_heartbeat_at = NULL
                    WHERE id = ? AND status IN ('streaming', 'cancelling')
                      AND worker_heartbeat_at IS NOT NULL
                      AND datetime(worker_heartbeat_at) < datetime('now', ?)
                    """,
                    (turn_id, f"-{stale_after_seconds} seconds"),
                )
                row = conn.execute(
                    """
                    SELECT t.* FROM chat_turns t JOIN chat_sessions s ON s.id = t.session_id
                    WHERE t.id = ? AND s.user_id = ?
                    """,
                    (turn_id, user_id),
                ).fetchone()
                if row is None or row["status"] != "pending":
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = 'streaming', started_at = ?, worker_id = ?, worker_heartbeat_at = ?,
                        retry_count = retry_count + 1
                    WHERE id = ? AND retry_count < 3
                    """,
                    (now, worker_id, now, turn_id),
                )
                claimed = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                if claimed is None or claimed["worker_id"] != worker_id:
                    if claimed is not None and int(claimed["retry_count"] or 0) >= 3:
                        conn.execute(
                            "UPDATE chat_turns SET status = 'failed', error_message = 'Exceeded maximum chat retries', finished_at = ? WHERE id = ?",
                            (now, turn_id),
                        )
                    conn.execute("COMMIT")
                    return None
                conn.execute("COMMIT")
                return row_to_turn(claimed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def touch_turn_worker(self, turn_id: str, worker_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE chat_turns SET worker_heartbeat_at = ?
                WHERE id = ? AND worker_id = ? AND status IN ('streaming', 'cancelling')
                """,
                (utc_now(), turn_id, worker_id),
            )

    def append_turn_event(self, turn_id: str, event_type: str, payload: dict) -> ChatTurnEvent:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT last_event_seq FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                if row is None:
                    raise KeyError("turn not found")
                seq = int(row["last_event_seq"]) + 1
                conn.execute(
                    "INSERT INTO chat_turn_events (turn_id, seq, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (turn_id, seq, event_type, json.dumps(payload, ensure_ascii=False, default=str), now),
                )
                conn.execute("UPDATE chat_turns SET last_event_seq = ? WHERE id = ?", (seq, turn_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return ChatTurnEvent(turn_id=turn_id, seq=seq, event_type=event_type, payload=payload, created_at=now)

    def list_turn_events(self, user_id: int, turn_id: str, after_seq: int = 0) -> list[ChatTurnEvent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT e.* FROM chat_turn_events e
                JOIN chat_turns t ON t.id = e.turn_id
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE e.turn_id = ? AND s.user_id = ? AND e.seq > ?
                ORDER BY e.seq
                """,
                (turn_id, user_id, max(0, after_seq)),
            ).fetchall()
        return [
            ChatTurnEvent(
                turn_id=row["turn_id"],
                seq=int(row["seq"]),
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"] or "{}"),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def complete_turn(
        self,
        user_id: int,
        turn_id: str,
        answer: str,
        summary: dict,
        footer: str = "",
        metrics: dict | None = None,
    ) -> ChatTurn:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT t.*, s.auto_memory AS session_auto_memory
                    FROM chat_turns t JOIN chat_sessions s ON s.id = t.session_id
                    WHERE t.id = ? AND s.user_id = ?
                    """,
                    (turn_id, user_id),
                ).fetchone()
                if row is None:
                    raise KeyError("turn not found")
                conn.execute("UPDATE chat_messages SET content = ? WHERE id = ?", (answer, row["assistant_message_id"]))
                conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = 'completed', answer = ?, summary_json = ?, footer = ?, metrics_json = ?, finished_at = ?,
                        worker_id = '', worker_heartbeat_at = NULL
                    WHERE id = ?
                    """,
                    (answer, json.dumps(summary, ensure_ascii=False, default=str), footer,
                     json.dumps(metrics or {}, ensure_ascii=False, default=str), now, turn_id),
                )
                conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, row["session_id"]))
                # Project Reflection is deliberately outside the realtime
                # query path, but its durable outbox entry is part of the
                # same transaction as the completed turn.  Generic chats and
                # incomplete scopes never map to a shared namespace.
                session_auto_memory = (
                    int(row["session_auto_memory"] or 0) if "session_auto_memory" in row.keys() else 1
                )
                if (
                    getattr(config.settings, "MEMORY_ENABLED", True)
                    and getattr(config.settings, "MEMORY_EXTRACTION_ENABLED", True)
                    and session_auto_memory == 1
                    and row["department_id"] not in (None, "")
                    and row["kb_id"] not in (None, "")
                ):
                    from src.memory.catalog import scope_fingerprint
                    from src.memory.jobs import enqueue_project_reflection_in_connection

                    debounce = max(0, int(getattr(config.settings, "MEMORY_DEBOUNCE_SECONDS", 300)))
                    available_at = (
                        datetime.now(timezone.utc).replace(microsecond=0)
                        + timedelta(seconds=debounce)
                    ).isoformat()
                    enqueue_project_reflection_in_connection(
                        conn,
                        session_id=int(row["session_id"]),
                        scope_fingerprint=scope_fingerprint(
                            scope="project",
                            department_id=row["department_id"],
                            kb_id=row["kb_id"],
                        ),
                        target_turn_id=turn_id,
                        target_message_id=int(row["assistant_message_id"]),
                        available_at=available_at,
                    )
                completed = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                conn.execute("COMMIT")
                return row_to_turn(completed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def fail_turn(self, user_id: int, turn_id: str, message: str, cancelled: bool = False) -> ChatTurn | None:
        now = utc_now()
        status = "cancelled" if cancelled else "failed"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT t.assistant_message_id FROM chat_turns t
                    JOIN chat_sessions s ON s.id = t.session_id
                    WHERE t.id = ? AND s.user_id = ?
                    """,
                    (turn_id, user_id),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                cursor = conn.execute(
                """
                UPDATE chat_turns SET status = ?, error_message = ?, finished_at = ?,
                    worker_id = '', worker_heartbeat_at = NULL
                WHERE id = ? AND session_id IN (
                    SELECT id FROM chat_sessions
                    WHERE user_id = ?
                )
                """,
                (status, message[:1000], now, turn_id, user_id),
                )
                if cursor.rowcount == 0:
                    conn.execute("COMMIT")
                    return None
                placeholder = "已停止生成" if cancelled else f"生成失败：{message[:500]}"
                conn.execute(
                    "UPDATE chat_messages SET content = ? WHERE id = ? AND content = ''",
                    (placeholder, row["assistant_message_id"]),
                )
                completed = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                conn.execute("COMMIT")
                return row_to_turn(completed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def is_turn_worker_stale(self, user_id: int, turn_id: str, stale_after_seconds: int | None = None) -> bool:
        stale_after_seconds = max(30, int(stale_after_seconds or config.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS))
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM chat_turns t JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = ? AND s.user_id = ?
                  AND t.status IN ('streaming', 'cancelling')
                  AND t.worker_heartbeat_at IS NOT NULL
                  AND datetime(t.worker_heartbeat_at) < datetime('now', ?)
                """,
                (turn_id, user_id, f"-{stale_after_seconds} seconds"),
            ).fetchone()
        return row is not None

    def request_turn_cancel(self, user_id: int, turn_id: str) -> ChatTurn | None:
        existing = self.get_turn(user_id, turn_id)
        if existing is None or existing.status in {"completed", "cancelled", "failed"}:
            return existing
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                """
                UPDATE chat_turns SET cancel_requested = 1,
                  status = CASE
                    WHEN status = 'pending' THEN 'cancelled'
                    WHEN status = 'streaming' THEN 'cancelling'
                    ELSE status
                  END,
                  finished_at = CASE WHEN status = 'pending' THEN ? ELSE finished_at END,
                  error_message = CASE WHEN status = 'pending' THEN '已停止生成' ELSE error_message END
                WHERE id = ? AND session_id IN (
                    SELECT id FROM chat_sessions
                    WHERE user_id = ?
                )
                  AND status IN ('pending', 'streaming', 'cancelling')
                """,
                (now, turn_id, user_id),
                )
                # A pending turn has no worker to write a terminal message.
                # Replace its assistant placeholder immediately so history
                # never contains a permanent blank assistant row.
                conn.execute(
                    """
                    UPDATE chat_messages SET content = '已停止生成'
                    WHERE id = (
                        SELECT assistant_message_id FROM chat_turns WHERE id = ? AND status = 'cancelled'
                    ) AND content = ''
                    """,
                    (turn_id,),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_turn(user_id, turn_id)

    def is_turn_cancel_requested(self, turn_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT cancel_requested FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def task_metrics_summary(self, department_id: int | None = None, hours: int = 24) -> dict:
        """Aggregate non-content operational metrics for the management UI/API."""
        hours = max(1, min(int(hours), 24 * 30))
        clause = "WHERE datetime(created_at) >= datetime('now', ?)"
        params: list[object] = [f"-{hours} hours"]
        if department_id is not None:
            clause += " AND department_id = ?"
            params.append(int(department_id))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT status, metrics_json FROM chat_turns {clause}", params
            ).fetchall()
        total = len(rows)
        completed = sum(1 for row in rows if row["status"] == "completed")
        failed = sum(1 for row in rows if row["status"] == "failed")
        cancelled = sum(1 for row in rows if row["status"] == "cancelled")
        metric_rows = []
        for row in rows:
            try:
                payload = json.loads(row["metrics_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                metric_rows.append(payload)
        def average(key: str) -> int | None:
            values = [int(item[key]) for item in metric_rows if isinstance(item.get(key), (int, float))]
            return int(sum(values) / len(values)) if values else None
        terminal = completed + failed
        return {
            "window_hours": hours,
            "total": total,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "failure_rate": round(failed / terminal, 4) if terminal else 0.0,
            "avg_queue_ms": average("queue_ms"),
            "avg_first_token_ms": average("first_token_ms"),
            "avg_total_ms": average("total_ms"),
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_session(row) -> ChatSession:
    return ChatSession(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        kb_name=row["kb_name"],
        department_id=row["department_id"],
        kb_id=row["kb_id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_message(row) -> ChatMessage:
    content = row["content"]
    footer = row["footer"] if "footer" in row.keys() else ""
    memory_context: list[dict] = []
    if "turn_summary" in row.keys():
        try:
            summary = json.loads(row["turn_summary"] or "{}")
        except (TypeError, ValueError):
            summary = {}
        candidate = summary.get("memory_context") if isinstance(summary, dict) else None
        if isinstance(candidate, list):
            memory_context = [item for item in candidate if isinstance(item, dict)]
    legacy_suffix = f"\n\n---\n{footer}" if footer else ""
    if legacy_suffix and content.endswith(legacy_suffix):
        content = content[:-len(legacy_suffix)]
    return ChatMessage(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        role=row["role"],
        content=content,
        footer=footer,
        created_at=row["created_at"],
        edited_at=row["edited_at"] if "edited_at" in row.keys() else None,
        redacted=bool(row["redacted"]) if "redacted" in row.keys() else False,
        memory_context=memory_context,
    )


def row_to_turn(row) -> ChatTurn:
    try:
        summary = json.loads(row["summary_json"] or "{}")
    except (TypeError, ValueError):
        summary = {}
    try:
        metrics = json.loads(row["metrics_json"] or "{}") if "metrics_json" in row.keys() else {}
    except (TypeError, ValueError):
        metrics = {}
    try:
        trace_context = json.loads(row["trace_context_json"] or "{}") if "trace_context_json" in row.keys() else {}
    except (TypeError, ValueError):
        trace_context = {}
    if not isinstance(trace_context, dict):
        trace_context = {}
    return ChatTurn(
        id=row["id"],
        session_id=int(row["session_id"]),
        user_message_id=int(row["user_message_id"]),
        assistant_message_id=int(row["assistant_message_id"]),
        kb_name=row["kb_name"],
        department_id=row["department_id"],
        kb_id=row["kb_id"],
        query=row["query"],
        query_mode=row["query_mode"] if "query_mode" in row.keys() else "fast",
        status=row["status"],
        client_request_id=row["client_request_id"],
        cancel_requested=bool(row["cancel_requested"]),
        last_event_seq=int(row["last_event_seq"]),
        answer=row["answer"] or "",
        summary=summary if isinstance(summary, dict) else {},
        footer=row["footer"] or "",
        metrics=metrics if isinstance(metrics, dict) else {},
        trace_context={str(key): str(value) for key, value in trace_context.items() if value},
        error_message=row["error_message"] or "",
        worker_id=row["worker_id"] or "",
        worker_heartbeat_at=row["worker_heartbeat_at"],
        retry_count=int(row["retry_count"] or 0),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
