import os
import json
import hashlib
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

import src.settings

# Sentinel kb_name persisted in chat sessions/turns for general (non-KB) chat.
# The literal value is part of the durable schema; never change it.
GENERAL_CHAT_KB_NAME = "__general__"


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
    document_context: dict | None = None


@dataclass
class ChatMessage:
    id: int
    session_id: int
    turn_id: str | None
    role: str
    content: str
    footer: str
    created_at: str
    edited_at: str | None = None
    redacted: bool = False
    memory_context: list[dict] = field(default_factory=list)
    document_context: dict | None = None
    # 紧凑引用列表（来自 turn summary.evidence，内容截断），让"参考来源"
    # 面板在刷新后仍可用，而不必重放 turn 事件。
    citations: list[dict] = field(default_factory=list)


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
    wiki_assist: bool
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
    document_context: dict | None = None
    export_plan: dict = field(default_factory=dict)
    source_scope: str = "auto"
    required_attachment_ids: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    # Server-assigned, per-session monotonic turn order.  0 only appears for
    # rows written before the expand migration; runtime always allocates.
    turn_sequence: int = 0
    # Execution ownership captured at claim time.  0 means the legacy
    # (non-serialized) path and disables stale-owner rejection for that turn.
    fencing_token: int = 0
    session_generation: int = 0
    # Server-generated identity of the execution attempt that owns this turn.
    attempt_id: str = ""


# A turn waits here while its required chat attachments are still parsing.
TURN_STATUS_WAITING_FOR_ATTACHMENTS = "waiting_for_attachments"

# Non-terminal statuses that keep later turns of the same session queued.
TURN_STATUSES_NON_TERMINAL = ("pending", "streaming", "cancelling", "waiting_for_attachments")


class StaleTurnOwner(RuntimeError):
    """Raised when a superseded attempt tries to write authoritative state."""


@dataclass
class TurnClaimResult:
    """Structured ``claim_turn`` outcome (plan §5.2).

    ``status`` is one of ``claimed``/``blocked``/``stale``/``cancelled``/
    ``retry_exhausted``/``noop``.  ``turn`` is only set for ``claimed``; the
    legacy ``ChatTurn | None`` adapter keeps existing callers working.
    """

    status: str
    turn: ChatTurn | None = None
    blocker_turn_id: str = ""


@dataclass
class ChatTurnEvent:
    turn_id: str
    seq: int
    event_type: str
    payload: dict
    created_at: str


@dataclass
class TurnCompletion:
    """Structured ``complete_turn`` outcome (v3 §4.7).

    ``terminal_events`` are persisted in the same transaction as the answer
    and terminal status, so replay can never observe a completed turn without
    its final events.
    """

    turn: ChatTurn
    terminal_events: list[ChatTurnEvent] = field(default_factory=list)


@dataclass
class TurnFinalization:
    """Structured ``fail_turn`` outcome; ``turn`` is None when already terminal."""

    turn: ChatTurn | None = None
    terminal_events: list[ChatTurnEvent] = field(default_factory=list)


# Paths whose schema has already been initialized by this process; repeated
# constructions must not re-run DDL/backfills on the shared authority DB.
_INITIALIZED_DB_PATHS: set[str] = set()
_INIT_LOCK = threading.Lock()


class ConversationService:
    def __new__(cls, db_path: str | None = None):
        """Dispatch to the configured control-plane backend (SQLite default)."""
        import src.settings as settings

        backend = (
            str(getattr(settings, "CONTROL_PLANE_BACKEND", "sqlite") or "sqlite")
            .strip()
            .lower()
        )
        if backend == "sqlite":
            return super().__new__(cls)
        from src.storage.control_plane.factory import build_conversation_service

        return build_conversation_service()

    def __init__(self, db_path: str | None = None):
        self.db_path = os.path.abspath(db_path or src.settings.AUTH_DB_PATH)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with _INIT_LOCK:
            needs_init = self.db_path not in _INITIALIZED_DB_PATHS
        if needs_init:
            self._init_db_with_retry()
            with _INIT_LOCK:
                _INITIALIZED_DB_PATHS.add(self.db_path)

    def list_waiting_turn_ids(self, attachment_ids: list[str] | None = None, *, limit: int = 100) -> list[str]:
        bounded_limit = max(1, min(int(limit), 1000))
        with closing(self._connect()) as conn:
            if attachment_ids is not None:
                if not attachment_ids:
                    return []
                placeholders = ",".join("?" for _ in attachment_ids)
                rows = conn.execute(
                    f"""SELECT DISTINCT t.id, t.created_at FROM chat_turns t
                        JOIN turn_attachments ta ON ta.turn_id = t.id
                        WHERE t.status = 'waiting_for_attachments'
                        AND ta.attachment_id IN ({placeholders}) ORDER BY t.created_at, t.id LIMIT ?""",
                    (*attachment_ids, bounded_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM chat_turns WHERE status = 'waiting_for_attachments' ORDER BY created_at, id LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
        return [str(row["id"]) for row in rows]

    def _init_db_with_retry(self) -> None:
        """Bounded busy retry for concurrent first-use schema initialization."""
        deadline = time.monotonic() + 10.0
        while True:
            try:
                self._init_db()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

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
                    document_context_json TEXT NOT NULL DEFAULT '{}',
                    -- Next sequence value to hand to a new turn of this session.
                    -- Legacy rows are reconciled to MAX(turn_sequence)+1 on init.
                    next_turn_sequence INTEGER NOT NULL DEFAULT 1,
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
                    turn_sequence INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    answer TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    footer TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    trace_context_json TEXT NOT NULL DEFAULT '{}',
                    document_context_json TEXT NOT NULL DEFAULT '{}',
                    export_plan_json TEXT NOT NULL DEFAULT '{}',
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
            try:
                conn.execute(
                    "ALTER TABLE chat_turns ADD COLUMN wiki_assist INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在
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
            # Frozen attachment views per turn (design §4.5). History must
            # still render "file.pdf（已删除）" after the attachment is gone;
            # snapshots duplicate metadata instead of joining live rows.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS turn_attachments (
                    turn_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    filename_snapshot TEXT NOT NULL,
                    media_type_snapshot TEXT NOT NULL DEFAULT '',
                    usage_hint_snapshot TEXT NOT NULL DEFAULT 'reference',
                    content_hash_snapshot TEXT NOT NULL DEFAULT '',
                    parser_version_snapshot TEXT NOT NULL DEFAULT '',
                    asset_id TEXT NOT NULL DEFAULT '',
                    parse_status_snapshot TEXT NOT NULL DEFAULT 'queued',
                    PRIMARY KEY(turn_id, attachment_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn_attachments_asset "
                "ON turn_attachments(asset_id)"
            )
            self._ensure_column(conn, "chat_sessions", "department_id", "INTEGER")
            self._ensure_column(conn, "chat_sessions", "kb_id", "INTEGER")
            self._ensure_column(conn, "chat_sessions", "document_context_json", "TEXT NOT NULL DEFAULT '{}'" )
            self._ensure_column(conn, "chat_turns", "department_id", "INTEGER")
            self._ensure_column(conn, "chat_turns", "kb_id", "INTEGER")
            self._ensure_column(conn, "chat_turns", "worker_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "chat_turns", "worker_heartbeat_at", "TEXT")
            self._ensure_column(conn, "chat_turns", "retry_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_turns", "query_mode", "TEXT NOT NULL DEFAULT 'fast'")
            self._ensure_column(conn, "chat_turns", "metrics_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "chat_turns", "trace_context_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "chat_turns", "document_context_json", "TEXT NOT NULL DEFAULT '{}'" )
            self._ensure_column(conn, "chat_turns", "export_plan_json", "TEXT NOT NULL DEFAULT '{}'" )
            self._ensure_column(conn, "chat_messages", "edited_at", "TEXT")
            self._ensure_column(conn, "chat_messages", "redacted", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_sessions", "auto_memory", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "chat_turns", "source_scope", "TEXT NOT NULL DEFAULT 'auto'")
            self._ensure_column(conn, "chat_turns", "required_attachment_ids", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "chat_sessions", "next_turn_sequence", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "chat_turns", "turn_sequence", "INTEGER")
            self._backfill_turn_sequences(conn)
            # Per-session execution authority (plan §5.2.1): the single source of
            # truth for lease, fencing token, generation and checkpoint pointers.
            # Turn/event/answer writes share this database transaction boundary.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_execution_state (
                    session_id INTEGER PRIMARY KEY,
                    active_turn_id TEXT NOT NULL DEFAULT '',
                    active_attempt_id TEXT NOT NULL DEFAULT '',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0,
                    recoverable_checkpoint_ref TEXT NOT NULL DEFAULT '',
                    committed_checkpoint_ref TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_turns_session_sequence"
                " ON chat_turns(session_id, turn_sequence)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_chat_turns_session_status_sequence"
                " ON chat_turns(session_id, status, turn_sequence)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_status_created ON chat_turns(status, created_at)")
            has_users = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ).fetchone()
            if has_users:
                # Backfill historical chats from their owning user. Once populated,
                # every read also enforces this department scope.  Only write when
                # rows actually need it: a no-op UPDATE would still contend for the
                # write lock during multi-process startup.
                pending_sessions = conn.execute(
                    "SELECT 1 FROM chat_sessions WHERE department_id IS NULL LIMIT 1"
                ).fetchone()
                if pending_sessions is not None:
                    conn.execute(
                        """
                        UPDATE chat_sessions
                        SET department_id = (SELECT department_id FROM users WHERE users.id = chat_sessions.user_id)
                        WHERE department_id IS NULL
                        """
                    )
                pending_turns = conn.execute(
                    "SELECT 1 FROM chat_turns WHERE department_id IS NULL LIMIT 1"
                ).fetchone()
                if pending_turns is not None:
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

    @staticmethod
    def _backfill_turn_sequences(conn) -> None:
        """Expand/backfill legacy rows in (session_id, user_message_id) order.

        Existing non-null sequences are respected; new values continue from the
        per-session maximum.  The per-session counter stores the next available
        value, so it is reconciled to ``MAX(turn_sequence) + 1`` (1 for sessions
        without turns).  This never reuses a sequence that history already used.
        """
        rows = conn.execute(
            """
            SELECT id, session_id FROM chat_turns
            WHERE turn_sequence IS NULL
            ORDER BY session_id, user_message_id, rowid
            """
        ).fetchall()
        if rows:
            counters = {
                int(row["session_id"]): int(row["max_seq"] or 0)
                for row in conn.execute(
                    "SELECT session_id, MAX(turn_sequence) AS max_seq FROM chat_turns GROUP BY session_id"
                ).fetchall()
            }
            for row in rows:
                session_id = int(row["session_id"])
                counters[session_id] = counters.get(session_id, 0) + 1
                conn.execute(
                    "UPDATE chat_turns SET turn_sequence = ? WHERE id = ?",
                    (counters[session_id], str(row["id"])),
                )
        mismatch = conn.execute(
            """
            SELECT 1 FROM chat_sessions
            WHERE next_turn_sequence <> COALESCE((
                SELECT MAX(turns.turn_sequence)
                FROM chat_turns turns WHERE turns.session_id = chat_sessions.id
            ), 0) + 1
            LIMIT 1
            """
        ).fetchone()
        if mismatch is not None:
            conn.execute(
                """
                UPDATE chat_sessions
                SET next_turn_sequence = COALESCE((
                    SELECT MAX(turns.turn_sequence)
                    FROM chat_turns turns WHERE turns.session_id = chat_sessions.id
                ), 0) + 1
                WHERE next_turn_sequence <> COALESCE((
                    SELECT MAX(turns.turn_sequence)
                    FROM chat_turns turns WHERE turns.session_id = chat_sessions.id
                ), 0) + 1
                """
            )

    @staticmethod
    def _ensure_execution_state(conn, session_id: int):
        """Fetch (creating if needed) the per-session execution state row.

        Returns ``None`` when the session no longer exists: a deleted session
        must never be resurrected by a late worker re-creating state.
        """
        sid = int(session_id)
        row = conn.execute(
            "SELECT * FROM conversation_execution_state WHERE session_id = ?", (sid,)
        ).fetchone()
        if row is not None:
            return row
        exists = conn.execute("SELECT 1 FROM chat_sessions WHERE id = ?", (sid,)).fetchone()
        if exists is None:
            return None
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_execution_state (session_id, updated_at)
            VALUES (?, ?)
            """,
            (sid, utc_now()),
        )
        return conn.execute(
            "SELECT * FROM conversation_execution_state WHERE session_id = ?", (sid,)
        ).fetchone()

    @classmethod
    def _bump_session_generation(cls, conn, session_id: int) -> None:
        """Revoke in-flight commits for a session without touching pointers."""
        state = cls._ensure_execution_state(conn, session_id)
        if state is None:
            return
        conn.execute(
            """
            UPDATE conversation_execution_state
            SET generation = generation + 1, revision = revision + 1, updated_at = ?
            WHERE session_id = ?
            """,
            (utc_now(), int(session_id)),
        )

    @classmethod
    def _reset_execution_state(cls, conn, session_id: int) -> None:
        """Clear history: advance generation and drop ownership/checkpoint refs."""
        state = cls._ensure_execution_state(conn, session_id)
        if state is None:
            return
        conn.execute(
            """
            UPDATE conversation_execution_state
            SET generation = generation + 1,
                active_turn_id = '', active_attempt_id = '',
                lease_owner = '', lease_expires_at = NULL,
                recoverable_checkpoint_ref = '', committed_checkpoint_ref = '',
                revision = revision + 1, updated_at = ?
            WHERE session_id = ?
            """,
            (utc_now(), int(session_id)),
        )

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

    def get_session_unscoped(self, session_id: int) -> ChatSession | None:
        """Look up a session without an owner filter.

        Reserved for governance paths (release review, audit) that must first
        resolve the session's knowledge base and then enforce KB permission
        themselves; ordinary content reads must keep using ``get_session``.
        """

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ?",
                (session_id,),
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
                       t.id AS turn_id,
                       COALESCE(t.summary_json, '{}') AS turn_summary,
                       COALESCE(t.document_context_json, '{}') AS turn_document_context
                FROM chat_messages m
                LEFT JOIN chat_turns t
                  ON t.assistant_message_id = m.id OR t.user_message_id = m.id
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
                    """
                    UPDATE chat_sessions
                    SET title = '新对话', updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (now, session_id, user_id),
                )
                # Sequence values already handed out are never reused; the
                # generation advance invalidates in-flight commits and pointers.
                self._reset_execution_state(conn, session_id)
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
        # Reserve a durable cleanup task before deleting the session row. The
        # attachment database is separate from the conversation database, so
        # a post-delete best-effort enqueue could permanently lose the only
        # reference needed to reclaim private files. If the outbox cannot be
        # written, abort the deletion and let the caller retry safely.
        from src.attachments.store import AttachmentStore

        attachment_store = AttachmentStore()
        attachment_store.enqueue_session_cleanup(
            session_id=session_id,
            user_id=user_id,
            targets={
                "attachments": True,
                "storage": True,
                "require_session_absent": True,
            },
        )
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
        if not getattr(src.settings, "MEMORY_ENABLED", True) or not getattr(
            src.settings, "MEMORY_EXTRACTION_ENABLED", True
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

        debounce = max(0, int(getattr(src.settings, "MEMORY_DEBOUNCE_SECONDS", 300)))
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
        wiki_assist: bool = False,
        document_context: dict | Any | None = None,
        attachment_ids: list[str] | None = None,
        source_scope: str = "auto",
    ) -> ChatTurn:
        """Persist one user request and its assistant placeholder atomically.

        The idempotency key makes browser retries safe: the same request returns
        the original turn instead of incurring a second model invocation.

        With attachments, the turn starts as ``waiting_for_attachments`` while
        any referenced asset is still parsing, and frozen attachment snapshots
        are stored for history rendering.
        """
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        request_id = (client_request_id or "").strip()[:128] or None
        query_mode = query_mode if query_mode in {"fast", "deep"} else "fast"
        if hasattr(document_context, "model_dump"):
            document_context = document_context.model_dump(mode="json")
        if document_context is not None and not isinstance(document_context, dict):
            raise ValueError("document_context must be an object")
        document_context_json = json.dumps(
            document_context or {}, ensure_ascii=False, sort_keys=True, default=str,
        )
        attachment_ids = [str(item or "").strip() for item in (attachment_ids or []) if str(item or "").strip()]
        session = self.get_session(user_id, session_id)
        if session is None:
            raise PermissionError("chat session does not belong to current user")
        from src.attachments.models import (
            resolve_source_scope,
            scope_allows_attachments,
        )

        effective_scope = resolve_source_scope(
            requested_scope=source_scope,
            has_attachments=bool(attachment_ids),
            is_general_chat=session.kb_name == GENERAL_CHAT_KB_NAME,
        )
        attachment_refs: list[dict] = []
        if attachment_ids and scope_allows_attachments(effective_scope):
            # Lazy import keeps the conversation module independent of the
            # attachment domain (same pattern as memory/result_exports).
            from src.attachments.service import AttachmentService

            service = AttachmentService()
            refs, blocked = service.resolve_refs_for_turn(
                user_id=user_id, session_id=session_id, attachment_ids=attachment_ids
            )
            if blocked:
                raise ValueError(
                    "attachment_unavailable: " + ", ".join(blocked[:10])
                )
            attachment_refs = [
                {
                    "attachment_id": ref.attachment_id,
                    "asset_id": ref.asset_id,
                    "filename": ref.filename,
                    "media_type": ref.media_type,
                    "extension": ref.extension,
                    "size_bytes": ref.size_bytes,
                    "sha256": ref.sha256,
                    "usage_hint": ref.usage_hint,
                    "parse_status": ref.parse_status,
                    "parser_version": ref.parser_version,
                    "degraded_reason": ref.degraded_reason,
                }
                for ref in refs
            ]
        from src.result_exports import infer_export_intent

        export_plan = infer_export_intent(query)
        export_plan_json = json.dumps(
            export_plan.to_dict() if export_plan is not None else {},
            ensure_ascii=False,
            sort_keys=True,
        )
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session_row = conn.execute(
                    """
                    SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if session_row is None:
                    raise PermissionError("chat session does not belong to current user")
                if request_id:
                    existing = conn.execute(
                        "SELECT * FROM chat_turns WHERE session_id = ? AND client_request_id = ?",
                        (session_id, request_id),
                    ).fetchone()
                    if existing is not None:
                        conn.execute("COMMIT")
                        return row_to_turn(existing)
                keys = session_row.keys()
                if "next_turn_sequence" in keys:
                    turn_sequence = int(session_row["next_turn_sequence"] or 0)
                else:
                    # Legacy schema before the expand migration ran; the
                    # surrounding BEGIN IMMEDIATE makes the max+1 read safe.
                    turn_sequence = 0
                if turn_sequence <= 0:
                    turn_sequence = 1
                # Defensive reconciliation: never hand out a sequence history
                # already used (covers concurrent writers on a legacy schema).
                max_row = conn.execute(
                    "SELECT COALESCE(MAX(turn_sequence), 0) AS seq FROM chat_turns WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                turn_sequence = max(turn_sequence, int(max_row["seq"] or 0) + 1)
                user_cursor = conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                    (session_id, query, now),
                )
                assistant_cursor = conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, 'assistant', '', ?)",
                    (session_id, now),
                )
                conn.execute(
                    """UPDATE chat_sessions
                       SET updated_at = ?,
                           next_turn_sequence = ?,
                           title = CASE WHEN title = '新对话' THEN ? ELSE title END,
                           document_context_json = CASE WHEN ? = '{}' THEN document_context_json ELSE ? END
                       WHERE id = ?""",
                    (
                        now, turn_sequence + 1, query.replace("\n", " ")[:32] or "新对话",
                        document_context_json, document_context_json, session_id,
                    ),
                )
                turn_id = str(uuid.uuid4())
                # A turn waits while any required attachment is still parsing.
                waiting = any(
                    ref.get("parse_status") in {"queued", "running"}
                    for ref in attachment_refs
                )
                initial_status = (
                    TURN_STATUS_WAITING_FOR_ATTACHMENTS if waiting else "pending"
                )
                required_ids = [ref["attachment_id"] for ref in attachment_refs]
                conn.execute(
                    """
                    INSERT INTO chat_turns (
                        id, session_id, user_message_id, assistant_message_id, kb_name, query, query_mode,
                        department_id, kb_id, status, client_request_id, turn_sequence,
                        trace_context_json, created_at,
                        document_context_json, export_plan_json, source_scope, required_attachment_ids,
                        wiki_assist
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        session_id,
                        user_cursor.lastrowid,
                        assistant_cursor.lastrowid,
                        session_row["kb_name"],
                        query,
                        query_mode,
                        session_row["department_id"],
                        session_row["kb_id"],
                        initial_status,
                        request_id,
                        turn_sequence,
                        json.dumps(trace_context or {}, ensure_ascii=False),
                        now,
                        document_context_json,
                        export_plan_json,
                        effective_scope,
                        json.dumps(required_ids, ensure_ascii=False),
                        1 if wiki_assist else 0,
                    ),
                )
                if attachment_refs:
                    for ordinal, ref in enumerate(attachment_refs):
                        conn.execute(
                            """
                            INSERT INTO turn_attachments (
                                turn_id, attachment_id, ordinal, filename_snapshot,
                                media_type_snapshot, usage_hint_snapshot,
                                content_hash_snapshot, parser_version_snapshot,
                                asset_id, parse_status_snapshot
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(turn_id, attachment_id) DO UPDATE SET
                                ordinal = excluded.ordinal,
                                filename_snapshot = excluded.filename_snapshot,
                                media_type_snapshot = excluded.media_type_snapshot,
                                usage_hint_snapshot = excluded.usage_hint_snapshot,
                                content_hash_snapshot = excluded.content_hash_snapshot,
                                parser_version_snapshot = excluded.parser_version_snapshot,
                                asset_id = excluded.asset_id,
                                parse_status_snapshot = excluded.parse_status_snapshot
                            """,
                            (
                                turn_id,
                                ref["attachment_id"],
                                ordinal,
                                ref["filename"],
                                ref["media_type"],
                                ref["usage_hint"],
                                ref["sha256"],
                                ref["parser_version"],
                                ref["asset_id"],
                                ref["parse_status"],
                            ),
                        )
                row = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                turn = row_to_turn(row)
                conn.execute("COMMIT")
                turn.attachments = attachment_refs
                return turn
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

    def find_turn_by_client_request(self, session_id: int, client_request_id: str) -> ChatTurn | None:
        """Idempotency lookup used before admission checks (P0-006)."""
        request_id = str(client_request_id or "").strip()[:128]
        if not request_id:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chat_turns WHERE session_id = ? AND client_request_id = ?",
                (int(session_id), request_id),
            ).fetchone()
        return row_to_turn(row) if row else None

    def count_unterminal_turns(self, user_id: int, *, session_id: int | None = None) -> int:
        """Non-terminal turns owned by a user (optionally scoped to a session).

        Includes attachment waits so per-session/user admission bounds apply to
        work that has not reached a terminal state yet (plan §6.4).
        """
        with closing(self._connect()) as conn:
            if session_id is not None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total FROM chat_turns t
                    JOIN chat_sessions s ON s.id = t.session_id
                    WHERE s.user_id = ? AND t.session_id = ?
                      AND t.status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                    """,
                    (int(user_id), int(session_id)),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total FROM chat_turns t
                    JOIN chat_sessions s ON s.id = t.session_id
                    WHERE s.user_id = ?
                      AND t.status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                    """,
                    (int(user_id),),
                ).fetchone()
        return int(row["total"] or 0) if row else 0

    def get_turn_by_message(self, user_id: int, message_id: int) -> ChatTurn | None:
        """Resolve a persisted user/assistant message to its owning turn."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT t.* FROM chat_turns t
                   JOIN chat_sessions s ON s.id = t.session_id
                   WHERE s.user_id = ? AND (t.user_message_id = ? OR t.assistant_message_id = ?)
                   ORDER BY t.created_at DESC LIMIT 1""",
                (user_id, message_id, message_id),
            ).fetchone()
        return row_to_turn(row) if row else None

    def list_active_turns(self, user_id: int, session_id: int) -> list[ChatTurn]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.session_id = ? AND s.user_id = ?
                  AND t.status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                ORDER BY t.created_at
                """,
                (session_id, user_id),
            ).fetchall()
        return [row_to_turn(row) for row in rows]

    def list_recent_turns(
        self, user_id: int, session_id: int, limit: int = 1
    ) -> list[ChatTurn]:
        """Newest turns regardless of status (refresh/reopen recovery)."""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.session_id = ? AND s.user_id = ?
                ORDER BY t.created_at DESC, t.rowid DESC
                LIMIT ?
                """,
                (session_id, user_id, max(1, int(limit))),
            ).fetchall()
        return [row_to_turn(row) for row in rows]

    def list_session_document_cards(
        self, user_id: int, session_id: int
    ) -> dict[str, list[dict]]:
        """Persisted ``document_card`` payloads per turn, oldest first.

        History rehydration reads these so candidate/document cards survive a
        page refresh; the live SSE path remains the primary source.
        """

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT e.turn_id, e.payload_json FROM chat_turn_events e
                JOIN chat_turns t ON t.id = e.turn_id
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.session_id = ? AND s.user_id = ?
                  AND e.event_type = 'document_card'
                ORDER BY e.turn_id, e.seq
                """,
                (session_id, user_id),
            ).fetchall()
        cards: dict[str, list[dict]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            card = payload.get("card") if isinstance(payload, dict) else None
            if isinstance(card, dict) and card:
                cards.setdefault(str(row["turn_id"]), []).append(card)
        return cards

    def get_turn_unscoped(self, turn_id: str) -> ChatTurn | None:
        """Turn lookup without a user binding, for background coordinators."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return row_to_turn(row) if row else None

    def transition_waiting_turn(
        self,
        turn_id: str,
        *,
        to_status: str,
        error_message: str = "",
        reason_code: str = "",
    ) -> bool:
        """Atomically move a waiting turn out of the attachment wait state.

        Only ``pending`` and ``failed`` destinations are accepted; the update
        is a no-op when a user cancelled the turn while it was waiting.
        """
        if to_status not in {"pending", "failed"}:
            raise ValueError("waiting turns can only transition to pending or failed")
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = ?,
                        error_message = ?,
                        finished_at = CASE WHEN ? = 'failed' THEN ? ELSE finished_at END
                    WHERE id = ? AND status = 'waiting_for_attachments'
                    """,
                    (
                        to_status,
                        error_message[:1000] if to_status == "failed" else "",
                        to_status, now, turn_id,
                    ),
                )
                if to_status == "failed":
                    conn.execute(
                        """
                        UPDATE chat_messages SET content = ?
                        WHERE id = (
                            SELECT assistant_message_id FROM chat_turns WHERE id = ?
                        ) AND content = ''
                        """,
                        (f"附件解析失败：{error_message[:400]}", turn_id),
                    )
                conn.execute("COMMIT")
                updated = int(cursor.rowcount or 0) > 0
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if updated:
            if reason_code == "attachment_wait_timeout":
                wait_status = "timed_out"
            elif to_status == "pending":
                wait_status = "completed"
            else:
                wait_status = "failed"
            self._record_attachment_wait(turn_id, wait_status)
        return updated

    def _record_attachment_wait(self, turn_id: str, wait_status: str) -> None:
        try:
            from src.observability.metrics import histogram

            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT created_at FROM chat_turns WHERE id = ?", (turn_id,)
                ).fetchone()
            if row is None:
                return
            created = datetime.fromisoformat(str(row["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            duration = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
            histogram(
                "hdb.conversation.attachment_wait",
                duration,
                attributes={"status": wait_status},
                description="Time spent in waiting_for_attachments by terminal outcome",
            )
        except Exception:
            pass

    def expire_waiting_turns(self, max_wait_seconds: int | None = None, limit: int = 50) -> int:
        """Fail attachment waits that exceeded the cap so later turns proceed.

        Uses the same conditional transition as the settlement path, so a parse
        completion racing the sweep can win only once.
        """
        configured = getattr(src.settings, "WAITING_TURN_MAX_SECONDS", 600)
        max_wait_seconds = max(1, int(max_wait_seconds if max_wait_seconds is not None else configured))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id FROM chat_turns
                WHERE status = 'waiting_for_attachments'
                  AND datetime(created_at) < datetime('now', ?)
                ORDER BY created_at, id
                LIMIT ?
                """,
                (f"-{int(max_wait_seconds)} seconds", max(1, min(int(limit), 500))),
            ).fetchall()
        expired = 0
        for row in rows:
            turn_id = str(row["id"])
            updated = self.transition_waiting_turn(
                turn_id,
                to_status="failed",
                error_message="附件解析等待超时，本轮请求已取消；可稍后重新发送。",
                reason_code="attachment_wait_timeout",
            )
            if updated:
                expired += 1
                try:
                    self.append_turn_event(
                        turn_id,
                        "attachment_wait_timeout",
                        {
                            "error_code": "attachment_wait_timeout",
                            "wait_seconds": int(max_wait_seconds),
                        },
                    )
                except Exception:
                    pass
        return expired

    def expire_queued_turns(self, max_queue_seconds: int | None = None, limit: int = 100) -> int:
        """Fail never-started turns that waited past the chat queue deadline.

        The deadline counts from turn acceptance and includes attachment waits
        and same-session blocking (plan §6.4).  Running turns keep their
        execution deadline and are never touched by this sweep.
        """
        configured = getattr(src.settings, "CHAT_QUEUE_DEADLINE_SECONDS", 600)
        max_queue_seconds = max(
            1, int(max_queue_seconds if max_queue_seconds is not None else configured)
        )
        now = utc_now()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id FROM chat_turns
                WHERE status = 'pending' AND cancel_requested = 0
                  AND datetime(created_at) < datetime('now', ?)
                ORDER BY created_at, id
                LIMIT ?
                """,
                (f"-{int(max_queue_seconds)} seconds", max(1, min(int(limit), 500))),
            ).fetchall()
        expired = 0
        for row in rows:
            turn_id = str(row["id"])
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.execute(
                        """
                        UPDATE chat_turns
                        SET status = 'failed',
                            error_message = '排队等待超时，本轮请求未执行；请稍后重试。',
                            finished_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (now, turn_id),
                    )
                    updated = int(cursor.rowcount or 0) > 0
                    if updated:
                        conn.execute(
                            "UPDATE chat_messages SET content = ? WHERE id = ("
                            " SELECT assistant_message_id FROM chat_turns WHERE id = ?"
                            ") AND content = ''",
                            ("生成失败：排队等待超时", turn_id),
                        )
                        # The terminal event shares the transaction with the
                        # terminal status (v3 §4.7).
                        self._append_turn_event_in_connection(
                            conn,
                            turn_id,
                            "chat_queue_timeout",
                            {
                                "error_code": "chat_queue_timeout",
                                "wait_seconds": int(max_queue_seconds),
                            },
                            created_at=now,
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            if not updated:
                continue
            expired += 1
            try:
                from src.observability.metrics import counter

                counter(
                    "hdb.chat.queue_timeout",
                    description="Turns failed after exceeding the chat queue deadline",
                )
            except Exception:
                pass
        return expired

    def turn_queue_context(self, turn_id: str) -> dict[str, Any]:
        """Queue visibility for the turn API: blocker, reason and deadline."""
        context: dict[str, Any] = {
            "blocked_by_turn_id": None,
            "queue_reason": "",
            "wait_deadline": None,
        }
        with closing(self._connect()) as conn:
            turn = conn.execute(
                "SELECT session_id, turn_sequence, status, created_at FROM chat_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn is None:
                return context
            blocker = conn.execute(
                """
                SELECT prior.id, prior.status FROM chat_turns prior
                WHERE prior.session_id = ? AND prior.turn_sequence < ?
                  AND prior.status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                ORDER BY prior.turn_sequence DESC
                LIMIT 1
                """,
                (int(turn["session_id"]), int(turn["turn_sequence"] or 0)),
            ).fetchone()
        if str(turn["status"]) == TURN_STATUS_WAITING_FOR_ATTACHMENTS:
            context["queue_reason"] = "waiting_for_attachments"
            context["wait_deadline"] = _iso_after(
                str(turn["created_at"]),
                int(getattr(src.settings, "WAITING_TURN_MAX_SECONDS", 600)),
            )
        elif blocker is not None:
            context["blocked_by_turn_id"] = str(blocker["id"])
            context["queue_reason"] = (
                "waiting_for_attachments"
                if str(blocker["status"]) == TURN_STATUS_WAITING_FOR_ATTACHMENTS
                else "queued_behind_previous_turn"
            )
        return context

    def list_turn_attachments(self, turn_id: str) -> list[dict]:
        """Frozen attachment snapshots for one turn (history rendering)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM turn_attachments WHERE turn_id = ? ORDER BY ordinal
                """,
                (turn_id,),
            ).fetchall()
        snapshots = [dict(row) for row in rows]
        self._mark_deleted_attachment_snapshots(snapshots)
        return snapshots

    def list_turn_attachments_for_session(self, session_id: int) -> dict[str, list[dict]]:
        """Snapshots grouped by turn_id for list views."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT ta.* FROM turn_attachments ta
                JOIN chat_turns t ON t.id = ta.turn_id
                WHERE t.session_id = ?
                ORDER BY ta.turn_id, ta.ordinal
                """,
                (session_id,),
            ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["turn_id"], []).append(dict(row))
        for snapshots in grouped.values():
            self._mark_deleted_attachment_snapshots(snapshots)
        return grouped

    @staticmethod
    def _mark_deleted_attachment_snapshots(snapshots: list[dict]) -> None:
        """Overlay current attachment lifecycle state onto frozen snapshots."""
        for snapshot in snapshots:
            snapshot["deleted"] = False
        if not snapshots:
            return
        attachment_ids = [
            str(snapshot.get("attachment_id") or "").strip()
            for snapshot in snapshots
            if str(snapshot.get("attachment_id") or "").strip()
        ]
        if not attachment_ids:
            return
        try:
            from src.attachments.store import AttachmentStore

            store = AttachmentStore()
            placeholders = ",".join("?" for _ in attachment_ids)
            with closing(store._connect()) as conn:
                rows = conn.execute(
                    f"""
                    SELECT att.attachment_id, att.status, asset.parse_status
                    FROM chat_attachments att
                    LEFT JOIN chat_attachment_assets asset ON asset.asset_id = att.asset_id
                    WHERE att.attachment_id IN ({placeholders})
                    """,
                    attachment_ids,
                ).fetchall()
            statuses = {
                row["attachment_id"]: (row["status"], row["parse_status"] or "")
                for row in rows
            }
        except Exception:
            # Attachment history must remain readable if the optional
            # attachment database is temporarily unavailable.
            return
        for snapshot in snapshots:
            attachment_id = str(snapshot.get("attachment_id") or "").strip()
            status, parse_status = statuses.get(attachment_id, ("missing", ""))
            snapshot["deleted"] = status != "active"
            if status == "active" and parse_status:
                snapshot["parse_status_snapshot"] = parse_status

    def list_waiting_turns_for_asset(self, asset_id: str) -> list[ChatTurn]:
        """Waiting turns that reference attachments built from this asset."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN turn_attachments ta ON ta.turn_id = t.id
                WHERE ta.asset_id = ? AND t.status = 'waiting_for_attachments'
                ORDER BY t.created_at
                """,
                (asset_id,),
            ).fetchall()
        return [row_to_turn(row) for row in rows]

    def requeue_stale_turns(self, stale_after_seconds: int | None = None) -> int:
        """Converge abandoned worker claims (v3 §4.6).

        A stale turn with ``cancel_requested`` becomes ``cancelled`` and a turn
        that exhausted retries becomes ``failed``; both get their terminal
        events in the same transaction.  Only the remaining turns return to
        ``pending`` for an independent worker, and the count of those is the
        return value.
        """
        stale_after_seconds = max(30, int(stale_after_seconds or src.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS))
        now = utc_now()
        requeued = 0
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT id, cancel_requested, retry_count
                    FROM chat_turns
                    WHERE status IN ('streaming', 'cancelling')
                      AND worker_heartbeat_at IS NOT NULL
                      AND datetime(worker_heartbeat_at) < datetime('now', ?)
                    """,
                    (f"-{stale_after_seconds} seconds",),
                ).fetchall()
                for row in rows:
                    turn_id = str(row["id"])
                    cancelled = bool(row["cancel_requested"])
                    exhausted = int(row["retry_count"] or 0) >= 3
                    if not cancelled and not exhausted:
                        cursor = conn.execute(
                            """
                            UPDATE chat_turns
                            SET status = 'pending', worker_id = '', worker_heartbeat_at = NULL
                            WHERE id = ? AND status IN ('streaming', 'cancelling')
                            """,
                            (turn_id,),
                        )
                        requeued += int(cursor.rowcount or 0)
                        continue
                    status = "cancelled" if cancelled else "failed"
                    message = "已停止生成" if cancelled else "任务执行器失去心跳，超过最大重试次数"
                    conn.execute(
                        """
                        UPDATE chat_turns
                        SET status = ?, error_message = ?, finished_at = ?,
                            worker_id = '', worker_heartbeat_at = NULL
                        WHERE id = ? AND status IN ('streaming', 'cancelling')
                        """,
                        (status, message, now, turn_id),
                    )
                    conn.execute(
                        "UPDATE chat_messages SET content = ? WHERE id = ("
                        " SELECT assistant_message_id FROM chat_turns WHERE id = ?"
                        ") AND content = ''",
                        ("已停止生成" if cancelled else f"生成失败：{message}", turn_id),
                    )
                    events = (
                        [
                            ("error", {"message": "已停止生成", "cancelled": True}),
                            ("turn_end", {"status": "cancelled"}),
                        ]
                        if cancelled
                        else [
                            ("error", {"message": message}),
                            ("turn_end", {"status": "failed"}),
                        ]
                    )
                    for event_type, payload in events:
                        self._append_turn_event_in_connection(
                            conn, turn_id, event_type, payload, created_at=now
                        )
                # A requeued or converged turn is no longer running: drop the
                # session lease so the next claim can take over.
                conn.execute(
                    """
                    UPDATE conversation_execution_state
                    SET active_turn_id = '', active_attempt_id = '',
                        lease_owner = '', lease_expires_at = NULL,
                        revision = revision + 1, updated_at = ?
                    WHERE active_turn_id <> '' AND NOT EXISTS (
                        SELECT 1 FROM chat_turns t
                        WHERE t.id = conversation_execution_state.active_turn_id
                          AND t.status IN ('streaming', 'cancelling')
                    )
                    """,
                    (now,),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return requeued

    def list_pending_turn_work(self, limit: int = 8) -> list[tuple[ChatTurn, int]]:
        """Return queued work without claiming it; ``claim_turn`` remains atomic."""
        serialize = bool(getattr(src.settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True))
        head_filter = (
            """
                  AND NOT EXISTS (
                      SELECT 1 FROM chat_turns prior
                      WHERE prior.session_id = t.session_id
                        AND prior.status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                        AND prior.turn_sequence < t.turn_sequence
                  )
            """
            if serialize
            else ""
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT t.*, s.user_id
                FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                JOIN users u ON u.id = s.user_id
                WHERE t.status = 'pending' AND t.cancel_requested = 0
                  AND u.is_active = 1
                  {head_filter}
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

    def allocate_turn_sequence(self, session_id: int) -> int:
        """Allocate the next per-session turn sequence atomically.

        Reconciles against the maximum persisted sequence so a stale counter
        can never hand out a number history already used.
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT next_turn_sequence FROM chat_sessions WHERE id = ?",
                    (int(session_id),),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    raise KeyError("session not found")
                counter = (
                    int(row["next_turn_sequence"] or 0)
                    if "next_turn_sequence" in row.keys()
                    else 0
                )
                maximum = conn.execute(
                    "SELECT COALESCE(MAX(turn_sequence), 0) AS seq FROM chat_turns WHERE session_id = ?",
                    (int(session_id),),
                ).fetchone()
                allocated = max(counter if counter > 0 else 1, int(maximum["seq"] or 0) + 1)
                conn.execute(
                    "UPDATE chat_sessions SET next_turn_sequence = ? WHERE id = ?",
                    (allocated + 1, int(session_id)),
                )
                conn.execute("COMMIT")
                return allocated
            except KeyError:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def claim_turn(
        self,
        user_id: int,
        turn_id: str,
        worker_id: str = "",
        stale_after_seconds: int | None = None,
    ) -> ChatTurn | None:
        """Legacy adapter: structured claim result projected to ``ChatTurn | None``."""
        return self.claim_turn_result(
            user_id, turn_id, worker_id, stale_after_seconds=stale_after_seconds
        ).turn

    def claim_turn_result(
        self,
        user_id: int,
        turn_id: str,
        worker_id: str = "",
        stale_after_seconds: int | None = None,
    ) -> TurnClaimResult:
        """Claim a pending turn once; concurrent start requests are harmless.

        With ``CONVERSATION_SERIALIZE_SAME_THREAD`` enabled, only the earliest
        non-terminal turn of a session is claimable, and the claim carries a
        per-session lease plus an increasing fencing token.  Superseded
        attempts are rejected by the guarded write methods.
        """
        now = utc_now()
        stale_after_seconds = max(30, int(stale_after_seconds or src.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS))
        serialize = bool(getattr(src.settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True))
        lease_seconds = max(30, int(getattr(src.settings, "CHAT_TURN_LEASE_SECONDS", 120)))
        claim_started = time.monotonic()

        def _done(status: str, conflict: bool = False) -> None:
            try:
                from src.observability.metrics import (
                    record_conversation_claim,
                    record_same_thread_conflict,
                )

                record_conversation_claim(status=status, wait_s=time.monotonic() - claim_started)
                if conflict:
                    record_same_thread_conflict()
            except Exception:
                pass

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
                if row is None:
                    conn.execute("COMMIT")
                    _done("noop")
                    return TurnClaimResult(status="noop")
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
                    if row is not None and int(row["cancel_requested"] or 0) == 1 and row["status"] == "cancelled":
                        conn.execute("COMMIT")
                        _done("cancelled")
                        return TurnClaimResult(status="cancelled")
                    conn.execute("COMMIT")
                    _done("noop")
                    return TurnClaimResult(status="noop")
                if int(row["cancel_requested"] or 0) == 1:
                    # A pending turn whose cancel was never finalised (worker
                    # died before writing the terminal state) is converged here
                    # instead of being returned as a no-op forever (v3 §4.6).
                    conn.execute(
                        """
                        UPDATE chat_turns
                        SET status = 'cancelled',
                            error_message = COALESCE(NULLIF(error_message, ''), '已停止生成'),
                            finished_at = ?, worker_id = '', worker_heartbeat_at = NULL
                        WHERE id = ? AND status = 'pending'
                        """,
                        (now, turn_id),
                    )
                    conn.execute(
                        "UPDATE chat_messages SET content = '已停止生成' WHERE id = ("
                        " SELECT assistant_message_id FROM chat_turns WHERE id = ?"
                        ") AND content = ''",
                        (turn_id,),
                    )
                    for event_type, payload in (
                        ("error", {"message": "已停止生成", "cancelled": True}),
                        ("turn_end", {"status": "cancelled"}),
                    ):
                        self._append_turn_event_in_connection(
                            conn, turn_id, event_type, payload, created_at=now
                        )
                    conn.execute("COMMIT")
                    _done("cancelled")
                    return TurnClaimResult(status="cancelled")
                session_id = int(row["session_id"])
                fencing_token = 0
                generation = 0
                attempt_id = ""
                if serialize:
                    session_row = self._ensure_execution_state(conn, session_id)
                    if session_row is None:
                        conn.execute("COMMIT")
                        _done("stale")
                        return TurnClaimResult(status="stale")
                    # Strict per-session ordering: an earlier non-terminal turn
                    # keeps this one queued.
                    earlier = conn.execute(
                        """
                        SELECT id FROM chat_turns
                        WHERE session_id = ? AND id <> ?
                          AND status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                          AND turn_sequence < ?
                        ORDER BY turn_sequence
                        LIMIT 1
                        """,
                        (session_id, turn_id, int(row["turn_sequence"] or 0)),
                    ).fetchone()
                    if earlier is not None:
                        conn.execute("COMMIT")
                        _done("blocked", conflict=True)
                        return TurnClaimResult(status="blocked", blocker_turn_id=str(earlier["id"]))
                    active_turn = str(session_row["active_turn_id"] or "")
                    if active_turn and active_turn != turn_id and _lease_active(
                        session_row["lease_expires_at"], now
                    ):
                        active_status = conn.execute(
                            "SELECT status FROM chat_turns WHERE id = ?", (active_turn,)
                        ).fetchone()
                        if active_status is not None and str(active_status["status"]) in TURN_STATUSES_NON_TERMINAL:
                            conn.execute("COMMIT")
                            _done("blocked", conflict=True)
                            return TurnClaimResult(status="blocked", blocker_turn_id=active_turn)
                        # Terminal turn with a leftover lease (crash between
                        # completion and release): reclaim it and continue.
                        conn.execute(
                            """
                            UPDATE conversation_execution_state
                            SET active_turn_id = '', active_attempt_id = '',
                                lease_owner = '', lease_expires_at = NULL,
                                revision = revision + 1, updated_at = ?
                            WHERE session_id = ? AND active_turn_id = ?
                            """,
                            (now, session_id, active_turn),
                        )
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
                    exhausted = claimed is not None and int(claimed["retry_count"] or 0) >= 3
                    if exhausted:
                        conn.execute(
                            "UPDATE chat_turns SET status = 'failed', error_message = 'Exceeded maximum chat retries', finished_at = ? WHERE id = ?",
                            (now, turn_id),
                        )
                    conn.execute("COMMIT")
                    _done("exhausted" if exhausted else "noop")
                    return TurnClaimResult(status="retry_exhausted" if exhausted else "noop")
                if serialize:
                    attempt_id = f"attempt-{uuid.uuid4().hex}"
                    conn.execute(
                        """
                        UPDATE conversation_execution_state
                        SET active_turn_id = ?, active_attempt_id = ?,
                            lease_owner = ?, lease_expires_at = ?,
                            fencing_token = fencing_token + 1,
                            revision = revision + 1, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            turn_id,
                            attempt_id,
                            worker_id,
                            _iso_after(now, lease_seconds),
                            now,
                            session_id,
                        ),
                    )
                    session_state = self._ensure_execution_state(conn, session_id)
                    if session_state is None:
                        conn.execute("ROLLBACK")
                        _done("stale")
                        return TurnClaimResult(status="stale")
                    fencing_token = int(session_state["fencing_token"] or 0)
                    generation = int(session_state["generation"] or 0)
                conn.execute("COMMIT")
                _done("claimed")
                turn = row_to_turn(claimed)
                turn.fencing_token = fencing_token
                turn.session_generation = generation
                turn.attempt_id = attempt_id
                return TurnClaimResult(status="claimed", turn=turn)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _verify_turn_owner(
        conn,
        turn_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> bool:
        """True when this attempt still owns the turn's session execution."""
        row = conn.execute(
            """
            SELECT e.active_turn_id, e.lease_owner, e.fencing_token
            FROM chat_turns t
            JOIN conversation_execution_state e ON e.session_id = t.session_id
            WHERE t.id = ?
            """,
            (turn_id,),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["active_turn_id"] or "") == turn_id
            and str(row["lease_owner"] or "") == worker_id
            and int(row["fencing_token"] or 0) == int(fencing_token)
        )

    def release_turn_ownership(
        self,
        turn_id: str,
        worker_id: str = "",
        *,
        fencing_token: int | None = None,
    ) -> bool:
        """Release the session lease after the owning worker finished a turn.

        Called after the final SSE events are appended.  A crash between
        completion and release leaves a terminal turn whose lease is reclaimed
        by the next claim, so the next turn is never blocked permanently.
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT session_id FROM chat_turns WHERE id = ?", (turn_id,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return False
                if fencing_token is not None and not self._verify_turn_owner(
                    conn, turn_id, worker_id, fencing_token
                ):
                    conn.execute("COMMIT")
                    return False
                cursor = conn.execute(
                    """
                    UPDATE conversation_execution_state
                    SET active_turn_id = '', active_attempt_id = '',
                        lease_owner = '', lease_expires_at = NULL,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ? AND active_turn_id = ?
                    """,
                    (utc_now(), int(row["session_id"]), turn_id),
                )
                conn.execute("COMMIT")
                return int(cursor.rowcount or 0) > 0
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def touch_turn_worker(self, turn_id: str, worker_id: str, *, fencing_token: int | None = None) -> bool:
        now = utc_now()
        with closing(self._connect()) as conn:
            if fencing_token is None:
                conn.execute(
                    """
                    UPDATE chat_turns SET worker_heartbeat_at = ?
                    WHERE id = ? AND worker_id = ? AND status IN ('streaming', 'cancelling')
                    """,
                    (now, turn_id, worker_id),
                )
                return True
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not self._verify_turn_owner(conn, turn_id, worker_id, fencing_token):
                    conn.execute("COMMIT")
                    try:
                        from src.observability.metrics import record_governor_lease_lost

                        record_governor_lease_lost(resource="chat", reason="stale_owner")
                    except Exception:
                        pass
                    return False
                renew_seconds = max(5, int(getattr(src.settings, "CHAT_TURN_LEASE_RENEW_SECONDS", 15)))
                conn.execute(
                    """
                    UPDATE chat_turns SET worker_heartbeat_at = ?
                    WHERE id = ? AND worker_id = ? AND status IN ('streaming', 'cancelling')
                    """,
                    (now, turn_id, worker_id),
                )
                row = conn.execute("SELECT session_id FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                if row is not None:
                    conn.execute(
                        """
                        UPDATE conversation_execution_state
                        SET lease_expires_at = ?, revision = revision + 1, updated_at = ?
                        WHERE session_id = ? AND active_turn_id = ? AND lease_owner = ?
                        """,
                        (_iso_after(now, renew_seconds), now, int(row["session_id"]), turn_id, worker_id),
                    )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_checkpoint_refs(self, session_id: int) -> dict[str, Any]:
        """Recoverable/committed checkpoint pointers plus execution identity."""
        empty: dict[str, Any] = {
            "recoverable": None,
            "committed": None,
            "generation": 0,
            "fencing_token": 0,
        }
        with closing(self._connect()) as conn:
            state = conn.execute(
                """
                SELECT recoverable_checkpoint_ref, committed_checkpoint_ref, generation, fencing_token
                FROM conversation_execution_state WHERE session_id = ?
                """,
                (int(session_id),),
            ).fetchone()
        if state is None:
            return empty

        def _load(raw: Any) -> dict | None:
            try:
                value = json.loads(str(raw or ""))
            except (TypeError, ValueError):
                return None
            return value if isinstance(value, dict) else None

        return {
            "recoverable": _load(state["recoverable_checkpoint_ref"]),
            "committed": _load(state["committed_checkpoint_ref"]),
            "generation": int(state["generation"] or 0),
            "fencing_token": int(state["fencing_token"] or 0),
        }

    def update_recoverable_checkpoint_ref(
        self,
        turn_id: str,
        worker_id: str,
        fencing_token: int,
        ref: dict,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """CAS-register the latest durable checkpoint for the owning attempt.

        Runs in the authority transaction: the pointer can only move forward
        while the attempt still owns the session and the generation matches.
        """
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT session_id FROM chat_turns WHERE id = ?", (turn_id,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return False
                if not self._verify_turn_owner(conn, turn_id, worker_id, fencing_token):
                    raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
                session_id = int(row["session_id"])
                if expected_generation is not None:
                    state = self._ensure_execution_state(conn, session_id)
                    if state is None or int(state["generation"] or 0) != int(expected_generation):
                        raise StaleTurnOwner("checkpoint pointer was revoked by cancel or takeover")
                conn.execute(
                    """
                    UPDATE conversation_execution_state
                    SET recoverable_checkpoint_ref = ?, revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        json.dumps(ref, ensure_ascii=False, sort_keys=True),
                        now,
                        session_id,
                    ),
                )
                conn.execute("COMMIT")
                return True
            except StaleTurnOwner:
                conn.execute("ROLLBACK")
                self._record_stale_write("checkpoint_pointer")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _append_turn_event_in_connection(
        conn,
        turn_id: str,
        event_type: str,
        payload: dict,
        *,
        created_at: str | None = None,
    ) -> ChatTurnEvent:
        """Allocate the next seq and persist one event in a caller-owned transaction.

        Callers already hold the authority write transaction and have verified
        ownership; the helper performs no guard of its own.
        """
        now = created_at or utc_now()
        row = conn.execute("SELECT last_event_seq FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError("turn not found")
        seq = int(row["last_event_seq"]) + 1
        conn.execute(
            "INSERT INTO chat_turn_events (turn_id, seq, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (turn_id, seq, event_type, json.dumps(payload, ensure_ascii=False, default=str), now),
        )
        conn.execute("UPDATE chat_turns SET last_event_seq = ? WHERE id = ?", (seq, turn_id))
        return ChatTurnEvent(turn_id=turn_id, seq=seq, event_type=event_type, payload=payload, created_at=now)

    def append_turn_event(
        self,
        turn_id: str,
        event_type: str,
        payload: dict,
        *,
        worker_id: str = "",
        fencing_token: int | None = None,
    ) -> ChatTurnEvent:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if fencing_token is not None and not self._verify_turn_owner(
                    conn, turn_id, worker_id, fencing_token
                ):
                    raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
                event = self._append_turn_event_in_connection(conn, turn_id, event_type, payload)
                conn.execute("COMMIT")
            except StaleTurnOwner:
                conn.execute("ROLLBACK")
                self._record_stale_write("append_event")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return event

    @staticmethod
    def _record_stale_write(operation: str) -> None:
        try:
            from src.observability.metrics import counter

            counter(
                "hdb.conversation.stale_write_rejected",
                attributes={"operation": operation},
                description="Authoritative conversation writes rejected from superseded attempts",
            )
        except Exception:
            pass

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
        *,
        worker_id: str = "",
        fencing_token: int | None = None,
        expected_generation: int | None = None,
        committed_checkpoint_ref: dict | None = None,
    ) -> ChatTurn:
        """Legacy adapter: finalize a turn without building terminal events."""
        return self.complete_turn_result(
            user_id,
            turn_id,
            answer,
            summary,
            footer,
            metrics,
            worker_id=worker_id,
            fencing_token=fencing_token,
            expected_generation=expected_generation,
            committed_checkpoint_ref=committed_checkpoint_ref,
        ).turn

    def complete_turn_result(
        self,
        user_id: int,
        turn_id: str,
        answer: str,
        summary: dict,
        footer: str = "",
        metrics: dict | None = None,
        *,
        worker_id: str = "",
        fencing_token: int | None = None,
        expected_generation: int | None = None,
        committed_checkpoint_ref: dict | None = None,
        terminal_events_builder: Callable[[ChatTurn], Sequence[tuple[str, dict]]] | None = None,
        domain_writer: Callable[[sqlite3.Connection, ChatTurn], None] | None = None,
    ) -> TurnCompletion:
        # Initialize the export schema before opening the completion
        # transaction. The actual snapshot/jobs are inserted through the
        # caller's connection below, so a completed answer and its requested
        # exports commit or roll back together.
        existing_turn = self.get_turn(user_id, turn_id)
        if existing_turn is None:
            raise KeyError("turn not found")
        # Export requests are completed by the server-side export worker.  A
        # stale model answer may still contain the old browser-print HTML
        # workaround; normalize it before persisting the canonical turn so
        # the chat UI, snapshot and every renderer share the same clean text.
        from src.result_exports.answer import normalize_export_answer

        answer = normalize_export_answer(answer, existing_turn.export_plan)
        export_store = None
        if existing_turn.export_plan:
            from src.result_exports.store import ResultExportStore

            export_store = ResultExportStore(self.db_path)
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
                if fencing_token is not None:
                    if not self._verify_turn_owner(conn, turn_id, worker_id, fencing_token):
                        raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
                    if expected_generation is not None:
                        current = self._ensure_execution_state(conn, int(row["session_id"]))
                        if current is None or int(current["generation"] or 0) != int(expected_generation):
                            raise StaleTurnOwner("turn completion was revoked by cancel or takeover")
                    if committed_checkpoint_ref is not None:
                        # The canonical committed pointer moves in the same
                        # authoritative transaction that publishes the answer.
                        conn.execute(
                            """
                            UPDATE conversation_execution_state
                            SET committed_checkpoint_ref = ?, revision = revision + 1, updated_at = ?
                            WHERE session_id = ?
                            """,
                            (
                                json.dumps(committed_checkpoint_ref, ensure_ascii=False, sort_keys=True),
                                now,
                                int(row["session_id"]),
                            ),
                        )
                conn.execute("UPDATE chat_messages SET content = ? WHERE id = ?", (answer, row["assistant_message_id"]))
                conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = 'completed', answer = ?, summary_json = ?, footer = ?, metrics_json = ?, finished_at = ?,
                        worker_id = '', worker_heartbeat_at = NULL
                    -- completed/cancelled 为终态不可复活；failed(SSE stale 临时标记)
                    -- 允许被 worker 真实的完成结果收编（见 test_stale_turn_is_reclaimed）。
                    WHERE id = ? AND status NOT IN ('completed', 'cancelled')
                    """,
                    (answer, json.dumps(summary, ensure_ascii=False, default=str), footer,
                     json.dumps(metrics or {}, ensure_ascii=False, default=str), now, turn_id),
                )
                conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, row["session_id"]))
                completed = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                completed_turn = row_to_turn(completed)
                # Terminal events share this transaction with the answer and
                # status (v3 §4.7); the caller publishes them after commit.
                terminal_events: list[ChatTurnEvent] = []
                if terminal_events_builder is not None:
                    for event_type, payload in terminal_events_builder(completed_turn):
                        terminal_events.append(
                            self._append_turn_event_in_connection(
                                conn, turn_id, event_type, payload, created_at=now
                            )
                        )
                if export_store is not None and completed_turn.export_plan:
                    from src.result_exports.content import envelope_from_turn

                    export_store.enqueue_completed_turn_in_connection(
                        conn,
                        owner_user_id=user_id,
                        tenant_id="default",
                        department_id=completed_turn.department_id,
                        knowledge_base_name=(
                            "" if completed_turn.kb_name == GENERAL_CHAT_KB_NAME else completed_turn.kb_name
                        ),
                        session_id=completed_turn.session_id,
                        turn_id=completed_turn.id,
                        assistant_message_id=completed_turn.assistant_message_id,
                        envelope=envelope_from_turn(completed_turn),
                        export_plan=completed_turn.export_plan,
                    )
                # Project Reflection is deliberately outside the realtime
                # query path, but its durable outbox entry is part of the
                # same transaction as the completed turn.  Generic chats and
                # incomplete scopes never map to a shared namespace.
                session_auto_memory = (
                    int(row["session_auto_memory"] or 0) if "session_auto_memory" in row.keys() else 1
                )
                if (
                    getattr(src.settings, "MEMORY_ENABLED", True)
                    and getattr(src.settings, "MEMORY_EXTRACTION_ENABLED", True)
                    and session_auto_memory == 1
                    and row["department_id"] not in (None, "")
                    and row["kb_id"] not in (None, "")
                ):
                    from src.memory.catalog import scope_fingerprint
                    from src.memory.jobs import enqueue_project_reflection_in_connection

                    debounce = max(0, int(getattr(src.settings, "MEMORY_DEBOUNCE_SECONDS", 300)))
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
                if domain_writer is not None:
                    # Domain commit (export snapshot, memory outbox, ...) shares
                    # the terminal transaction (v3 §5.3): any failure rolls the
                    # answer, status and terminal events back together.
                    domain_writer(conn, completed_turn)
                conn.execute("COMMIT")
                return TurnCompletion(turn=completed_turn, terminal_events=terminal_events)
            except StaleTurnOwner:
                conn.execute("ROLLBACK")
                self._record_stale_write("complete_turn")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def fail_turn(
        self,
        user_id: int,
        turn_id: str,
        message: str,
        cancelled: bool = False,
        *,
        worker_id: str = "",
        fencing_token: int | None = None,
    ) -> ChatTurn | None:
        """Legacy adapter: finalize a turn without building terminal events."""
        return self.fail_turn_result(
            user_id,
            turn_id,
            message,
            cancelled,
            worker_id=worker_id,
            fencing_token=fencing_token,
        ).turn

    def fail_turn_result(
        self,
        user_id: int,
        turn_id: str,
        message: str,
        cancelled: bool = False,
        *,
        worker_id: str = "",
        fencing_token: int | None = None,
        terminal_events_builder: Callable[[ChatTurn], Sequence[tuple[str, dict]]] | None = None,
        domain_writer: Callable[[sqlite3.Connection, ChatTurn], None] | None = None,
    ) -> TurnFinalization:
        now = utc_now()
        status = "cancelled" if cancelled else "failed"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT t.assistant_message_id, t.session_id FROM chat_turns t
                    JOIN chat_sessions s ON s.id = t.session_id
                    WHERE t.id = ? AND s.user_id = ?
                    """,
                    (turn_id, user_id),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return TurnFinalization(turn=None)
                if fencing_token is not None and not self._verify_turn_owner(
                    conn, turn_id, worker_id, fencing_token
                ):
                    raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
                cursor = conn.execute(
                """
                UPDATE chat_turns SET status = ?, error_message = ?, finished_at = ?,
                    worker_id = '', worker_heartbeat_at = NULL
                WHERE id = ? AND status NOT IN ('completed', 'failed', 'cancelled') AND session_id IN (
                    SELECT id FROM chat_sessions
                    WHERE user_id = ?
                )
                """,
                (status, message[:1000], now, turn_id, user_id),
                )
                if cursor.rowcount == 0:
                    conn.execute("COMMIT")
                    return TurnFinalization(turn=None)
                placeholder = "已停止生成" if cancelled else f"生成失败：{message[:500]}"
                conn.execute(
                    "UPDATE chat_messages SET content = ? WHERE id = ? AND content = ''",
                    (placeholder, row["assistant_message_id"]),
                )
                completed = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
                finalized_turn = row_to_turn(completed)
                # Terminal events share this transaction with the terminal
                # status (v3 §4.7); the caller publishes them after commit.
                terminal_events: list[ChatTurnEvent] = []
                if terminal_events_builder is not None:
                    for event_type, payload in terminal_events_builder(finalized_turn):
                        terminal_events.append(
                            self._append_turn_event_in_connection(
                                conn, turn_id, event_type, payload, created_at=now
                            )
                        )
                if domain_writer is not None:
                    domain_writer(conn, finalized_turn)
                conn.execute("COMMIT")
                return TurnFinalization(turn=finalized_turn, terminal_events=terminal_events)
            except StaleTurnOwner:
                conn.execute("ROLLBACK")
                self._record_stale_write("fail_turn")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def is_turn_worker_stale(self, user_id: int, turn_id: str, stale_after_seconds: int | None = None) -> bool:
        stale_after_seconds = max(30, int(stale_after_seconds or src.settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS))
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
                target = conn.execute(
                    """
                    SELECT t.status, t.session_id FROM chat_turns t
                    JOIN chat_sessions s ON s.id = t.session_id
                    WHERE t.id = ? AND s.user_id = ?
                    """,
                    (turn_id, user_id),
                ).fetchone()
                if target is not None and str(target["status"]) in {"streaming", "cancelling"}:
                    # Revoke the business commit before signalling the worker:
                    # an in-flight answer must not be accepted after a cancel.
                    self._bump_session_generation(conn, int(target["session_id"]))
                conn.execute(
                """
                UPDATE chat_turns SET cancel_requested = 1,
                  status = CASE
                    WHEN status = 'pending' THEN 'cancelled'
                    WHEN status = 'streaming' THEN 'cancelling'
                    WHEN status = 'waiting_for_attachments' THEN 'cancelled'
                    ELSE status
                  END,
                  finished_at = CASE WHEN status IN ('pending', 'waiting_for_attachments') THEN ? ELSE finished_at END,
                  error_message = CASE WHEN status IN ('pending', 'waiting_for_attachments') THEN '已停止生成' ELSE error_message END
                WHERE id = ? AND session_id IN (
                    SELECT id FROM chat_sessions
                    WHERE user_id = ?
                )
                  AND status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
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
                if target is not None and str(target["status"]) in {"pending", "waiting_for_attachments"}:
                    # An unstarted turn releases any stale lease so the next turn
                    # of the session can be claimed.  A running turn keeps its
                    # lease until the owning worker finalises it.
                    conn.execute(
                        """
                        UPDATE conversation_execution_state
                        SET active_turn_id = '', active_attempt_id = '',
                            lease_owner = '', lease_expires_at = NULL,
                            revision = revision + 1, updated_at = ?
                        WHERE session_id = ? AND active_turn_id = ?
                        """,
                        (now, int(target["session_id"]), turn_id),
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


def _iso_after(timestamp: str, seconds: int) -> str:
    """Return ``timestamp`` plus ``seconds`` as an ISO string (UTC-safe)."""
    try:
        base = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=max(0, int(seconds)))).isoformat()


def _lease_active(expires_at: Any, now: str) -> bool:
    """True when a stored lease expiry is still in the future."""
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at))
        current = datetime.fromisoformat(str(now))
    except (TypeError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expires > current


def row_to_session(row) -> ChatSession:
    try:
        raw_context = json.loads(row["document_context_json"] or "{}") if "document_context_json" in row.keys() else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_context = {}
    context = raw_context if isinstance(raw_context, dict) and raw_context else None
    return ChatSession(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        kb_name=row["kb_name"],
        department_id=row["department_id"],
        kb_id=row["kb_id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        document_context=context,
    )


def row_to_message(row) -> ChatMessage:
    content = row["content"]
    footer = row["footer"] if "footer" in row.keys() else ""
    memory_context: list[dict] = []
    citations: list[dict] = []
    if "turn_summary" in row.keys():
        try:
            summary = json.loads(row["turn_summary"] or "{}")
        except (TypeError, ValueError):
            summary = {}
        candidate = summary.get("memory_context") if isinstance(summary, dict) else None
        if isinstance(candidate, list):
            memory_context = [item for item in candidate if isinstance(item, dict)]
        evidence = summary.get("evidence") if isinstance(summary, dict) else None
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                locator = item.get("locator")
                citations.append(
                    {
                        "id": item.get("id"),
                        "source_name": item.get("source_name"),
                        "content_kind": item.get("content_kind"),
                        "locator": locator if isinstance(locator, dict) else {},
                        # 内容只保留预览长度，payload 保持轻量。
                        "content": str(item.get("content") or "")[:240],
                        "metadata": {
                            "citation_number": (item.get("metadata") or {}).get(
                                "citation_number"
                            )
                            if isinstance(item.get("metadata"), dict)
                            else None,
                        },
                    }
                )
    try:
        raw_context = json.loads(row["turn_document_context"] or "{}") if "turn_document_context" in row.keys() else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_context = {}
    document_context = raw_context if isinstance(raw_context, dict) and raw_context else None
    legacy_suffix = f"\n\n---\n{footer}" if footer else ""
    if legacy_suffix and content.endswith(legacy_suffix):
        content = content[:-len(legacy_suffix)]
    return ChatMessage(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        turn_id=(str(row["turn_id"]) if row["turn_id"] else None) if "turn_id" in row.keys() else None,
        role=row["role"],
        content=content,
        footer=footer,
        created_at=row["created_at"],
        edited_at=row["edited_at"] if "edited_at" in row.keys() else None,
        redacted=bool(row["redacted"]) if "redacted" in row.keys() else False,
        memory_context=memory_context,
        document_context=document_context,
        citations=citations,
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
    try:
        raw_document_context = (
            json.loads(row["document_context_json"] or "{}")
            if "document_context_json" in row.keys() else {}
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_document_context = {}
    document_context = raw_document_context if isinstance(raw_document_context, dict) and raw_document_context else None
    try:
        raw_export_plan = (
            json.loads(row["export_plan_json"] or "{}")
            if "export_plan_json" in row.keys() else {}
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_export_plan = {}
    export_plan = raw_export_plan if isinstance(raw_export_plan, dict) else {}
    try:
        raw_required = (
            json.loads(row["required_attachment_ids"] or "[]")
            if "required_attachment_ids" in row.keys() else []
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_required = []
    required_ids = [str(item) for item in raw_required] if isinstance(raw_required, list) else []
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
        wiki_assist=bool(row["wiki_assist"]) if "wiki_assist" in row.keys() else True,
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
        document_context=document_context,
        export_plan=export_plan,
        source_scope=row["source_scope"] if "source_scope" in row.keys() else "auto",
        required_attachment_ids=required_ids,
        turn_sequence=(
            int(row["turn_sequence"])
            if "turn_sequence" in row.keys() and row["turn_sequence"] is not None
            else 0
        ),
    )
