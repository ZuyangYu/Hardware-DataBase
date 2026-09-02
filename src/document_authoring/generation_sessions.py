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
    answered_at: datetime = Field(default_factory=_utc_now)


class GenerationBrief(BaseModel):
    purpose: str = ""
    scope: dict[str, Any] = Field(default_factory=dict)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    output_policy: dict[str, Any] = Field(default_factory=dict)
    missing_data_policy: MissingDataPolicy | None = None
    inference_policy: InferencePolicy | None = None
    clarification_answers: list[ClarificationAnswer] = Field(default_factory=list)
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
    work_order_id: str | None = None
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
    ) -> ClarificationMessage:
        session = self.get_session(session_id)
        message = ClarificationMessage(
            role=role,
            content=content,
            question_id=question_id,
            options=options or [],
            answer=answer,
            reason=reason,
        )
        now = _utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO document_generation_messages
                       (message_id, session_id, created_at, payload_json)
                       VALUES (?, ?, ?, ?)""",
                    (message.message_id, session_id, message.created_at.isoformat(), self._json(message)),
                )
                self._update_session_row(conn, session.model_copy(update={"updated_at": now}))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return message

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
