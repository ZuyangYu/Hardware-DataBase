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


def _compatibility_service(db_path: str):
    """Load the Phase 5 boundary lazily to keep legacy model imports light."""
    from src.document_authoring.compatibility import DocumentAuthoringCompatibilityService

    return DocumentAuthoringCompatibilityService(db_path=db_path)


def _guard_legacy_generation_write(
    db_path: str,
    session: "GenerationSession | None",
    *,
    operation: str,
) -> None:
    if session is None or session.contract_version != "legacy_brief_v1":
        return
    compatibility = _compatibility_service(db_path)
    if compatibility.closure_enabled:
        from src.document_authoring.compatibility import CompatibilityClosureError

        raise CompatibilityClosureError(
            f"direct GenerationBrief {operation} is closed; use an accepted OutputSpec/DocumentPlan"
        )
    compatibility.record_legacy_write(
        operation=operation,
        tenant_id=session.tenant_id,
        entity_type="generation_session",
        entity_id=session.session_id,
    )


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
    template_version_id: str | None = None
    contract_version: Literal["legacy_brief_v1", "output_spec_v1"] = "legacy_brief_v1"
    status: Literal[
        "needs_clarification", "ready_to_generate", "generating", "completed", "cancelled",
        "awaiting_plan", "awaiting_plan_confirmation", "planned", "blocked",
    ]
    brief: GenerationBrief = Field(default_factory=GenerationBrief)
    messages: list[ClarificationMessage] = Field(default_factory=list)
    # These references make the clarification state part of the main
    # conversation/task graph without moving the requirement brief into
    # Conversation itself.
    conversation_id: str | None = None
    initiating_turn_id: str | None = None
    document_task_id: str | None = None
    work_order_id: str | None = None
    output_spec_id: str | None = None
    output_spec_version: int | None = Field(default=None, ge=1)
    output_spec_draft: dict[str, Any] | None = None
    document_plan_id: str | None = None
    document_plan_version: int | None = Field(default=None, ge=1)
    last_question_id: str | None = None
    clarification_revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def validate_contract(self):
        if self.contract_version == "legacy_brief_v1" and not str(self.template_version_id or "").strip():
            raise ValueError("legacy generation sessions require a template")
        if self.contract_version == "legacy_brief_v1" and self.status in {
            "awaiting_plan", "awaiting_plan_confirmation", "planned", "blocked",
        }:
            raise ValueError("planning lifecycle statuses require output_spec_v1")
        for prefix in ("output_spec", "document_plan"):
            identifier = getattr(self, f"{prefix}_id")
            version = getattr(self, f"{prefix}_version")
            if (identifier is None) != (version is None):
                raise ValueError(f"{prefix} id and version must be provided together")
        if self.output_spec_draft is not None and self.output_spec_id is None:
            raise ValueError("output spec draft requires an output spec reference")
        return self


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
                    template_version_id TEXT,
                    contract_version TEXT NOT NULL DEFAULT 'legacy_brief_v1',
                    output_spec_id TEXT,
                    output_spec_version INTEGER,
                    output_spec_draft_json TEXT,
                    document_plan_id TEXT,
                    document_plan_version INTEGER,
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
            # Upgrade installations created before output-spec sessions used a
            # NOT NULL template reference.  Rebuild that table so a v2 draft
            # can safely omit a template while preserving every legacy
            # payload byte-for-byte.
            self._migrate_template_nullable(conn)
            for column, ddl in (
                ("contract_version", "TEXT NOT NULL DEFAULT 'legacy_brief_v1'"),
                ("output_spec_id", "TEXT"),
                ("output_spec_version", "INTEGER"),
                ("output_spec_draft_json", "TEXT"),
                ("document_plan_id", "TEXT"),
                ("document_plan_version", "INTEGER"),
            ):
                self._ensure_column(conn, "document_generation_sessions", column, ddl)
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
        template_version_id: str | None,
        brief: GenerationBrief | None = None,
        conversation_id: str | int | None = None,
        initiating_turn_id: str | None = None,
        document_task_id: str | None = None,
        contract_version: Literal["legacy_brief_v1", "output_spec_v1"] = "legacy_brief_v1",
        status: Literal[
            "needs_clarification", "ready_to_generate", "generating", "completed", "cancelled",
            "awaiting_plan", "awaiting_plan_confirmation", "planned", "blocked",
        ] | None = None,
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        output_spec_draft: dict[str, Any] | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
    ) -> GenerationSession:
        compatibility = _compatibility_service(self.db_path)
        if contract_version == "legacy_brief_v1" and compatibility.closure_enabled:
            from src.document_authoring.compatibility import CompatibilityClosureError

            raise CompatibilityClosureError(
                "direct GenerationBrief sessions are closed; use an OutputSpec/DocumentPlan session"
            )
        now = _utc_now()
        initial_status = status or (
            "awaiting_plan" if contract_version == "output_spec_v1" else "needs_clarification"
        )
        session = GenerationSession(
            session_id=f"generation-session-{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            user_id=user_id,
            knowledge_base_name=knowledge_base_name,
            template_version_id=template_version_id,
            contract_version=contract_version,
            status=initial_status,
            brief=brief or GenerationBrief(),
            conversation_id=self._optional(conversation_id),
            initiating_turn_id=self._optional(initiating_turn_id),
            document_task_id=self._optional(document_task_id),
            output_spec_id=self._optional(output_spec_id),
            output_spec_version=output_spec_version,
            output_spec_draft=output_spec_draft,
            document_plan_id=self._optional(document_plan_id),
            document_plan_version=document_plan_version,
            created_at=now,
            updated_at=now,
        )
        with closing(self._connect()) as conn:
            conn.execute(
                    """INSERT INTO document_generation_sessions (
                       session_id, tenant_id, user_id, knowledge_base_name,
                       template_version_id, contract_version, output_spec_id,
                       output_spec_version, output_spec_draft_json,
                       document_plan_id, document_plan_version,
                       status, created_at, updated_at, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id, session.tenant_id, session.user_id,
                    session.knowledge_base_name, session.template_version_id,
                    session.contract_version, session.output_spec_id,
                    session.output_spec_version,
                    self._json(session.output_spec_draft) if session.output_spec_draft is not None else None,
                    session.document_plan_id,
                    session.document_plan_version, session.status,
                    session.created_at.isoformat(), session.updated_at.isoformat(),
                    self._json(session.model_dump(mode="json", exclude={"messages"})),
                ),
            )
        if contract_version == "legacy_brief_v1":
            compatibility.record_legacy_direct_execution(
                operation="create_generation_session",
                tenant_id=session.tenant_id,
                entity_type="generation_session",
                entity_id=session.session_id,
            )
            compatibility.record_legacy_write(
                operation="create_generation_session",
                tenant_id=session.tenant_id,
                entity_type="generation_session",
                entity_id=session.session_id,
            )
        else:
            compatibility.record_new_write(
                operation="create_output_spec_session",
                tenant_id=session.tenant_id,
                entity_type="generation_session",
                entity_id=session.session_id,
                route="conversation",
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
        _guard_legacy_generation_write(
            self.db_path, session, operation="append_generation_session_message",
        )
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
        current_for_guard = self.get_session(session_id)
        _guard_legacy_generation_write(
            self.db_path, current_for_guard, operation="apply_clarification_answer",
        )
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
    def _migrate_template_nullable(conn: sqlite3.Connection) -> None:
        """Rebuild the legacy session table when its template is NOT NULL.

        SQLite cannot alter a column's nullability in place.  The migration is
        intentionally payload-preserving: only the indexed columns are copied
        and the original JSON string is never parsed or rewritten.
        """
        columns = conn.execute(
            "PRAGMA table_info(document_generation_sessions)"
        ).fetchall()
        template = next((row for row in columns if row[1] == "template_version_id"), None)
        if template is None or int(template[3]) == 0:
            return

        # Remove indexes before renaming; SQLite otherwise carries their names
        # to the temporary table and can collide when the replacement is
        # renamed back to the canonical table name.
        conn.execute("DROP INDEX IF EXISTS idx_document_generation_sessions_owner")
        conn.execute("DROP INDEX IF EXISTS idx_document_generation_messages_session")
        conn.execute("DROP INDEX IF EXISTS idx_document_generation_messages_request")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("ALTER TABLE document_generation_messages RENAME TO document_generation_messages_legacy")
            conn.execute("ALTER TABLE document_generation_sessions RENAME TO document_generation_sessions_legacy")
            conn.execute(
                """CREATE TABLE document_generation_sessions (
                    session_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    knowledge_base_name TEXT NOT NULL,
                    template_version_id TEXT,
                    contract_version TEXT NOT NULL DEFAULT 'legacy_brief_v1',
                    output_spec_id TEXT,
                    output_spec_version INTEGER,
                    document_plan_id TEXT,
                    document_plan_version INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT INTO document_generation_sessions
                   (session_id, tenant_id, user_id, knowledge_base_name,
                    template_version_id, status, created_at, updated_at, payload_json)
                   SELECT session_id, tenant_id, user_id, knowledge_base_name,
                          template_version_id, status, created_at, updated_at, payload_json
                     FROM document_generation_sessions_legacy"""
            )
            conn.execute("DROP TABLE document_generation_sessions_legacy")

            # Rebuild the child table as well because the legacy FK target was
            # rewritten by ALTER TABLE RENAME above.
            message_columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(document_generation_messages_legacy)"
            ).fetchall()}
            client_expr = "client_request_id" if "client_request_id" in message_columns else "NULL"
            conn.execute(
                """CREATE TABLE document_generation_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    client_request_id TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES document_generation_sessions(session_id)
                )"""
            )
            conn.execute(
                f"""INSERT INTO document_generation_messages
                    (message_id, session_id, created_at, client_request_id, payload_json)
                    SELECT message_id, session_id, created_at, {client_expr}, payload_json
                      FROM document_generation_messages_legacy"""
            )
            conn.execute("DROP TABLE document_generation_messages_legacy")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_document_generation_sessions_owner
               ON document_generation_sessions(tenant_id, user_id, updated_at DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_document_generation_messages_session
               ON document_generation_messages(session_id, created_at, message_id)"""
        )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def update_brief(self, session_id: str, updates: dict[str, Any]) -> GenerationSession:
        session = self.get_session(session_id)
        _guard_legacy_generation_write(
            self.db_path, session, operation="update_generation_brief",
        )
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
        if session.contract_version == "output_spec_v1":
            raise ValueError("output_spec_v1 sessions require an accepted document plan")
        _guard_legacy_generation_write(
            self.db_path, session, operation="confirm_generation_session",
        )
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

    def bind_plan(
        self,
        session_id: str,
        *,
        output_spec_id: str,
        output_spec_version: int,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        status: str | None = None,
    ) -> GenerationSession:
        """Bind immutable planning pointers to a session using a CAS-like rule."""
        spec_id = self._optional(output_spec_id)
        plan_id = self._optional(document_plan_id)
        if not spec_id or output_spec_version < 1:
            raise ValueError("output spec id and positive version are required")
        if (plan_id is None) != (document_plan_version is None):
            raise ValueError("document plan id and version must be provided together")
        session = self.get_session(session_id)
        existing = (session.output_spec_id, session.output_spec_version,
                    session.document_plan_id, session.document_plan_version)
        requested = (spec_id, output_spec_version, plan_id, document_plan_version)
        if any(
            current is not None and current != incoming
            for current, incoming in zip(existing, requested)
        ):
            raise ValueError("generation session is already bound to another plan")
        if all(current is not None for current in existing):
            # Replaying the exact binding is idempotent; do not advance the
            # revision or emit another persistence write.
            if existing == requested:
                return session
            else:
                raise ValueError("generation session is already bound to another plan")
        updates: dict[str, Any] = {
            "output_spec_id": spec_id,
            "output_spec_version": output_spec_version,
            "document_plan_id": plan_id,
            "document_plan_version": document_plan_version,
            "updated_at": _utc_now(),
        }
        if status is not None:
            normalized = str(status).strip()
            if normalized not in {
                "needs_clarification", "ready_to_generate", "generating", "completed", "cancelled",
                "awaiting_plan", "awaiting_plan_confirmation", "planned", "blocked",
            }:
                raise ValueError("unsupported generation session status")
            updates["status"] = normalized
        revised = session.model_copy(update=updates)
        with closing(self._connect()) as conn:
            self._update_session_row(conn, revised)
        return revised

    def update_output_spec_draft(
        self,
        session_id: str,
        draft: dict[str, Any],
        *,
        expected_version: int,
        status: str = "awaiting_plan",
    ) -> GenerationSession:
        """Persist the next draft revision only when its expected version matches."""
        session = self.get_session(session_id)
        if session.contract_version != "output_spec_v1":
            raise ValueError("output spec drafts require output_spec_v1")
        current_version = int(session.output_spec_version or 0)
        if current_version != int(expected_version):
            raise ValueError(
                f"expected output spec version {expected_version}, current version is {current_version}"
            )
        draft_version = int(draft.get("version") or 0)
        if draft_version <= current_version:
            raise ValueError("output spec draft version must advance")
        revised = session.model_copy(update={
            "output_spec_id": self._optional(draft.get("output_spec_id")) or session.output_spec_id,
            "output_spec_version": draft_version,
            "output_spec_draft": dict(draft),
            # A changed user decision invalidates any previously proposed
            # plan.  The immutable plan row remains available for stale
            # comparison, while the session can bind the next proposal.
            "document_plan_id": None,
            "document_plan_version": None,
            "status": status,
            "updated_at": _utc_now(),
        })
        with closing(self._connect()) as conn:
            self._update_session_row(conn, revised)
        return revised

    def clear_plan(self, session_id: str) -> GenerationSession:
        """Drop only the session's current plan pointer before reproposal."""

        session = self.get_session(session_id)
        if session.document_plan_id is None and session.document_plan_version is None:
            return session
        revised = session.model_copy(update={
            "document_plan_id": None,
            "document_plan_version": None,
            "updated_at": _utc_now(),
        })
        with closing(self._connect()) as conn:
            self._update_session_row(conn, revised)
        return revised

    def _update_session_row(self, conn: sqlite3.Connection, session: GenerationSession) -> None:
        cursor = conn.execute(
            """UPDATE document_generation_sessions
               SET template_version_id = ?, contract_version = ?,
                   output_spec_id = ?, output_spec_version = ?,
                   output_spec_draft_json = ?,
                   document_plan_id = ?, document_plan_version = ?,
                   status = ?, updated_at = ?, payload_json = ?
               WHERE session_id = ?""",
            (
                session.template_version_id, session.contract_version,
                session.output_spec_id, session.output_spec_version,
                self._json(session.output_spec_draft) if session.output_spec_draft is not None else None,
                session.document_plan_id, session.document_plan_version,
                session.status,
                session.updated_at.isoformat(),
                self._json(session.model_dump(mode="json", exclude={"messages"})),
                session.session_id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError("generation session not found")
