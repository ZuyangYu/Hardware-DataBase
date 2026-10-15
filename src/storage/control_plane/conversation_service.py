"""ConversationService-compatible facade over the PostgreSQL execution store.

PR-3 preparation (v3 §5.3): API and worker keep calling the same public
methods; when ``CONTROL_PLANE_BACKEND=postgres`` this class answers them from
PostgreSQL.  The SQLite default remains
``src.core.conversation.ConversationService``.
"""

from __future__ import annotations

from typing import Any

from src.core.conversation import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnEvent,
    TurnClaimResult,
    TurnCompletion,
    TurnFinalization,
)

from .postgres import PostgresExecutionStore


class PostgresConversationService:
    def __init__(self, dsn: str, *, pool_size: int = 8) -> None:
        self._store = PostgresExecutionStore(dsn, max_size=max(1, int(pool_size)))

    @property
    def store(self) -> PostgresExecutionStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    @property
    def closed(self) -> bool:
        return self._store._pool.closed

    def list_waiting_turn_ids(self, attachment_ids: list[str] | None = None, *, limit: int = 100) -> list[str]:
        return self._store.list_waiting_turn_ids(attachment_ids, limit=limit)

    # -- sessions -----------------------------------------------------------

    def create_session(
        self,
        user_id: int,
        kb_name: str,
        title: str = "新对话",
        department_id: int | None = None,
        kb_id: int | None = None,
        *,
        knowledge_selection=None,
        tenant_key: str = "default",
    ) -> ChatSession:
        return self._store.create_session(user_id, kb_name, title, department_id, kb_id, knowledge_selection=knowledge_selection, tenant_key=tenant_key)

    def get_session(self, user_id: int, session_id: int) -> ChatSession | None:
        return self._store.get_session(user_id, session_id)

    def get_session_unscoped(self, session_id: int) -> ChatSession | None:
        return self._store.get_session_unscoped(session_id)

    def list_sessions(
        self, user_id: int, kb_name: str | None = None
    ) -> list[ChatSession]:
        return self._store.list_sessions(user_id, kb_name)

    def get_or_create_session(
        self, user_id: int, kb_name: str, session_id: int | None = None
    ) -> ChatSession:
        return self._store.get_or_create_session(user_id, kb_name, session_id)

    # -- messages -----------------------------------------------------------

    def add_message(
        self, user_id: int, session_id: int, role: str, content: str
    ) -> ChatMessage:
        return self._store.add_message(user_id, session_id, role, content)

    def list_messages(self, user_id: int, session_id: int) -> list[ChatMessage]:
        return self._store.list_messages(user_id, session_id)

    def history_before_turn(
        self, user_id: int, turn_id: str, limit: int = 5
    ) -> list[tuple[str, str]]:
        return self._store.history_before_turn(user_id, turn_id, limit)

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
        return self._store.edit_message(
            user_id,
            session_id,
            message_id,
            content=content,
            redact=redact,
            reason=reason,
            request_id=request_id,
        )

    # -- turns --------------------------------------------------------------

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
        *,
        knowledge_scope=None,
    ) -> ChatTurn:
        return self._store.create_turn(
            user_id,
            session_id,
            query,
            client_request_id,
            query_mode,
            trace_context,
            wiki_assist,
            document_context,
            attachment_ids,
            source_scope,
            knowledge_scope=knowledge_scope,
        )

    def claim_turn(
        self,
        user_id: int,
        turn_id: str,
        worker_id: str = "",
        stale_after_seconds: int | None = None,
    ) -> ChatTurn | None:
        return self.claim_turn_result(
            user_id, turn_id, worker_id, stale_after_seconds
        ).turn

    def claim_turn_result(
        self,
        user_id: int,
        turn_id: str,
        worker_id: str = "",
        stale_after_seconds: int | None = None,
    ) -> TurnClaimResult:
        del user_id  # turn ownership is resolved from the session row
        return self._store.claim_turn(
            turn_id, worker_id, stale_after_seconds=stale_after_seconds
        )

    def get_turn(self, user_id: int, turn_id: str) -> ChatTurn | None:
        return self._store.get_turn_for_user(user_id, turn_id)

    def get_turn_unscoped(self, turn_id: str) -> ChatTurn | None:
        return self._store.get_turn(turn_id)

    def get_turn_by_message(self, user_id: int, message_id: int) -> ChatTurn | None:
        return self._store.get_turn_by_message(user_id, message_id)

    def list_active_turns(self, user_id: int, session_id: int) -> list[ChatTurn]:
        return self._store.list_active_turns(user_id, session_id)

    def list_recent_turns(
        self, user_id: int, session_id: int, limit: int = 1
    ) -> list[ChatTurn]:
        return self._store.list_recent_turns(user_id, session_id, limit)

    def list_turn_events(
        self, user_id: int, turn_id: str, after_seq: int = 0
    ) -> list[ChatTurnEvent]:
        del user_id
        return self._store.list_turn_events(turn_id, after_seq)

    def append_turn_event(
        self,
        turn_id: str,
        event_type: str,
        payload: dict,
        *,
        worker_id: str = "",
        fencing_token: int | None = None,
    ) -> ChatTurnEvent:
        return self._store.append_turn_event(
            turn_id,
            event_type,
            payload,
            worker_id=worker_id,
            attempt_id="",
            fencing_token=fencing_token,
        )

    def touch_turn_worker(
        self, turn_id: str, worker_id: str, *, fencing_token: int | None = None
    ) -> bool:
        return self._store.touch_turn_worker(
            turn_id, worker_id, fencing_token=fencing_token
        )

    def release_turn_ownership(
        self, turn_id: str, worker_id: str, *, fencing_token: int | None = None
    ) -> bool:
        return self._store.release_turn_ownership(turn_id, worker_id, fencing_token)

    def request_turn_cancel(self, user_id: int, turn_id: str) -> ChatTurn | None:
        return self._store.request_turn_cancel(user_id, turn_id)

    def is_turn_cancel_requested(self, turn_id: str) -> bool:
        return self._store.is_turn_cancel_requested(turn_id)

    def is_turn_worker_stale(
        self, user_id: int, turn_id: str, stale_after_seconds: int | None = None
    ) -> bool:
        return self._store.is_turn_worker_stale(
            user_id, turn_id, stale_after_seconds
        )

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
        terminal_events_builder=None,
        domain_writer=None,
    ) -> TurnCompletion:
        return self._store.complete_turn(
            user_id,
            turn_id,
            answer,
            summary,
            footer,
            metrics,
            worker_id=worker_id,
            attempt_id="",
            fencing_token=fencing_token,
            expected_generation=expected_generation,
            committed_checkpoint_ref=committed_checkpoint_ref,
            terminal_events_builder=terminal_events_builder,
            domain_writer=domain_writer
            or self._default_domain_writer(user_id),
        )

    def _default_domain_writer(self, user_id: int):
        """Terminal domain writes (export snapshot/jobs + Memory outbox).

        Mirrors the SQLite completion transaction (v3 §4.7) so enabling the
        PostgreSQL control plane cannot silently drop export tasks or the
        Memory outbox.
        """

        def _writer(conn, completed_turn) -> None:
            from .terminal_domain import write_terminal_domains

            write_terminal_domains(
                conn, completed_turn, owner_user_id=user_id
            )

        return _writer

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
        terminal_events_builder=None,
        domain_writer=None,
    ) -> TurnFinalization:
        return self._store.fail_turn(
            user_id,
            turn_id,
            message,
            cancelled,
            worker_id=worker_id,
            attempt_id="",
            fencing_token=fencing_token,
            terminal_events_builder=terminal_events_builder,
            domain_writer=domain_writer,
        )

    # -- queue and governance ----------------------------------------------

    def allocate_turn_sequence(self, session_id: int) -> int:
        return self._store.allocate_turn_sequence(session_id)

    def find_turn_by_client_request(
        self, session_id: int, client_request_id: str
    ) -> ChatTurn | None:
        return self._store.find_turn_by_client_request(session_id, client_request_id)

    def count_unterminal_turns(
        self, user_id: int, *, session_id: int | None = None
    ) -> int:
        return self._store.count_unterminal_turns(user_id, session_id=session_id)

    def requeue_stale_turns(self, stale_after_seconds: int | None = None) -> int:
        return self._store.requeue_stale_turns(
            stale_after_seconds=stale_after_seconds
        ).requeued

    def expire_queued_turns(
        self, max_queue_seconds: int | None = None, limit: int = 100
    ) -> int:
        return self._store.expire_queued_turns(max_queue_seconds, limit)

    def expire_waiting_turns(
        self, max_wait_seconds: int | None = None, limit: int = 50
    ) -> int:
        return self._store.expire_waiting_turns(max_wait_seconds, limit)

    def transition_waiting_turn(
        self,
        turn_id: str,
        *,
        to_status: str,
        error_message: str = "",
        reason_code: str = "",
    ) -> bool:
        return self._store.transition_waiting_turn(
            turn_id,
            to_status=to_status,
            error_message=error_message,
            reason_code=reason_code,
        )

    def pending_turn_queue_state(self) -> tuple[int, float]:
        return self._store.pending_turn_queue_state()

    def list_pending_turn_work(self, limit: int = 8) -> list[tuple[ChatTurn, int]]:
        return self._store.list_pending_turn_work(limit)

    def task_metrics_summary(
        self, department_id: int | None = None, hours: int = 24
    ) -> dict:
        return self._store.task_metrics_summary(department_id, hours)

    def turn_queue_context(self, turn_id: str) -> dict[str, Any]:
        return self._store.turn_queue_context(turn_id)

    def get_session_memory_settings(
        self, user_id: int, session_id: int
    ) -> dict[str, object]:
        return self._store.get_session_memory_settings(user_id, session_id)

    def set_session_auto_extract(
        self, user_id: int, session_id: int, enabled: bool
    ) -> dict[str, object]:
        return self._store.set_session_auto_extract(user_id, session_id, enabled)

    def list_turn_attachments(self, turn_id: str) -> list[dict]:
        return self._store.list_turn_attachments(turn_id)

    def list_turn_attachments_for_session(
        self, session_id: int
    ) -> dict[str, list[dict]]:
        return self._store.list_turn_attachments_for_session(session_id)

    def list_session_document_cards(
        self, user_id: int, session_id: int
    ) -> dict[str, list[dict]]:
        return self._store.list_session_document_cards(user_id, session_id)

    def get_checkpoint_refs(self, session_id: int) -> dict[str, Any]:
        return self._store.get_checkpoint_refs(session_id)

    def update_recoverable_checkpoint_ref(
        self,
        turn_id: str,
        worker_id: str,
        fencing_token: int,
        ref: dict,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        return self._store.update_recoverable_checkpoint_ref(
            turn_id,
            worker_id,
            fencing_token,
            ref,
            expected_generation=expected_generation,
        )

    def clear_session(self, user_id: int, session_id: int) -> None:
        self._store.clear_session(user_id, session_id)

    def delete_session(self, user_id: int, session_id: int) -> bool:
        return self._store.delete_session(user_id, session_id)
