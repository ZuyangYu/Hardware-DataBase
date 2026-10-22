"""PostgreSQL execution store (v3 §4/§5).

Implements the execution protocol on the frozen control-plane schema with
psycopg3 and a bounded connection pool:

- the database clock is the only timebase (``clock_timestamp()``);
- the lock order is ``conversation_execution_state -> chat_turns ->
  chat_turn_events``, with per-candidate ``FOR UPDATE SKIP LOCKED`` so a hot
  session never blocks another session's claim;
- terminal events commit in the same transaction as the answer/status and an
  optional ``domain_writer`` callback (export snapshot, memory outbox);
- stale turns converge to ``cancelled``/``failed`` instead of silently
  requeueing forever.

``attempt_id`` is enforced when supplied; an empty attempt id keeps legacy
callers working with the owner+token check only.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from src.core.conversation import (
    GENERAL_CHAT_KB_NAME,
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnEvent,
    StaleTurnOwner,
    TurnClaimResult,
    TurnCompletion,
    TurnFinalization,
    row_to_message,
    row_to_session,
)

from .base import RequeueSummary

_NON_TERMINAL = ("pending", "streaming", "cancelling", "waiting_for_attachments")
_LEASE_SECONDS = 120


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value


def _timestamp(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _turn_from_row(row: dict) -> ChatTurn:
    return ChatTurn(
        id=str(row["id"]),
        session_id=int(row["session_id"]),
        user_message_id=int(row["user_message_id"]),
        assistant_message_id=int(row["assistant_message_id"]),
        kb_name=str(row["kb_name"]),
        department_id=row["department_id"],
        kb_id=row["kb_id"],
        query=str(row["query"]),
        query_mode=str(row.get("query_mode") or "fast"),
        wiki_assist=bool(row.get("wiki_assist")),
        status=str(row["status"]),
        client_request_id=row.get("client_request_id"),
        cancel_requested=bool(row.get("cancel_requested")),
        last_event_seq=int(row.get("last_event_seq") or 0),
        answer=str(row.get("answer") or ""),
        summary=_json_value(row.get("summary_json"), {}),
        footer=str(row.get("footer") or ""),
        metrics=_json_value(row.get("metrics_json"), {}),
        trace_context={
            str(key): str(value)
            for key, value in (_json_value(row.get("trace_context_json"), {}) or {}).items()
            if value
        },
        error_message=str(row.get("error_message") or ""),
        worker_id=str(row.get("worker_id") or ""),
        worker_heartbeat_at=_timestamp(row.get("worker_heartbeat_at")),
        retry_count=int(row.get("retry_count") or 0),
        created_at=_timestamp(row["created_at"]),
        started_at=_timestamp(row.get("started_at")),
        finished_at=_timestamp(row.get("finished_at")),
        document_context=_json_value(row.get("document_context_json"), None) or None,
        export_plan=_json_value(row.get("export_plan_json"), {}),
        source_scope=str(row.get("source_scope") or "auto"),
        required_attachment_ids=[
            str(item)
            for item in (_json_value(row.get("required_attachment_ids"), []) or [])
        ],
        turn_sequence=int(row.get("turn_sequence") or 0),
    )


def _sqlite_like_row(row: dict) -> dict:
    """Shape a PostgreSQL row like the SQLite rows the shared projections expect.

    JSON/JSONB values become JSON strings and timestamps become ISO strings so
    ``row_to_message``/``row_to_session`` parse exactly as they do on SQLite.
    """
    converted: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            converted[key] = value.isoformat()
        elif isinstance(value, (dict, list)):
            converted[key] = json.dumps(value, ensure_ascii=False, default=str)
        else:
            converted[key] = value
    return converted


def _message_from_row(row: dict) -> ChatMessage:
    return row_to_message(_sqlite_like_row(row))


def _session_from_row(row: dict) -> ChatSession:
    return row_to_session(_sqlite_like_row(row))


def _event_from_row(row: dict) -> ChatTurnEvent:
    return ChatTurnEvent(
        turn_id=str(row["turn_id"]),
        seq=int(row["seq"]),
        event_type=str(row["event_type"]),
        payload=_json_value(row.get("payload_json"), {}),
        created_at=_timestamp(row["created_at"]),
    )


class PostgresExecutionStore:
    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        pool_timeout: float = 10.0,
    ) -> None:
        self.dsn = str(dsn)
        self._pool = ConnectionPool(
            self.dsn,
            min_size=max(0, int(min_size)),
            max_size=max(1, int(max_size)),
            timeout=float(pool_timeout),
            open=True,
            kwargs={
                "row_factory": dict_row,
                "options": "-c TimeZone=UTC -c statement_timeout=30000",
            },
        )

    # -- infrastructure -----------------------------------------------------

    def list_waiting_turn_ids(self, attachment_ids: list[str] | None = None, *, limit: int = 100) -> list[str]:
        bounded_limit = max(1, min(int(limit), 1000))
        with self._tx() as conn:
            if attachment_ids is not None:
                if not attachment_ids:
                    return []
                rows = conn.execute(
                    """SELECT DISTINCT t.id, t.created_at FROM chat_turns t
                       JOIN turn_attachments ta ON ta.turn_id = t.id
                       WHERE t.status = 'waiting_for_attachments' AND ta.attachment_id = ANY(%s)
                       ORDER BY t.created_at, t.id LIMIT %s""",
                    (attachment_ids, bounded_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM chat_turns WHERE status = 'waiting_for_attachments' ORDER BY created_at, id LIMIT %s",
                    (bounded_limit,),
                ).fetchall()
        return [str(row["id"]) for row in rows]

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn

    @staticmethod
    def _owns(
        state: dict | None,
        turn_id: str,
        worker_id: str,
        attempt_id: str,
        fencing_token: int,
    ) -> bool:
        if state is None:
            return False
        if str(state.get("active_turn_id") or "") != turn_id:
            return False
        if str(state.get("lease_owner") or "") != worker_id:
            return False
        if int(state.get("fencing_token") or 0) != int(fencing_token):
            return False
        if attempt_id and str(state.get("active_attempt_id") or "") != attempt_id:
            return False
        return True

    @staticmethod
    def _load_state(conn, session_id: int, *, skip_locked: bool = False) -> dict | None:
        suffix = " FOR UPDATE SKIP LOCKED" if skip_locked else " FOR UPDATE"
        return conn.execute(
            "SELECT * FROM conversation_execution_state WHERE session_id = %s" + suffix,
            (int(session_id),),
        ).fetchone()

    @staticmethod
    def _append_event_locked(
        conn, turn_id: str, event_type: str, payload: dict
    ) -> ChatTurnEvent:
        row = conn.execute(
            """
            WITH bump AS (
                UPDATE chat_turns SET last_event_seq = last_event_seq + 1
                WHERE id = %s
                RETURNING last_event_seq
            )
            INSERT INTO chat_turn_events (turn_id, seq, event_type, payload_json, created_at)
            SELECT %s, last_event_seq, %s, %s, clock_timestamp() FROM bump
            RETURNING turn_id, seq, event_type, payload_json, created_at
            """,
            (turn_id, turn_id, event_type, Jsonb(payload)),
        ).fetchone()
        if row is None:
            raise KeyError("turn not found")
        return _event_from_row(row)

    @staticmethod
    def _clear_state(conn, session_id: int, turn_id: str) -> None:
        conn.execute(
            """
            UPDATE conversation_execution_state
            SET active_turn_id = '', active_attempt_id = '', lease_owner = '',
                lease_expires_at = NULL, revision = revision + 1, updated_at = clock_timestamp()
            WHERE session_id = %s AND active_turn_id = %s
            """,
            (int(session_id), turn_id),
        )

    def _converge_cancelled_locked(
        self,
        conn,
        turn_id: str,
        session_id: int,
        assistant_message_id: int | None,
        *,
        emit_events: bool = True,
    ) -> None:
        """Finalize a cancelled turn and emit its terminal event pair.

        ``emit_events=False`` is kept for internal convergence paths that
        already published the terminal events; the user-facing cancel path
        always emits them so replay subscribers close cleanly.
        """
        cursor = conn.execute(
            """
            UPDATE chat_turns
            SET status = 'cancelled',
                error_message = COALESCE(NULLIF(error_message, ''), '已停止生成'),
                finished_at = clock_timestamp(), worker_id = '', worker_heartbeat_at = NULL
            WHERE id = %s AND status IN ('pending', 'streaming', 'cancelling')
            """,
            (turn_id,),
        )
        if int(cursor.rowcount or 0) == 0:
            # Another request already finalised the turn; do not append a
            # second terminal event pair.
            return
        if assistant_message_id is not None:
            conn.execute(
                "UPDATE chat_messages SET content = '已停止生成' WHERE id = %s AND content = ''",
                (int(assistant_message_id),),
            )
        if emit_events:
            self._append_event_locked(
                conn, turn_id, "error", {"message": "已停止生成", "cancelled": True}
            )
            self._append_event_locked(conn, turn_id, "turn_end", {"status": "cancelled"})
        self._clear_state(conn, session_id, turn_id)

    # -- contract -----------------------------------------------------------

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
        query = str(query or "").strip()
        if not query:
            raise ValueError("query must not be empty")
        request_id = (client_request_id or "").strip()[:128] or None
        query_mode = query_mode if query_mode in {"fast", "deep"} else "fast"
        if hasattr(document_context, "model_dump"):
            document_context = document_context.model_dump(mode="json")
        if document_context is not None and not isinstance(document_context, dict):
            raise ValueError("document_context must be an object")
        document = document_context or {}
        attachment_ids = [
            str(item or "").strip()
            for item in (attachment_ids or [])
            if str(item or "").strip()
        ]
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
            from src.attachments.service import AttachmentService

            service = AttachmentService()
            refs, blocked = service.resolve_refs_for_turn(
                user_id=user_id, session_id=session_id, attachment_ids=attachment_ids
            )
            if blocked:
                raise ValueError("attachment_unavailable: " + ", ".join(blocked[:10]))
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
        export_plan_payload = (
            export_plan.to_dict() if export_plan is not None else {}
        )
        waiting = any(
            ref.get("parse_status") in {"queued", "running"} for ref in attachment_refs
        )
        initial_status = "waiting_for_attachments" if waiting else "pending"
        required_ids = [ref["attachment_id"] for ref in attachment_refs]

        with self._tx() as conn:
            session_row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = %s AND user_id = %s FOR UPDATE",
                (int(session_id), int(user_id)),
            ).fetchone()
            if session_row is None:
                raise PermissionError("chat session does not belong to current user")
            if request_id:
                existing = conn.execute(
                    "SELECT * FROM chat_turns WHERE session_id = %s AND client_request_id = %s",
                    (int(session_id), request_id),
                ).fetchone()
                if existing is not None:
                    return _turn_from_row(existing)
            turn_sequence = self._allocate_sequence_locked(conn, session_id)
            user_message = conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at)"
                " VALUES (%s, 'user', %s, clock_timestamp()) RETURNING id",
                (int(session_id), query),
            ).fetchone()
            assistant_message = conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at)"
                " VALUES (%s, 'assistant', '', clock_timestamp()) RETURNING id",
                (int(session_id),),
            ).fetchone()
            title = query.replace("\n", " ")[:32] or "新对话"
            conn.execute(
                """
                UPDATE chat_sessions
                SET updated_at = clock_timestamp(),
                    title = CASE WHEN title = '新对话' THEN %s ELSE title END,
                    document_context_json = CASE
                        WHEN %s = '{}'::jsonb THEN document_context_json
                        ELSE %s
                    END
                WHERE id = %s
                """,
                (title, Jsonb(document), Jsonb(document), int(session_id)),
            )
            turn_id = str(uuid.uuid4())
            row = conn.execute(
                """
                INSERT INTO chat_turns (
                    id, session_id, user_message_id, assistant_message_id, kb_name,
                    department_id, kb_id, query, query_mode, status, client_request_id,
                    turn_sequence, trace_context_json, document_context_json,
                    export_plan_json, source_scope, required_attachment_ids, wiki_assist,
                    created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, clock_timestamp()
                ) RETURNING *
                """,
                (
                    turn_id,
                    int(session_id),
                    int(user_message["id"]),
                    int(assistant_message["id"]),
                    str(session_row["kb_name"]),
                    session_row["department_id"],
                    session_row["kb_id"],
                    query,
                    query_mode,
                    initial_status,
                    request_id,
                    int(turn_sequence),
                    Jsonb(trace_context or {}),
                    Jsonb(document),
                    Jsonb(export_plan_payload),
                    effective_scope,
                    Jsonb(required_ids),
                    bool(wiki_assist),
                ),
            ).fetchone()
            for ordinal, ref in enumerate(attachment_refs):
                conn.execute(
                    """
                    INSERT INTO turn_attachments (
                        turn_id, attachment_id, ordinal, filename_snapshot,
                        media_type_snapshot, usage_hint_snapshot,
                        content_hash_snapshot, parser_version_snapshot,
                        asset_id, parse_status_snapshot
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (turn_id, attachment_id) DO NOTHING
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
            turn = _turn_from_row(row)
            turn.attachments = attachment_refs
            return turn

    @staticmethod
    def _allocate_sequence_locked(conn, session_id: int) -> int:
        """Allocate the next sequence while the session row lock is held.

        Reconciles against the persisted maximum so a stale counter can never
        hand out a used number (parity with ``allocate_turn_sequence``).
        """
        row = conn.execute(
            """
            UPDATE chat_sessions
            SET next_turn_sequence = GREATEST(
                    next_turn_sequence,
                    (SELECT COALESCE(MAX(turn_sequence), 0) + 1
                     FROM chat_turns WHERE session_id = %s)
                ) + 1
            WHERE id = %s
            RETURNING next_turn_sequence - 1 AS allocated
            """,
            (int(session_id), int(session_id)),
        ).fetchone()
        if row is None:
            raise KeyError("session not found")
        return int(row["allocated"])

    def allocate_turn_sequence(self, session_id: int) -> int:
        with self._tx() as conn:
            conn.execute(
                "SELECT id FROM chat_sessions WHERE id = %s FOR UPDATE",
                (int(session_id),),
            )
            return self._allocate_sequence_locked(conn, session_id)

    def get_turn(self, turn_id: str) -> ChatTurn | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM chat_turns WHERE id = %s", (str(turn_id),)
            ).fetchone()
        return _turn_from_row(row) if row is not None else None

    def list_turn_events(self, turn_id: str, after_seq: int = 0) -> list[ChatTurnEvent]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_turn_events WHERE turn_id = %s AND seq > %s ORDER BY seq",
                (str(turn_id), max(0, int(after_seq))),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    # -- read parity with ConversationService -------------------------------

    def get_session(self, user_id: int, session_id: int) -> ChatSession | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = %s AND user_id = %s",
                (int(session_id), int(user_id)),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def get_session_unscoped(self, session_id: int) -> ChatSession | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = %s", (int(session_id),)
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def list_messages(self, user_id: int, session_id: int) -> list[ChatMessage]:
        if self.get_session(user_id, session_id) is None:
            return []
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT m.*, COALESCE(t.footer, '') AS footer, t.id AS turn_id,
                       COALESCE(t.summary_json, '{}'::jsonb) AS turn_summary,
                       COALESCE(t.document_context_json, '{}'::jsonb) AS turn_document_context
                FROM chat_messages m
                LEFT JOIN chat_turns t
                  ON t.assistant_message_id = m.id OR t.user_message_id = m.id
                WHERE m.session_id = %s
                ORDER BY m.id
                """,
                (int(session_id),),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def history_before_turn(
        self, user_id: int, turn_id: str, limit: int = 5
    ) -> list[tuple[str, str]]:
        with self._tx() as conn:
            located = conn.execute(
                """
                SELECT t.session_id, t.user_message_id FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = %s AND s.user_id = %s
                """,
                (str(turn_id), int(user_id)),
            ).fetchone()
            if located is None:
                return []
            rows = conn.execute(
                """
                SELECT u.content AS user_content, a.content AS assistant_content
                FROM chat_turns previous
                JOIN chat_messages u ON u.id = previous.user_message_id
                JOIN chat_messages a ON a.id = previous.assistant_message_id
                WHERE previous.session_id = %s AND previous.user_message_id < %s
                  AND previous.status = 'completed'
                ORDER BY previous.user_message_id DESC
                LIMIT %s
                """,
                (
                    int(located["session_id"]),
                    int(located["user_message_id"]),
                    max(1, min(int(limit), 20)),
                ),
            ).fetchall()
        return [
            (str(row["user_content"]), str(row["assistant_content"]))
            for row in reversed(rows)
        ]

    def list_active_turns(self, user_id: int, session_id: int) -> list[ChatTurn]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.session_id = %s AND s.user_id = %s
                  AND t.status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                ORDER BY t.created_at
                """,
                (int(session_id), int(user_id)),
            ).fetchall()
        return [_turn_from_row(row) for row in rows]

    def list_recent_turns(
        self, user_id: int, session_id: int, limit: int = 1
    ) -> list[ChatTurn]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.session_id = %s AND s.user_id = %s
                ORDER BY t.created_at DESC, t.turn_sequence DESC
                LIMIT %s
                """,
                (int(session_id), int(user_id), max(1, int(limit))),
            ).fetchall()
        return [_turn_from_row(row) for row in rows]

    def list_session_document_cards(
        self, user_id: int, session_id: int
    ) -> dict[str, list[dict]]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT e.turn_id, e.payload_json FROM chat_turn_events e
                JOIN chat_turns t ON t.id = e.turn_id
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.session_id = %s AND s.user_id = %s AND e.event_type = 'document_card'
                ORDER BY e.turn_id, e.seq
                """,
                (int(session_id), int(user_id)),
            ).fetchall()
        cards: dict[str, list[dict]] = {}
        for row in rows:
            payload = row["payload_json"]
            if not isinstance(payload, dict):
                try:
                    payload = json.loads(payload or "{}")
                except ValueError:
                    payload = {}
            card = payload.get("card") if isinstance(payload, dict) else None
            if isinstance(card, dict) and card:
                cards.setdefault(str(row["turn_id"]), []).append(card)
        return cards

    def pending_turn_queue_state(self) -> tuple[int, float]:
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS depth, MIN(created_at) AS oldest_at,
                       EXTRACT(EPOCH FROM (now() - MIN(created_at))) AS oldest_age
                FROM chat_turns WHERE status = 'pending' AND cancel_requested = FALSE
                """
            ).fetchone()
        depth = int(row["depth"] or 0)
        age = float(row["oldest_age"]) if row["oldest_age"] is not None else 0.0
        return depth, max(0.0, age)

    def list_pending_turn_work(self, limit: int = 8) -> list[tuple[ChatTurn, int]]:
        import src.settings as settings

        serialize = bool(getattr(settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True))
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
        with self._tx() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*, s.user_id FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                JOIN users u ON u.id = s.user_id
                WHERE t.status = 'pending' AND t.cancel_requested = FALSE
                  AND u.is_active = 1
                  {head_filter}
                ORDER BY t.created_at, t.id
                LIMIT %s
                """,
                (max(1, min(int(limit), 64)),),
            ).fetchall()
        return [(_turn_from_row(row), int(row["user_id"])) for row in rows]

    # -- attachment waits and snapshots --------------------------------------

    @staticmethod
    def _transition_waiting_locked(
        conn, turn_id: str, *, to_status: str, error_message: str
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE chat_turns
            SET status = %s, error_message = %s,
                finished_at = CASE WHEN %s = 'failed' THEN clock_timestamp()
                                   ELSE finished_at END
            WHERE id = %s AND status = 'waiting_for_attachments'
            """,
            (
                to_status,
                error_message[:1000] if to_status == "failed" else "",
                to_status,
                str(turn_id),
            ),
        )
        updated = int(cursor.rowcount or 0) > 0
        if updated and to_status == "failed":
            conn.execute(
                """
                UPDATE chat_messages SET content = %s
                WHERE id = (
                    SELECT assistant_message_id FROM chat_turns WHERE id = %s
                ) AND content = ''
                """,
                (f"附件解析失败：{error_message[:400]}", str(turn_id)),
            )
        return updated

    def transition_waiting_turn(
        self,
        turn_id: str,
        *,
        to_status: str,
        error_message: str = "",
        reason_code: str = "",
    ) -> bool:
        del reason_code  # wait metrics stay in the SQLite metrics facade
        if to_status not in {"pending", "failed"}:
            raise ValueError("waiting turns can only transition to pending or failed")
        with self._tx() as conn:
            return self._transition_waiting_locked(
                conn, turn_id, to_status=to_status, error_message=error_message
            )

    def expire_waiting_turns(
        self, max_wait_seconds: int | None = None, limit: int = 50
    ) -> int:
        import src.settings as settings

        configured = getattr(settings, "WAITING_TURN_MAX_SECONDS", 600)
        max_wait = max(
            1,
            int(max_wait_seconds if max_wait_seconds is not None else configured),
        )
        message = "附件解析等待超时，本轮请求已取消；可稍后重新发送。"
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT id FROM chat_turns
                WHERE status = 'waiting_for_attachments'
                  AND created_at < now() - make_interval(secs => %s)
                ORDER BY created_at, id
                LIMIT %s
                """,
                (max_wait, max(1, min(int(limit), 500))),
            ).fetchall()
            expired = 0
            for row in rows:
                turn_id = str(row["id"])
                if not self._transition_waiting_locked(
                    conn, turn_id, to_status="failed", error_message=message
                ):
                    continue
                self._append_event_locked(
                    conn,
                    turn_id,
                    "attachment_wait_timeout",
                    {"error_code": "attachment_wait_timeout", "wait_seconds": max_wait},
                )
                expired += 1
        return expired

    @staticmethod
    def _decorate_attachment_snapshots(snapshots: list[dict]) -> None:
        from src.core.conversation import ConversationService

        ConversationService._mark_deleted_attachment_snapshots(snapshots)

    def list_turn_attachments(self, turn_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM turn_attachments WHERE turn_id = %s ORDER BY ordinal",
                (str(turn_id),),
            ).fetchall()
        snapshots = [dict(row) for row in rows]
        self._decorate_attachment_snapshots(snapshots)
        return snapshots

    def list_turn_attachments_for_session(self, session_id: int) -> dict[str, list[dict]]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT ta.* FROM turn_attachments ta
                JOIN chat_turns t ON t.id = ta.turn_id
                WHERE t.session_id = %s
                ORDER BY ta.turn_id, ta.ordinal
                """,
                (int(session_id),),
            ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(str(row["turn_id"]), []).append(dict(row))
        for snapshots in grouped.values():
            self._decorate_attachment_snapshots(snapshots)
        return grouped

    def task_metrics_summary(
        self, department_id: int | None = None, hours: int = 24
    ) -> dict:
        hours = max(1, min(int(hours), 24 * 30))
        clause = "WHERE created_at >= now() - make_interval(hours => %s)"
        params: list[Any] = [hours]
        if department_id is not None:
            clause += " AND department_id = %s"
            params.append(int(department_id))
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT status, metrics_json FROM chat_turns {clause}", params
            ).fetchall()
        total = len(rows)
        completed = sum(1 for row in rows if str(row["status"]) == "completed")
        failed = sum(1 for row in rows if str(row["status"]) == "failed")
        cancelled = sum(1 for row in rows if str(row["status"]) == "cancelled")
        metric_rows: list[dict] = []
        for row in rows:
            payload = row["metrics_json"]
            if not isinstance(payload, dict):
                try:
                    payload = json.loads(payload or "{}")
                except ValueError:
                    payload = {}
            if isinstance(payload, dict):
                metric_rows.append(payload)

        def average(key: str) -> int | None:
            values = [
                int(item[key])
                for item in metric_rows
                if isinstance(item.get(key), (int, float))
            ]
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

    # -- session/message writes ---------------------------------------------

    def create_session(
        self,
        user_id: int,
        kb_name: str,
        title: str = "新对话",
        department_id: int | None = None,
        kb_id: int | None = None,
    ) -> ChatSession:
        with self._tx() as conn:
            if department_id is None:
                owner = conn.execute(
                    "SELECT department_id FROM users WHERE id = %s", (int(user_id),)
                ).fetchone()
                department_id = owner["department_id"] if owner else None
            row = conn.execute(
                """
                INSERT INTO chat_sessions
                    (user_id, kb_name, department_id, kb_id, title, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp())
                RETURNING *
                """,
                (
                    int(user_id),
                    str(kb_name),
                    department_id,
                    kb_id,
                    str(title),
                ),
            ).fetchone()
        return _session_from_row(row)

    def list_sessions(
        self, user_id: int, kb_name: str | None = None
    ) -> list[ChatSession]:
        with self._tx() as conn:
            if kb_name:
                rows = conn.execute(
                    """
                    SELECT * FROM chat_sessions
                    WHERE user_id = %s AND kb_name = %s
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (int(user_id), str(kb_name)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM chat_sessions
                    WHERE user_id = %s
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (int(user_id),),
                ).fetchall()
        return [_session_from_row(row) for row in rows]

    def add_message(
        self, user_id: int, session_id: int, role: str, content: str
    ) -> ChatMessage:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("message role must be user, assistant, or system")
        if self.get_session(user_id, session_id) is None:
            raise PermissionError("chat session does not belong to current user")
        with self._tx() as conn:
            row = conn.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, created_at)
                VALUES (%s, %s, %s, clock_timestamp()) RETURNING *
                """,
                (int(session_id), str(role), str(content)),
            ).fetchone()
            conn.execute(
                "UPDATE chat_sessions SET updated_at = clock_timestamp()"
                " WHERE id = %s AND user_id = %s",
                (int(session_id), int(user_id)),
            )
            if role == "user":
                title = str(content).strip().replace("\n", " ")[:32] or "新对话"
                conn.execute(
                    """
                    UPDATE chat_sessions SET title = %s
                    WHERE id = %s AND user_id = %s AND title = '新对话'
                    """,
                    (title, int(session_id), int(user_id)),
                )
        return _message_from_row(row)

    def find_turn_by_client_request(
        self, session_id: int, client_request_id: str
    ) -> ChatTurn | None:
        request_id = str(client_request_id or "").strip()[:128]
        if not request_id:
            return None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM chat_turns WHERE session_id = %s AND client_request_id = %s",
                (int(session_id), request_id),
            ).fetchone()
        return _turn_from_row(row) if row is not None else None

    def count_unterminal_turns(
        self, user_id: int, *, session_id: int | None = None
    ) -> int:
        scope = "AND t.session_id = %s" if session_id is not None else ""
        params: list[Any] = [int(user_id)]
        if session_id is not None:
            params.append(int(session_id))
        with self._tx() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS total FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE s.user_id = %s {scope}
                  AND t.status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                """,
                params,
            ).fetchone()
        return int(row["total"] or 0) if row is not None else 0

    def get_turn_for_user(self, user_id: int, turn_id: str) -> ChatTurn | None:
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = %s AND s.user_id = %s
                """,
                (str(turn_id), int(user_id)),
            ).fetchone()
        return _turn_from_row(row) if row is not None else None

    def get_checkpoint_refs(self, session_id: int) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "recoverable": None,
            "committed": None,
            "generation": 0,
            "fencing_token": 0,
        }
        with self._tx() as conn:
            state = conn.execute(
                """
                SELECT recoverable_checkpoint_ref, committed_checkpoint_ref,
                       generation, fencing_token
                FROM conversation_execution_state WHERE session_id = %s
                """,
                (int(session_id),),
            ).fetchone()
        if state is None:
            return empty

        def _load(value: Any) -> dict | None:
            if value is None:
                return None
            if isinstance(value, dict):
                return value or None
            try:
                parsed = json.loads(value)
            except ValueError:
                return None
            if not isinstance(parsed, dict) or not parsed:
                return None
            return parsed

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
        with self._tx() as conn:
            located = conn.execute(
                "SELECT session_id FROM chat_turns WHERE id = %s", (str(turn_id),)
            ).fetchone()
            if located is None:
                return False
            session_id = int(located["session_id"])
            state = self._load_state(conn, session_id)
            if not self._owns(state, str(turn_id), worker_id, "", fencing_token):
                raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
            if expected_generation is not None and int(
                state.get("generation") or 0
            ) != int(expected_generation):
                raise StaleTurnOwner(
                    "checkpoint pointer was revoked by cancel or takeover"
                )
            conn.execute(
                """
                UPDATE conversation_execution_state
                SET recoverable_checkpoint_ref = %s, revision = revision + 1,
                    updated_at = clock_timestamp()
                WHERE session_id = %s
                """,
                (Jsonb(ref), session_id),
            )
            return True

    def touch_turn_worker(
        self, turn_id: str, worker_id: str, *, fencing_token: int | None = None
    ) -> bool:
        """Legacy heartbeat write; the token path renews the execution lease."""
        if fencing_token is None:
            with self._tx() as conn:
                cursor = conn.execute(
                    """
                    UPDATE chat_turns SET worker_heartbeat_at = clock_timestamp()
                    WHERE id = %s AND worker_id = %s
                      AND status IN ('streaming', 'cancelling')
                    """,
                    (str(turn_id), str(worker_id)),
                )
                del cursor
            return True
        return self.renew_turn_lease(turn_id, worker_id, "", int(fencing_token))

    def get_or_create_session(
        self, user_id: int, kb_name: str, session_id: int | None = None
    ) -> ChatSession:
        if session_id is not None:
            session = self.get_session(user_id, session_id)
            if session is not None and session.kb_name == kb_name:
                return session
        sessions = self.list_sessions(user_id, kb_name)
        if sessions:
            return sessions[0]
        return self.create_session(user_id, kb_name)

    def get_turn_by_message(self, user_id: int, message_id: int) -> ChatTurn | None:
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT t.* FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE s.user_id = %s
                  AND (t.user_message_id = %s OR t.assistant_message_id = %s)
                ORDER BY t.created_at DESC
                LIMIT 1
                """,
                (int(user_id), int(message_id), int(message_id)),
            ).fetchone()
        return _turn_from_row(row) if row is not None else None

    def is_turn_worker_stale(
        self, user_id: int, turn_id: str, stale_after_seconds: int | None = None
    ) -> bool:
        import src.settings as settings

        stale_after = max(
            30,
            int(
                stale_after_seconds
                or getattr(settings, "CHAT_TURN_HEARTBEAT_TTL_SECONDS", 90)
            ),
        )
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = %s AND s.user_id = %s
                  AND t.status IN ('streaming', 'cancelling')
                  AND t.worker_heartbeat_at IS NOT NULL
                  AND t.worker_heartbeat_at < now() - make_interval(secs => %s)
                """,
                (str(turn_id), int(user_id), stale_after),
            ).fetchone()
        return row is not None

    def is_turn_cancel_requested(self, turn_id: str) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM chat_turns WHERE id = %s",
                (str(turn_id),),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    @staticmethod
    def _reset_execution_state_locked(conn, session_id: int) -> None:
        """Clear history: advance generation and drop ownership/checkpoint refs."""
        conn.execute(
            """
            INSERT INTO conversation_execution_state (session_id, updated_at)
            VALUES (%s, clock_timestamp())
            ON CONFLICT (session_id) DO NOTHING
            """,
            (int(session_id),),
        )
        conn.execute(
            """
            UPDATE conversation_execution_state
            SET generation = generation + 1,
                active_turn_id = '', active_attempt_id = '',
                lease_owner = '', lease_expires_at = NULL,
                recoverable_checkpoint_ref = NULL, committed_checkpoint_ref = NULL,
                revision = revision + 1, updated_at = clock_timestamp()
            WHERE session_id = %s
            """,
            (int(session_id),),
        )

    def expire_queued_turns(
        self, max_queue_seconds: int | None = None, limit: int = 100
    ) -> int:
        import src.settings as settings

        configured = getattr(settings, "CHAT_QUEUE_DEADLINE_SECONDS", 600)
        max_wait = max(
            1,
            int(max_queue_seconds if max_queue_seconds is not None else configured),
        )
        message = "排队等待超时，本轮请求未执行；请稍后重试。"
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT id FROM chat_turns
                WHERE status = 'pending' AND cancel_requested = FALSE
                  AND created_at < now() - make_interval(secs => %s)
                ORDER BY created_at, id
                LIMIT %s
                """,
                (max_wait, max(1, min(int(limit), 500))),
            ).fetchall()
            expired = 0
            for row in rows:
                turn_id = str(row["id"])
                cursor = conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = 'failed', error_message = %s,
                        finished_at = clock_timestamp()
                    WHERE id = %s AND status = 'pending'
                    """,
                    (message, turn_id),
                )
                if int(cursor.rowcount or 0) == 0:
                    continue
                conn.execute(
                    """
                    UPDATE chat_messages SET content = %s
                    WHERE id = (
                        SELECT assistant_message_id FROM chat_turns WHERE id = %s
                    ) AND content = ''
                    """,
                    ("生成失败：排队等待超时", turn_id),
                )
                self._append_event_locked(
                    conn,
                    turn_id,
                    "chat_queue_timeout",
                    {"error_code": "chat_queue_timeout", "wait_seconds": max_wait},
                )
                expired += 1
        return expired

    def turn_queue_context(self, turn_id: str) -> dict[str, Any]:
        import src.settings as settings

        context: dict[str, Any] = {
            "blocked_by_turn_id": None,
            "queue_reason": "",
            "wait_deadline": None,
        }
        with self._tx() as conn:
            turn = conn.execute(
                "SELECT session_id, turn_sequence, status, created_at"
                " FROM chat_turns WHERE id = %s",
                (str(turn_id),),
            ).fetchone()
            if turn is None:
                return context
            blocker = conn.execute(
                """
                SELECT prior.id, prior.status FROM chat_turns prior
                WHERE prior.session_id = %s AND prior.turn_sequence < %s
                  AND prior.status IN ('pending', 'streaming', 'cancelling',
                                       'waiting_for_attachments')
                ORDER BY prior.turn_sequence DESC
                LIMIT 1
                """,
                (int(turn["session_id"]), int(turn["turn_sequence"] or 0)),
            ).fetchone()
        if str(turn["status"]) == "waiting_for_attachments":
            context["queue_reason"] = "waiting_for_attachments"
            created = turn["created_at"]
            if not isinstance(created, datetime):
                created = datetime.fromisoformat(str(created))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            context["wait_deadline"] = (
                created
                + timedelta(
                    seconds=int(getattr(settings, "WAITING_TURN_MAX_SECONDS", 600))
                )
            ).isoformat()
        elif blocker is not None:
            context["blocked_by_turn_id"] = str(blocker["id"])
            context["queue_reason"] = (
                "waiting_for_attachments"
                if str(blocker["status"]) == "waiting_for_attachments"
                else "queued_behind_previous_turn"
            )
        return context

    def get_session_memory_settings(
        self, user_id: int, session_id: int
    ) -> dict[str, object]:
        if self.get_session(user_id, session_id) is None:
            raise PermissionError("chat session does not belong to current user")
        with self._tx() as conn:
            extracted = conn.execute(
                """
                SELECT COUNT(DISTINCT ms.memory_id) AS count
                FROM memory_sources ms
                JOIN memory_records mr ON mr.memory_id = ms.memory_id
                WHERE ms.session_id = %s AND ms.source_valid
                  AND mr.status IN ('candidate', 'verified')
                """,
                (int(session_id),),
            ).fetchone()["count"]
            auto = conn.execute(
                "SELECT auto_memory FROM chat_sessions WHERE id = %s",
                (int(session_id),),
            ).fetchone()["auto_memory"]
        return {
            "auto_extract_enabled": bool(auto),
            "extracted_memories": int(extracted or 0),
        }

    def set_session_auto_extract(
        self, user_id: int, session_id: int, enabled: bool
    ) -> dict[str, object]:
        with self._tx() as conn:
            session = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = %s AND user_id = %s",
                (int(session_id), int(user_id)),
            ).fetchone()
            if session is None:
                raise PermissionError("chat session does not belong to current user")
            conn.execute(
                "UPDATE chat_sessions SET auto_memory = %s, updated_at = clock_timestamp()"
                " WHERE id = %s",
                (bool(enabled), int(session["id"])),
            )
            if not enabled:
                conn.execute(
                    """
                    UPDATE memory_jobs
                    SET status = 'cancelled', generation = generation + 1,
                        last_error = 'session_auto_extract_disabled',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                    WHERE session_id = %s AND job_kind = 'project_reflection'
                      AND status IN ('pending', 'retrying', 'running')
                    """,
                    (int(session["id"]),),
                )
                conn.execute(
                    """
                    INSERT INTO memory_audit_events
                        (audit_event_id, event_type, metadata_json, created_at)
                    VALUES (%s, 'session_auto_extract_disabled', %s, clock_timestamp())
                    """,
                    (str(uuid.uuid4()), Jsonb({"session_id": int(session["id"])})),
                )
        return self.get_session_memory_settings(user_id, session_id)

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
        if not redact and not (content and content.strip()):
            raise ValueError("edit_message requires new content or redact=True")
        if redact:
            content = "[已脱敏]"
        from .memory_invalidation import invalidate_message_memory

        with self._tx() as conn:
            session = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = %s AND user_id = %s",
                (int(session_id), int(user_id)),
            ).fetchone()
            if session is None:
                raise PermissionError("chat session does not belong to current user")
            message = conn.execute(
                "SELECT * FROM chat_messages WHERE id = %s AND session_id = %s",
                (int(message_id), int(session["id"])),
            ).fetchone()
            if message is None:
                raise KeyError("message not found")
            invalidate_message_memory(
                conn,
                int(session["id"]),
                int(message_id),
                reason=reason or ("message_redacted" if redact else "message_edited"),
            )
            if redact:
                from src.quality.store import redact_message_in_connection
                from .sql_adapter import PgSqlAdapterConnection

                redact_message_in_connection(PgSqlAdapterConnection(conn), int(message_id))
            conn.execute(
                """
                INSERT INTO chat_message_edits
                    (message_id, editor_user_id, action, previous_content,
                     previous_content_hash, reason, request_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                """,
                (
                    int(message_id),
                    int(user_id),
                    "redact" if redact else "edit",
                    None if redact else str(message["content"]),
                    hashlib.sha256(str(message["content"]).encode("utf-8")).hexdigest(),
                    reason or "",
                    request_id or "",
                ),
            )
            conn.execute(
                "UPDATE chat_messages SET content = %s, edited_at = clock_timestamp(),"
                " redacted = %s WHERE id = %s",
                (str(content), bool(redact), int(message_id)),
            )
            conn.execute(
                "UPDATE chat_sessions SET updated_at = clock_timestamp() WHERE id = %s",
                (int(session["id"]),),
            )
            if bool(session["auto_memory"]):
                self._enqueue_project_reflection_guarded_locked(conn, session)
            row = conn.execute(
                "SELECT * FROM chat_messages WHERE id = %s", (int(message_id),)
            ).fetchone()
        return _message_from_row(row)

    def _enqueue_project_reflection_guarded_locked(self, conn, session) -> None:
        """Rebuild queue entry after an edit; mirrors the SQLite guard."""
        import src.settings as settings

        if not getattr(settings, "MEMORY_ENABLED", True) or not getattr(
            settings, "MEMORY_EXTRACTION_ENABLED", True
        ):
            return
        department_id = session["department_id"]
        kb_id = session["kb_id"]
        if department_id in (None, "") or kb_id in (None, ""):
            return
        turn_row = conn.execute(
            """
            SELECT id, assistant_message_id FROM chat_turns
            WHERE session_id = %s AND status = 'completed'
            ORDER BY finished_at DESC, created_at DESC
            LIMIT 1
            """,
            (int(session["id"]),),
        ).fetchone()
        if turn_row is None:
            return
        from src.memory.catalog import scope_fingerprint

        from .memory_jobs import enqueue_project_reflection

        debounce = max(0, int(getattr(settings, "MEMORY_DEBOUNCE_SECONDS", 300)))
        available_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=debounce)
        ).isoformat()
        enqueue_project_reflection(
            conn,
            session_id=int(session["id"]),
            scope_fingerprint=scope_fingerprint(
                scope="project", department_id=department_id, kb_id=kb_id
            ),
            target_turn_id=str(turn_row["id"]),
            target_message_id=int(turn_row["assistant_message_id"]),
            available_at=available_at,
            force=True,
        )

    def clear_session(self, user_id: int, session_id: int) -> None:
        if self.get_session(user_id, session_id) is None:
            return
        from .memory_invalidation import invalidate_session_memory

        with self._tx() as conn:
            invalidate_session_memory(conn, int(session_id), reason="session_cleared")
            conn.execute(
                "DELETE FROM chat_messages WHERE session_id = %s", (int(session_id),)
            )
            conn.execute(
                """
                UPDATE chat_sessions SET title = '新对话', updated_at = clock_timestamp()
                WHERE id = %s AND user_id = %s
                """,
                (int(session_id), int(user_id)),
            )
            self._reset_execution_state_locked(conn, session_id)

    def delete_session(self, user_id: int, session_id: int) -> bool:
        if self.get_session(user_id, session_id) is None:
            return False
        # Reserve the durable attachment cleanup before deleting the session
        # row; a lost reference could strand private files (same order as the
        # SQLite service).
        from src.attachments.store import AttachmentStore

        AttachmentStore().enqueue_session_cleanup(
            session_id=session_id,
            user_id=user_id,
            targets={
                "attachments": True,
                "storage": True,
                "require_session_absent": True,
            },
        )
        from .memory_invalidation import invalidate_session_memory

        with self._tx() as conn:
            invalidate_session_memory(conn, int(session_id), reason="session_deleted")
            conn.execute(
                "DELETE FROM chat_sessions WHERE id = %s AND user_id = %s",
                (int(session_id), int(user_id)),
            )
        return True

    def claim_turn(
        self, turn_id: str, worker_id: str, *, stale_after_seconds: int | None = None
    ) -> TurnClaimResult:
        del stale_after_seconds  # the reaper owns heartbeat-timeout convergence
        import src.settings as settings

        serialize = bool(
            getattr(settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True)
        )
        with self._tx() as conn:
            located = conn.execute(
                "SELECT session_id FROM chat_turns WHERE id = %s", (str(turn_id),)
            ).fetchone()
            if located is None:
                return TurnClaimResult(status="noop")
            session_id = int(located["session_id"])
            state = self._load_state(conn, session_id, skip_locked=True)
            if state is None:
                # A busy session is skipped instead of blocking other claims.
                exists = conn.execute(
                    "SELECT 1 FROM conversation_execution_state WHERE session_id = %s",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO conversation_execution_state (session_id, updated_at)"
                        " VALUES (%s, clock_timestamp()) ON CONFLICT DO NOTHING",
                        (session_id,),
                    )
                    state = self._load_state(conn, session_id)
                if state is None:
                    return TurnClaimResult(status="blocked")
            current = conn.execute(
                "SELECT status, cancel_requested, turn_sequence, retry_count, assistant_message_id"
                " FROM chat_turns WHERE id = %s FOR UPDATE",
                (str(turn_id),),
            ).fetchone()
            if (
                current is not None
                and str(current["status"]) in {"streaming", "cancelling"}
                and str(state.get("active_turn_id") or "") == str(turn_id)
            ):
                # Reclaim this turn's expired claim so a takeover does not have
                # to wait for the heartbeat-based reaper (v3 §4.4).
                lease_valid = False
                if state.get("lease_expires_at") is not None:
                    lease_valid = bool(
                        conn.execute(
                            "SELECT (%s::timestamptz > clock_timestamp()) AS valid",
                            (state["lease_expires_at"],),
                        ).fetchone()["valid"]
                    )
                if not lease_valid:
                    conn.execute(
                        "UPDATE chat_turns SET status = 'pending', worker_id = '',"
                        " worker_heartbeat_at = NULL"
                        " WHERE id = %s AND status IN ('streaming', 'cancelling')",
                        (str(turn_id),),
                    )
                    current = conn.execute(
                        "SELECT status, cancel_requested, turn_sequence, retry_count,"
                        " assistant_message_id FROM chat_turns WHERE id = %s FOR UPDATE",
                        (str(turn_id),),
                    ).fetchone()
            if current is None or str(current["status"]) != "pending":
                if (
                    current is not None
                    and bool(current["cancel_requested"])
                    and str(current["status"]) == "cancelled"
                ):
                    return TurnClaimResult(status="cancelled")
                return TurnClaimResult(status="noop")
            if bool(current["cancel_requested"]):
                self._converge_cancelled_locked(
                    conn, str(turn_id), session_id, current["assistant_message_id"]
                )
                return TurnClaimResult(status="cancelled")
            if serialize:
                earlier = conn.execute(
                    """
                    SELECT id FROM chat_turns
                    WHERE session_id = %s AND id <> %s
                      AND status IN ('pending', 'streaming', 'cancelling', 'waiting_for_attachments')
                      AND turn_sequence < %s
                    ORDER BY turn_sequence LIMIT 1
                    """,
                    (session_id, str(turn_id), int(current["turn_sequence"] or 0)),
                ).fetchone()
                if earlier is not None:
                    return TurnClaimResult(
                        status="blocked", blocker_turn_id=str(earlier["id"])
                    )
                active_turn = str(state.get("active_turn_id") or "")
                if active_turn and active_turn != str(turn_id):
                    lease_valid = False
                    if state.get("lease_expires_at") is not None:
                        lease_valid = bool(
                            conn.execute(
                                "SELECT (%s::timestamptz > clock_timestamp()) AS valid",
                                (state["lease_expires_at"],),
                            ).fetchone()["valid"]
                        )
                    active = conn.execute(
                        "SELECT status FROM chat_turns WHERE id = %s", (active_turn,)
                    ).fetchone()
                    if (
                        active is not None
                        and str(active["status"]) in _NON_TERMINAL
                        and lease_valid
                    ):
                        return TurnClaimResult(
                            status="blocked", blocker_turn_id=active_turn
                        )
            if int(current["retry_count"] or 0) >= 3:
                conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = 'failed', finished_at = clock_timestamp(),
                        error_message = 'Exceeded maximum chat retries',
                        worker_id = '', worker_heartbeat_at = NULL
                    WHERE id = %s AND status = 'pending'
                    """,
                    (str(turn_id),),
                )
                self._clear_state(conn, session_id, str(turn_id))
                return TurnClaimResult(status="retry_exhausted")
            attempt_id = f"attempt-{uuid.uuid4().hex}"
            updated = conn.execute(
                """
                UPDATE conversation_execution_state
                SET active_turn_id = %s, active_attempt_id = %s, lease_owner = %s,
                    lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                    fencing_token = fencing_token + 1,
                    revision = revision + 1, updated_at = clock_timestamp()
                WHERE session_id = %s
                RETURNING fencing_token, generation
                """,
                (str(turn_id), attempt_id, str(worker_id), _LEASE_SECONDS, session_id),
            ).fetchone()
            cursor = conn.execute(
                """
                UPDATE chat_turns
                SET status = 'streaming', worker_id = %s, started_at = clock_timestamp(),
                    worker_heartbeat_at = clock_timestamp(), retry_count = retry_count + 1
                WHERE id = %s AND status = 'pending' AND cancel_requested = FALSE
                """,
                (str(worker_id), str(turn_id)),
            )
            if int(cursor.rowcount or 0) == 0:
                raise RuntimeError(f"turn {turn_id} was claimed concurrently")
            row = conn.execute(
                "SELECT * FROM chat_turns WHERE id = %s", (str(turn_id),)
            ).fetchone()
            turn = _turn_from_row(row)
            turn.fencing_token = int(updated["fencing_token"])
            turn.session_generation = int(updated["generation"])
            turn.attempt_id = attempt_id
            return TurnClaimResult(status="claimed", turn=turn)

    def renew_turn_lease(
        self, turn_id: str, worker_id: str, attempt_id: str, fencing_token: int
    ) -> bool:
        attempt_clause = " AND active_attempt_id = %s" if attempt_id else ""
        params: list[Any] = [str(turn_id), str(worker_id), int(fencing_token)]
        if attempt_id:
            params.append(str(attempt_id))
        with self._tx() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_execution_state
                SET lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                    revision = revision + 1, updated_at = clock_timestamp()
                WHERE active_turn_id = %s AND lease_owner = %s AND fencing_token = %s
                  AND lease_expires_at > clock_timestamp(){attempt_clause}
                """,
                [_LEASE_SECONDS, *params],
            )
            if int(cursor.rowcount or 0) == 0:
                return False
            conn.execute(
                "UPDATE chat_turns SET worker_heartbeat_at = clock_timestamp()"
                " WHERE id = %s AND worker_id = %s",
                (str(turn_id), str(worker_id)),
            )
            return True

    def release_turn_ownership(
        self, turn_id: str, worker_id: str, fencing_token: int | None = None
    ) -> bool:
        clauses = ["active_turn_id = %s"]
        params: list[Any] = [str(turn_id)]
        if worker_id:
            clauses.append("lease_owner = %s")
            params.append(str(worker_id))
        if fencing_token is not None:
            clauses.append("fencing_token = %s")
            params.append(int(fencing_token))
        with self._tx() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_execution_state
                SET active_turn_id = '', active_attempt_id = '', lease_owner = '',
                    lease_expires_at = NULL, revision = revision + 1,
                    updated_at = clock_timestamp()
                WHERE session_id = (SELECT session_id FROM chat_turns WHERE id = %s)
                  AND {' AND '.join(clauses)}
                """,
                [str(turn_id), *params],
            )
            return int(cursor.rowcount or 0) > 0

    def append_turn_event(
        self,
        turn_id: str,
        event_type: str,
        payload: dict,
        *,
        worker_id: str = "",
        attempt_id: str = "",
        fencing_token: int | None = None,
    ) -> ChatTurnEvent:
        if fencing_token is None:
            with self._tx() as conn:
                return self._append_event_locked(conn, str(turn_id), event_type, payload)
        attempt_clause = " AND e.active_attempt_id = %s" if attempt_id else ""
        params: list[Any] = [str(turn_id), str(worker_id), int(fencing_token)]
        if attempt_id:
            params.append(str(attempt_id))
        params.extend([str(turn_id), event_type, Jsonb(payload)])
        with self._tx() as conn:
            row = conn.execute(
                f"""
                WITH owned AS (
                    UPDATE chat_turns t SET last_event_seq = last_event_seq + 1
                    FROM conversation_execution_state e
                    WHERE t.id = %s
                      AND e.session_id = t.session_id
                      AND e.active_turn_id = t.id
                      AND e.lease_owner = %s
                      AND e.fencing_token = %s{attempt_clause}
                      AND e.lease_expires_at > clock_timestamp()
                    RETURNING t.last_event_seq
                )
                INSERT INTO chat_turn_events (turn_id, seq, event_type, payload_json, created_at)
                SELECT %s, last_event_seq, %s, %s, clock_timestamp() FROM owned
                RETURNING turn_id, seq, event_type, payload_json, created_at
                """,
                params,
            ).fetchone()
            if row is None:
                raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
            return _event_from_row(row)

    def request_turn_cancel(self, user_id: int, turn_id: str) -> ChatTurn | None:
        with self._tx() as conn:
            located = conn.execute(
                """
                SELECT t.session_id, t.status, t.assistant_message_id
                FROM chat_turns t JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = %s AND s.user_id = %s
                """,
                (str(turn_id), int(user_id)),
            ).fetchone()
            if located is None:
                return None
            session_id = int(located["session_id"])
            self._load_state(conn, session_id)
            current = conn.execute(
                "SELECT status, cancel_requested, assistant_message_id FROM chat_turns"
                " WHERE id = %s FOR UPDATE",
                (str(turn_id),),
            ).fetchone()
            if current is None:
                return None
            status = str(current["status"])
            if status in {"completed", "failed", "cancelled"}:
                return self.get_turn(turn_id)
            if status in {"streaming", "cancelling"}:
                conn.execute(
                    "UPDATE conversation_execution_state"
                    " SET generation = generation + 1, revision = revision + 1,"
                    " updated_at = clock_timestamp() WHERE session_id = %s",
                    (session_id,),
                )
                conn.execute(
                    "UPDATE chat_turns SET cancel_requested = TRUE WHERE id = %s",
                    (str(turn_id),),
                )
                # Cancel is terminal immediately.  The browser can submit its
                # next turn without waiting for the worker to unwind; late
                # worker writes are fenced by the cleared lease and advanced
                # session generation.
                self._converge_cancelled_locked(
                    conn,
                    str(turn_id),
                    session_id,
                    current["assistant_message_id"],
                )
            elif status in {"pending", "waiting_for_attachments"}:
                conn.execute(
                    "UPDATE chat_turns SET cancel_requested = TRUE WHERE id = %s",
                    (str(turn_id),),
                )
                self._converge_cancelled_locked(
                    conn,
                    str(turn_id),
                    session_id,
                    current["assistant_message_id"],
                )
        return self.get_turn(turn_id)

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
        attempt_id: str = "",
        fencing_token: int | None = None,
        expected_generation: int | None = None,
        committed_checkpoint_ref: dict | None = None,
        terminal_events_builder: Callable[[ChatTurn], Sequence[tuple[str, dict]]] | None = None,
        domain_writer: Callable[[Any, ChatTurn], None] | None = None,
    ) -> TurnCompletion:
        with self._tx() as conn:
            located = conn.execute(
                """
                SELECT t.session_id FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = %s AND s.user_id = %s
                """,
                (str(turn_id), int(user_id)),
            ).fetchone()
            if located is None:
                raise KeyError("turn not found")
            session_id = int(located["session_id"])
            state = self._load_state(conn, session_id)
            if fencing_token is not None:
                if not self._owns(state, str(turn_id), worker_id, attempt_id, fencing_token):
                    raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
                if (
                    expected_generation is not None
                    and int(state.get("generation") or 0) != int(expected_generation)
                ):
                    raise StaleTurnOwner("turn completion was revoked by cancel or takeover")
            conn.execute(
                "UPDATE chat_messages SET content = %s WHERE id ="
                " (SELECT assistant_message_id FROM chat_turns WHERE id = %s)",
                (str(answer), str(turn_id)),
            )
            updated = conn.execute(
                """
                UPDATE chat_turns
                SET status = 'completed', answer = %s, summary_json = %s, footer = %s,
                    metrics_json = %s, finished_at = clock_timestamp(),
                    worker_id = '', worker_heartbeat_at = NULL
                WHERE id = %s AND status NOT IN ('completed', 'cancelled')
                RETURNING *
                """,
                (
                    str(answer),
                    Jsonb(summary or {}),
                    str(footer or ""),
                    Jsonb(metrics or {}),
                    str(turn_id),
                ),
            ).fetchone()
            if updated is None:
                raise StaleTurnOwner(f"turn {turn_id} is already terminal")
            completed_turn = _turn_from_row(updated)
            terminal_events: list[ChatTurnEvent] = []
            if terminal_events_builder is not None:
                for event_type, payload in terminal_events_builder(completed_turn):
                    terminal_events.append(
                        self._append_event_locked(conn, str(turn_id), event_type, payload)
                    )
            if committed_checkpoint_ref is not None and fencing_token is not None:
                conn.execute(
                    """
                    UPDATE conversation_execution_state
                    SET committed_checkpoint_ref = %s, revision = revision + 1,
                        updated_at = clock_timestamp()
                    WHERE session_id = %s AND active_turn_id = %s AND fencing_token = %s
                    """,
                    (
                        Jsonb(committed_checkpoint_ref),
                        session_id,
                        str(turn_id),
                        int(fencing_token),
                    ),
                )
            if domain_writer is not None:
                domain_writer(conn, completed_turn)
            from src.quality.store import capture_completed_turn
            from .sql_adapter import PgSqlAdapterConnection

            capture_completed_turn(PgSqlAdapterConnection(conn), completed_turn, user_id)
            return TurnCompletion(turn=completed_turn, terminal_events=terminal_events)

    def fail_turn(
        self,
        user_id: int,
        turn_id: str,
        message: str,
        cancelled: bool = False,
        *,
        worker_id: str = "",
        attempt_id: str = "",
        fencing_token: int | None = None,
        terminal_events_builder: Callable[[ChatTurn], Sequence[tuple[str, dict]]] | None = None,
        domain_writer: Callable[[Any, ChatTurn], None] | None = None,
    ) -> TurnFinalization:
        status = "cancelled" if cancelled else "failed"
        with self._tx() as conn:
            located = conn.execute(
                """
                SELECT t.session_id, t.assistant_message_id FROM chat_turns t
                JOIN chat_sessions s ON s.id = t.session_id
                WHERE t.id = %s AND s.user_id = %s
                """,
                (str(turn_id), int(user_id)),
            ).fetchone()
            if located is None:
                return TurnFinalization(turn=None)
            session_id = int(located["session_id"])
            if fencing_token is not None:
                state = self._load_state(conn, session_id)
                if not self._owns(state, str(turn_id), worker_id, attempt_id, fencing_token):
                    raise StaleTurnOwner(f"turn {turn_id} is owned by a newer attempt")
            updated = conn.execute(
                """
                UPDATE chat_turns
                SET status = %s, error_message = %s, finished_at = clock_timestamp(),
                    worker_id = '', worker_heartbeat_at = NULL
                WHERE id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
                RETURNING *
                """,
                (status, str(message)[:1000], str(turn_id)),
            ).fetchone()
            if updated is None:
                return TurnFinalization(turn=None)
            placeholder = "已停止生成" if cancelled else f"生成失败：{str(message)[:500]}"
            conn.execute(
                "UPDATE chat_messages SET content = %s WHERE id = %s AND content = ''",
                (placeholder, int(located["assistant_message_id"])),
            )
            finalized_turn = _turn_from_row(updated)
            terminal_events: list[ChatTurnEvent] = []
            if terminal_events_builder is not None:
                for event_type, payload in terminal_events_builder(finalized_turn):
                    terminal_events.append(
                        self._append_event_locked(conn, str(turn_id), event_type, payload)
                    )
            if domain_writer is not None:
                domain_writer(conn, finalized_turn)
            return TurnFinalization(turn=finalized_turn, terminal_events=terminal_events)

    def requeue_stale_turns(self, stale_after_seconds: int | None = None) -> RequeueSummary:
        import src.settings as settings

        stale_after_seconds = max(30, int(stale_after_seconds or settings.CHAT_TURN_HEARTBEAT_TTL_SECONDS))
        summary = RequeueSummary()
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, cancel_requested, retry_count, assistant_message_id
                FROM chat_turns
                WHERE status IN ('streaming', 'cancelling')
                  AND (
                    cancel_requested = TRUE
                    OR (
                      worker_heartbeat_at IS NOT NULL
                      AND worker_heartbeat_at < clock_timestamp() - make_interval(secs => %s)
                    )
                  )
                """,
                (stale_after_seconds,),
            ).fetchall()
            for row in rows:
                turn_id = str(row["id"])
                session_id = int(row["session_id"])
                state = self._load_state(conn, session_id, skip_locked=True)
                if state is None:
                    continue
                current = conn.execute(
                    "SELECT status, cancel_requested, retry_count, assistant_message_id"
                    " FROM chat_turns WHERE id = %s FOR UPDATE",
                    (turn_id,),
                ).fetchone()
                if current is None or str(current["status"]) not in {"streaming", "cancelling"}:
                    continue
                if bool(current["cancel_requested"]):
                    self._converge_cancelled_locked(
                        conn, turn_id, session_id, current["assistant_message_id"]
                    )
                    summary.cancelled += 1
                    continue
                if int(current["retry_count"] or 0) >= 3:
                    conn.execute(
                        """
                        UPDATE chat_turns
                        SET status = 'failed', finished_at = clock_timestamp(),
                            error_message = '任务执行器失去心跳，超过最大重试次数',
                            worker_id = '', worker_heartbeat_at = NULL
                        WHERE id = %s AND status IN ('streaming', 'cancelling')
                        """,
                        (turn_id,),
                    )
                    self._append_event_locked(
                        conn,
                        turn_id,
                        "error",
                        {"message": "任务执行器失去心跳，超过最大重试次数"},
                    )
                    self._append_event_locked(conn, turn_id, "turn_end", {"status": "failed"})
                    self._clear_state(conn, session_id, turn_id)
                    summary.failed += 1
                    continue
                cursor = conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = 'pending', worker_id = '', worker_heartbeat_at = NULL
                    WHERE id = %s AND status IN ('streaming', 'cancelling')
                    """,
                    (turn_id,),
                )
                summary.requeued += int(cursor.rowcount or 0)
                self._clear_state(conn, session_id, turn_id)
        return summary

    def close(self) -> None:
        self._pool.close()
