"""Canonical HarnessRun status transition contract.

The business state machine is authoritative in the Store layer; LangGraph
checkpoints own graph state only. ``retrying`` is a transaction-internal
transient state and must never bypass the lease. Every illegal transition
yields a stable error code so callers and tests can assert exact behaviour.
"""

from __future__ import annotations

from typing import Literal

RunStatus = Literal[
    "planned", "queued", "running", "paused", "waiting_human",
    "retrying", "failed", "completed", "cancelled",
]

TRANSIENT_RETRYING = "retrying"

TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "waiting_human", "failed", "completed", "cancelled"}),
    "paused": frozenset({"queued", "cancelled"}),
    "waiting_human": frozenset({"running", "failed", "cancelled"}),
    "retrying": frozenset({"queued"}),
    "failed": frozenset({"queued", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

TERMINAL_STATES = frozenset({"completed", "cancelled"})

ERROR_INVALID_TRANSITION = "harness_run_invalid_status_transition"
ERROR_TERMINAL_STATE = "harness_run_terminal_state"
ERROR_RETRYING_NOT_TRANSIENT = "harness_run_retrying_requires_lease_release"
ERROR_UNKNOWN_STATE = "harness_run_unknown_status"


class InvalidRunTransition(ValueError):
    """Raised when a status change violates the canonical state machine."""

    def __init__(self, current: str, target: str, error_code: str):
        super().__init__(f"{error_code}: {current} -> {target}")
        self.current = current
        self.target = target
        self.error_code = error_code


def validate_transition(current: str, target: str, *, lease_released: bool = True) -> None:
    """Assert ``current -> target`` is legal.

    ``lease_released=False`` marks a move into ``retrying`` that still holds
    the lease; the state machine forbids that because ``retrying`` may only
    exist inside the transaction that releases the lease back to ``queued``.
    """

    if current not in TRANSITIONS:
        raise InvalidRunTransition(current, target, ERROR_UNKNOWN_STATE)
    if target not in TRANSITIONS:
        raise InvalidRunTransition(current, target, ERROR_UNKNOWN_STATE)
    if current in TERMINAL_STATES:
        raise InvalidRunTransition(current, target, ERROR_TERMINAL_STATE)
    if target == TRANSIENT_RETRYING and not lease_released:
        raise InvalidRunTransition(current, target, ERROR_RETRYING_NOT_TRANSIENT)
    if target not in TRANSITIONS[current]:
        raise InvalidRunTransition(current, target, ERROR_INVALID_TRANSITION)


def is_legal_transition(current: str, target: str, *, lease_released: bool = True) -> bool:
    try:
        validate_transition(current, target, lease_released=lease_released)
    except InvalidRunTransition:
        return False
    return True
