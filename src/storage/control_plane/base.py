"""Execution-store contract (v3 §5.1/§5.3).

The store owns the execution protocol: turn creation, sequence allocation,
claim, lease renewal, event append, cancellation transition, terminal commit
and stale-requeue convergence.  The SQLite adapter (over the existing
``ConversationService``) and the PostgreSQL implementation must pass the same
contract tests; ``domain_writer`` is how a domain commit (export snapshot,
memory outbox) stays in the same transaction as the terminal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only, real imports live in implementations
    from src.core.conversation import (
        ChatTurn,
        ChatTurnEvent,
        TurnClaimResult,
        TurnCompletion,
        TurnFinalization,
    )

TerminalEventsBuilder = Callable[["ChatTurn"], Sequence[tuple[str, dict]]]
DomainWriter = Callable[[object, "ChatTurn"], None]


@dataclass
class RequeueSummary:
    """Outcome of one stale-sweep pass (v3 §4.6)."""

    requeued: int = 0
    cancelled: int = 0
    failed: int = 0


class ExecutionStore:
    """Protocol implemented by the SQLite adapter and the PostgreSQL store.

    Ownership calls accept ``attempt_id`` so the PostgreSQL store can enforce
    the full ``owner+attempt+token`` tuple; an empty attempt id keeps legacy
    callers working (the token check still applies).
    """

    def create_turn(
        self,
        user_id: int,
        session_id: int,
        query: str,
        *,
        client_request_id: str | None = None,
        query_mode: str = "fast",
        source_scope: str = "auto",
        disabled_skill_refs: list[str] | None = None,
        knowledge_scope=None,
    ) -> "ChatTurn":
        raise NotImplementedError

    def claim_turn(
        self, turn_id: str, worker_id: str, *, stale_after_seconds: int | None = None
    ) -> "TurnClaimResult":
        raise NotImplementedError

    def renew_turn_lease(
        self, turn_id: str, worker_id: str, attempt_id: str, fencing_token: int
    ) -> bool:
        raise NotImplementedError

    def release_turn_ownership(
        self, turn_id: str, worker_id: str, fencing_token: int | None = None
    ) -> bool:
        raise NotImplementedError

    def append_turn_event(
        self,
        turn_id: str,
        event_type: str,
        payload: dict,
        *,
        worker_id: str = "",
        attempt_id: str = "",
        fencing_token: int | None = None,
    ) -> "ChatTurnEvent":
        raise NotImplementedError

    def request_turn_cancel(self, user_id: int, turn_id: str) -> "ChatTurn | None":
        raise NotImplementedError

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
        terminal_events_builder: TerminalEventsBuilder | None = None,
        domain_writer: DomainWriter | None = None,
    ) -> "TurnCompletion":
        raise NotImplementedError

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
        terminal_events_builder: TerminalEventsBuilder | None = None,
        domain_writer: DomainWriter | None = None,
    ) -> "TurnFinalization":
        raise NotImplementedError

    def requeue_stale_turns(self, stale_after_seconds: int | None = None) -> RequeueSummary:
        raise NotImplementedError

    def allocate_turn_sequence(self, session_id: int) -> int:
        raise NotImplementedError

    def get_turn(self, turn_id: str) -> "ChatTurn | None":
        raise NotImplementedError

    def list_turn_events(self, turn_id: str, after_seq: int = 0) -> "list[ChatTurnEvent]":
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
