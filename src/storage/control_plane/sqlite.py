"""SQLite execution store: thin adapter over the existing ConversationService.

Behavior is unchanged (v3 §5.3): this adapter exists so the same contract
tests run against both implementations.  Session-scoped service methods are
resolved through the unscoped lookups; every write delegates to the service.
"""

from __future__ import annotations

from src.core.conversation import (
    ChatTurn,
    ChatTurnEvent,
    ConversationService,
    TurnClaimResult,
    TurnCompletion,
    TurnFinalization,
)

from .base import RequeueSummary


class SqliteExecutionStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._conv = ConversationService(db_path)

    @property
    def conversations(self) -> ConversationService:
        return self._conv

    def _user_id_for_turn(self, turn_id: str) -> int:
        turn = self._conv.get_turn_unscoped(turn_id)
        if turn is None:
            raise KeyError("turn not found")
        session = self._conv.get_session_unscoped(turn.session_id)
        if session is None:
            raise KeyError("session not found")
        return int(session.user_id)

    def create_turn(
        self,
        user_id: int,
        session_id: int,
        query: str,
        *,
        client_request_id: str | None = None,
        query_mode: str = "fast",
        source_scope: str = "auto",
    ) -> ChatTurn:
        return self._conv.create_turn(
            user_id,
            session_id,
            query,
            client_request_id=client_request_id,
            query_mode=query_mode,
            source_scope=source_scope,
        )

    def claim_turn(
        self, turn_id: str, worker_id: str, *, stale_after_seconds: int | None = None
    ) -> TurnClaimResult:
        return self._conv.claim_turn_result(
            self._user_id_for_turn(turn_id),
            turn_id,
            worker_id,
            stale_after_seconds=stale_after_seconds,
        )

    def renew_turn_lease(
        self, turn_id: str, worker_id: str, attempt_id: str, fencing_token: int
    ) -> bool:
        del attempt_id  # the SQLite execution state carries owner+token only
        return self._conv.touch_turn_worker(turn_id, worker_id, fencing_token=fencing_token)

    def release_turn_ownership(
        self, turn_id: str, worker_id: str, fencing_token: int | None = None
    ) -> bool:
        return self._conv.release_turn_ownership(
            turn_id, worker_id, fencing_token=fencing_token
        )

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
        del attempt_id
        return self._conv.append_turn_event(
            turn_id,
            event_type,
            payload,
            worker_id=worker_id,
            fencing_token=fencing_token,
        )

    def request_turn_cancel(self, user_id: int, turn_id: str) -> ChatTurn | None:
        return self._conv.request_turn_cancel(user_id, turn_id)

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
        terminal_events_builder=None,
        domain_writer=None,
    ) -> TurnCompletion:
        del attempt_id
        return self._conv.complete_turn_result(
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
            terminal_events_builder=terminal_events_builder,
            domain_writer=domain_writer,
        )

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
        terminal_events_builder=None,
        domain_writer=None,
    ) -> TurnFinalization:
        del attempt_id
        return self._conv.fail_turn_result(
            user_id,
            turn_id,
            message,
            cancelled,
            worker_id=worker_id,
            fencing_token=fencing_token,
            terminal_events_builder=terminal_events_builder,
            domain_writer=domain_writer,
        )

    def requeue_stale_turns(self, stale_after_seconds: int | None = None) -> RequeueSummary:
        return RequeueSummary(
            requeued=self._conv.requeue_stale_turns(stale_after_seconds=stale_after_seconds)
        )

    def allocate_turn_sequence(self, session_id: int) -> int:
        return self._conv.allocate_turn_sequence(session_id)

    def get_turn(self, turn_id: str) -> ChatTurn | None:
        return self._conv.get_turn_unscoped(turn_id)

    def list_turn_events(self, turn_id: str, after_seq: int = 0) -> list[ChatTurnEvent]:
        return self._conv.list_turn_events(
            self._user_id_for_turn(turn_id), turn_id, after_seq
        )

    def close(self) -> None:
        return None
