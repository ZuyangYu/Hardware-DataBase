"""Persistent, tenant-scoped requirement clarification sessions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.document_authoring.harness.agent_contracts import (
    InferencePolicy,
    MissingDataPolicy,
    normalize_clarification_policy,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ClarificationAnswer(BaseModel):
    """Audit record for one clarification decision: raw text plus canonical policy."""

    question_id: str
    raw_answer: str
    normalized_answer: str | None = None
    # A caller supplied request key makes answer retries safe across HTTP,
    # tool and browser boundaries.  Historical answers have no key.
    client_request_id: str | None = None
    answered_at: datetime = Field(default_factory=_utc_now)


class GenerationBrief(BaseModel):
    purpose: str = ""
    scope: dict[str, Any] = Field(default_factory=dict)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    output_policy: dict[str, Any] = Field(default_factory=dict)
    missing_data_policy: MissingDataPolicy | None = None
    inference_policy: InferencePolicy | None = None
    clarification_answers: list[ClarificationAnswer] = Field(default_factory=list)
    # Resolver output is a snapshot of what was unresolved when the brief was
    # planned.  It is metadata for the clarifier/audit path, not writer input.
    unresolved_requirements: list[dict[str, Any]] = Field(default_factory=list)
    resolved_fields: dict[str, Any] = Field(default_factory=dict)
    allowed_derivations: list[str] = Field(default_factory=list)
    confirmed: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    updated_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_policies(cls, data: Any) -> Any:
        """Normalize legacy Chinese clarification answers on read.

        Old payloads stored the raw option text in the policy slots; unknown
        values must never reach a Writer as policy, so they normalize to None
        while remaining visible in clarification_answers when supplied.
        """
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for question_id in ("missing_data_policy", "inference_policy"):
            if question_id in normalized:
                canonical = normalize_clarification_policy(question_id, normalized[question_id])
                normalized[question_id] = canonical
        return normalized

    @field_validator("allowed_derivations")
    @classmethod
    def _unique_derivations(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_derivations must be unique")
        return value


class ClarificationMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: f"clarification-message-{uuid.uuid4().hex}")
    role: Literal["assistant", "user", "system"]
    content: str
    question_id: str | None = None
    options: list[str] = Field(default_factory=list)
    answer: str | None = None
    reason: str | None = None
    client_request_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)


class GenerationSession(BaseModel):
    session_id: str
    tenant_id: str
    user_id: str
    knowledge_base_name: str
    template_version_id: str
    status: Literal["needs_clarification", "ready_to_generate", "generating", "completed", "cancelled"]
    brief: GenerationBrief = Field(default_factory=GenerationBrief)
    messages: list[ClarificationMessage] = Field(default_factory=list)
    # These references make the clarification state part of the main
    # conversation/task graph without moving the requirement brief into
    # Conversation itself.
    conversation_id: str | None = None
    initiating_turn_id: str | None = None
    document_task_id: str | None = None
    work_order_id: str | None = None
    last_question_id: str | None = None
    clarification_revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class GenerationSessionStore:
    """SQLite store that can safely share DocumentAuthoringStore's database."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_generation_sessions (
                    session_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    knowledge_base_name TEXT NOT NULL,
                    template_version_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_document_generation_sessions_owner
                    ON document_generation_sessions(tenant_id, user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS document_generation_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES document_generation_sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_document_generation_messages_session
                    ON document_generation_messages(session_id, created_at, message_id);
                """
            )
            # The message table predates answer idempotency.  Keep the
            # payload as the source of truth while adding a queryable unique
            # request key for safe retries on upgraded installations.
            self._ensure_column(conn, "document_generation_messages", "client_request_id", "TEXT")
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS
                   idx_document_generation_messages_request
                   ON document_generation_messages(session_id, client_request_id)
                   WHERE client_request_id IS NOT NULL AND client_request_id != ''"""
            )

    @staticmethod
    def _json(value: BaseModel | dict[str, Any]) -> str:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def create_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        knowledge_base_name: str,
        template_version_id: str,
        brief: GenerationBrief | None = None,
        conversation_id: str | int | None = None,
        initiating_turn_id: str | None = None,
        document_task_id: str | None = None,
    ) -> GenerationSession:
        now = _utc_now()
        session = GenerationSession(
            session_id=f"generation-session-{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            user_id=user_id,
            knowledge_base_name=knowledge_base_name,
            template_version_id=template_version_id,
            status="needs_clarification",
            brief=brief or GenerationBrief(),
            conversation_id=self._optional(conversation_id),
            initiating_turn_id=self._optional(initiating_turn_id),
            document_task_id=self._optional(document_task_id),
            created_at=now,
            updated_at=now,
        )
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO document_generation_sessions (
                       session_id, tenant_id, user_id, knowledge_base_name,
                       template_version_id, status, created_at, updated_at, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.tenant_id,
                    session.user_id,
                    session.knowledge_base_name,
                    session.template_version_id,
                    session.status,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    self._json(session.model_dump(mode="json", exclude={"messages"})),
                ),
            )
        return session

    def get_session(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> GenerationSession:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_generation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError("generation session not found")
            if tenant_id is not None and row["tenant_id"] != tenant_id:
                raise PermissionError("generation session is outside the current tenant")
            if user_id is not None and row["user_id"] != user_id:
                raise PermissionError("generation session belongs to another user")
            messages = [
                ClarificationMessage.model_validate(json.loads(message_row["payload_json"]))
                for message_row in conn.execute(
                    """SELECT payload_json FROM document_generation_messages
                       WHERE session_id = ? ORDER BY created_at, message_id""",
                    (session_id,),
                ).fetchall()
            ]
        payload = json.loads(row["payload_json"])
        payload["messages"] = messages
        return GenerationSession.model_validate(payload)

    def append_message(
        self,
        session_id: str,
        *,
        role: Literal["assistant", "user", "system"],
        content: str,
        question_id: str | None = None,
        options: list[str] | None = None,
        answer: str | None = None,
        reason: str | None = None,
        client_request_id: str | None = None,
    ) -> ClarificationMessage:
        session = self.get_session(session_id)
        message = ClarificationMessage(
            role=role,
            content=content,
            question_id=question_id,
            options=options or [],
            answer=answer,
            reason=reason,
            client_request_id=self._optional(client_request_id),
        )
        now = _utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO document_generation_messages
                       (message_id, session_id, created_at, client_request_id, payload_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        message.message_id,
                        session_id,
                        message.created_at.isoformat(),
                        message.client_request_id,
                        self._json(message),
                    ),
                )
                last_question_id = session.last_question_id
                if role == "assistant":
                    last_question_id = message.question_id
                revised = session.model_copy(update={
                    "updated_at": now,
                    "last_question_id": last_question_id,
                })
                self._update_session_row(conn, revised)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return message

    def apply_clarification_answer(
        self,
        session_id: str,
        *,
        question_id: str,
        answer: str,
        brief: GenerationBrief,
        next_message: ClarificationMessage,
        client_request_id: str | None = None,
    ) -> tuple[GenerationSession, bool]:
        """Atomically append an answer and its next prompt.

        Returns ``(session, applied)``.  A repeated request key returns the
        already committed session with ``applied=False``; a conflicting retry
        is rejected instead of silently changing the brief.
        """
        normalized_question = str(question_id or "").strip()
        normalized_answer = str(answer or "").strip()
        request_id = self._optional(client_request_id)
        if not normalized_question or not normalized_answer:
            raise ValueError("clarification question and answer are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM document_generation_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise KeyError("generation session not found")
                current = self._session_from_connection(conn, row)
                if request_id:
                    existing_row = conn.execute(
                        """SELECT payload_json FROM document_generation_messages
                           WHERE session_id = ? AND client_request_id = ?""",
                        (session_id, request_id),
                    ).fetchone()
                    if existing_row is not None:
                        existing = ClarificationMessage.model_validate(
                            json.loads(existing_row["payload_json"])
                        )
                        if (
                            existing.role != "user"
                            or existing.question_id != normalized_question
                            or (existing.answer or existing.content).strip() != normalized_answer
                        ):
                            raise ValueError(
                                "clarification idempotency key conflicts with existing answer"
                            )
                        conn.execute("COMMIT")
                        return current, False
                if current.status != "needs_clarification":
                    raise ValueError("generation session is not accepting clarification answers")
                pending = next(
                    (
                        message for message in reversed(current.messages)
                        if message.role == "assistant" and message.question_id
                    ),
                    None,
                )
                if pending is None or pending.question_id != normalized_question:
                    raise ValueError("clarification answer does not match the current question")

                user_message = ClarificationMessage(
                    role="user",
                    content=normalized_answer,
                    question_id=normalized_question,
                    answer=normalized_answer,
                    client_request_id=request_id,
                )
                conn.execute(
                    """INSERT INTO document_generation_messages
                       (message_id, session_id, created_at, client_request_id, payload_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        user_message.message_id,
                        session_id,
                        user_message.created_at.isoformat(),
                        request_id,
                        self._json(user_message),
                    ),
                )
                conn.execute(
                    """INSERT INTO document_generation_messages
                       (message_id, session_id, created_at, client_request_id, payload_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        next_message.message_id,
                        session_id,
                        next_message.created_at.isoformat(),
                        None,
                        self._json(next_message),
                    ),
                )
                answers = list(brief.clarification_answers)
                if request_id and answers:
                    last = answers[-1]
                    if last.question_id == normalized_question:
                        answers[-1] = last.model_copy(update={"client_request_id": request_id})
                        brief = brief.model_copy(update={"clarification_answers": answers})
                revised = current.model_copy(update={
                    "brief": brief,
                    "last_question_id": next_message.question_id,
                    "clarification_revision": current.clarification_revision + 1,
                    "updated_at": _utc_now(),
                })
                self._update_session_row(conn, revised)
                conn.execute("COMMIT")
                return self._session_from_connection_by_id(session_id), True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def bind_document_task(self, session_id: str, document_task_id: str) -> GenerationSession:
        session = self.get_session(session_id)
        task_id = self._optional(document_task_id)
        if not task_id:
            raise ValueError("document task id is required")
        if session.document_task_id and session.document_task_id != task_id:
            raise ValueError("generation session is already bound to another document task")
        revised = session.model_copy(update={"document_task_id": task_id, "updated_at": _utc_now()})
        with closing(self._connect()) as conn:
            self._update_session_row(conn, revised)
        return revised

    @staticmethod
    def _optional(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _session_from_connection(self, conn: sqlite3.Connection, row: sqlite3.Row) -> GenerationSession:
        payload = json.loads(row["payload_json"])
        message_rows = conn.execute(
            """SELECT payload_json FROM document_generation_messages
               WHERE session_id = ? ORDER BY created_at, message_id""",
            (row["session_id"],),
        ).fetchall()
        payload["messages"] = [
            ClarificationMessage.model_validate(json.loads(item["payload_json"]))
            for item in message_rows
        ]
        return GenerationSession.model_validate(payload)

    def _session_from_connection_by_id(self, session_id: str) -> GenerationSession:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM document_generation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError("generation session not found")
            return self._session_from_connection(conn, row)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def update_brief(self, session_id: str, updates: dict[str, Any]) -> GenerationSession:
        session = self.get_session(session_id)
        brief_payload = session.brief.model_dump()
        brief_payload.update(updates)
        brief_payload["updated_at"] = _utc_now()
        brief = GenerationBrief.model_validate(brief_payload)
        revised = session.model_copy(update={"brief": brief, "updated_at": _utc_now()})
        with closing(self._connect()) as conn:
            self._update_session_row(conn, revised)
        return revised

    def confirm(self, session_id: str) -> GenerationSession:
        session = self.get_session(session_id)
        if session.status == "ready_to_generate" and session.brief.confirmed:
            return session
        now = _utc_now()
        brief = session.brief.model_copy(update={"confirmed": True, "updated_at": now})
        confirmed = session.model_copy(
            update={"brief": brief, "status": "ready_to_generate", "updated_at": now},
        )
        with closing(self._connect()) as conn:
            self._update_session_row(conn, confirmed)
        return confirmed

    def bind_work_order(self, session_id: str, work_order_id: str) -> GenerationSession:
        session = self.get_session(session_id)
        if session.work_order_id and session.work_order_id != work_order_id:
            raise ValueError("generation session is already bound to another work order")
        revised = session.model_copy(update={"work_order_id": work_order_id, "updated_at": _utc_now()})
        with closing(self._connect()) as conn:
            self._update_session_row(conn, revised)
        return revised

    def _update_session_row(self, conn: sqlite3.Connection, session: GenerationSession) -> None:
        cursor = conn.execute(
            """UPDATE document_generation_sessions
               SET status = ?, updated_at = ?, payload_json = ?
               WHERE session_id = ?""",
            (
                session.status,
                session.updated_at.isoformat(),
                self._json(session.model_dump(mode="json", exclude={"messages"})),
                session.session_id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError("generation session not found")
