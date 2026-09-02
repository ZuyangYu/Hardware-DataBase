"""HarnessCheckpoint -> HarnessRun migration contract (Phase 0 functions only).

Deliverables are pure functions: no production write path switches here
(Task 5b) and no table retirement (Phase D / Task 10). Each legacy checkpoint
payload maps to exactly one of:

- ``converted``: business fields preserved AND the produced versioned graph
  state passed schema/size validation AND (when a backend writer is supplied)
  a real write/read-back round-trip on the selected checkpointer backend.
- ``legacy_terminal``: the run is business-terminal for migration purposes; a
  new run with lineage must be created. No same-node resume is promised.

``migration_state=legacy_terminal`` does not change the business terminal
semantics of the run (completed/cancelled/failed stay what they are).
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from src.document_authoring.harness.agent_contracts import serialize_graph_state

GRAPH_STATE_VERSION = 1

LEGACY_REASON_INVALID_PAYLOAD = "legacy_payload_invalid"
LEGACY_REASON_MISSING_IDENTITY = "legacy_payload_missing_identity"
LEGACY_REASON_ACTIVE_LEASE_UNVERIFIABLE = "legacy_active_lease_unverifiable"
LEGACY_REASON_ACTIVE_LEASE_EXPIRED = "legacy_active_lease_expired"
LEGACY_REASON_STATE_TOO_LARGE = "legacy_graph_state_size_exceeded"
LEGACY_REASON_ROUND_TRIP_FAILED = "legacy_checkpoint_round_trip_failed"

_PRESERVED_STATUSES = {"paused", "waiting_human", "failed", "completed", "cancelled"}
_REQUIRED_IDENTITY = ("harness_run_id", "work_order_id", "input_fingerprint", "source_set_snapshot_id")
_GRAPH_STATE_KEYS = (
    "current_node", "step_count", "retrieval_round_count", "completed_units",
    "total_units", "unit_statuses", "unit_attempts", "dispatch_cursor",
    "evidence_matrix_hash", "draft_ids", "pending_human_event",
)

BackendRoundTrip = Callable[[str, dict[str, Any]], None]


class CheckpointMigrationResult(BaseModel):
    outcome: Literal["converted", "legacy_terminal"]
    migration_reason: str | None = None
    harness_run_fields: dict[str, Any] = Field(default_factory=dict)
    graph_state: dict[str, Any] | None = None


def _business_fields(payload: dict[str, Any], status: str) -> dict[str, Any]:
    unit_statuses = dict(payload.get("unit_statuses") or {})
    unit_attempts = dict(payload.get("unit_attempts") or {})
    for unit_id in unit_statuses:
        unit_attempts.setdefault(unit_id, 1)
    return {
        "tenant_id": payload.get("tenant_id") or "default",
        "work_order_id": payload["work_order_id"],
        "input_fingerprint": payload["input_fingerprint"],
        "input_fingerprint_version": 1,
        "source_set_snapshot_id": payload["source_set_snapshot_id"],
        "status": status,
        "current_node": payload.get("current_node") or "initialize",
        "step_count": int(payload.get("step_count") or 0),
        "retrieval_round_count": int(payload.get("retrieval_round_count") or 0),
        "completed_units": int(payload.get("completed_units") or 0),
        "total_units": int(payload.get("total_units") or 0),
        "retry_count": int(payload.get("retry_count") or 0),
        "max_retries": int(payload.get("max_retries") or 0),
        "unit_statuses": unit_statuses,
        "unit_attempts": unit_attempts,
        "dispatch_cursor": int(payload.get("dispatch_cursor") or 0),
        "evidence_matrix_hash": payload.get("evidence_matrix_hash"),
        "draft_ids": list(payload.get("draft_ids") or []),
        "pending_human_event": payload.get("pending_human_event"),
        "fencing_token": int(payload.get("fencing_token") or 0),
        "migration_state": "converted",
    }


def _graph_state(payload: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {"graph_state_version": GRAPH_STATE_VERSION}
    for key in _GRAPH_STATE_KEYS:
        if key in payload and payload[key] is not None:
            state[key] = payload[key]
    state.setdefault("unit_attempts", {})
    state.setdefault("dispatch_cursor", 0)
    return state


def migrate_checkpoint_payload(
    payload: dict[str, Any],
    *,
    lease_active: bool | None = None,
    backend_round_trip: BackendRoundTrip | None = None,
) -> CheckpointMigrationResult:
    """Convert one legacy HarnessCheckpoint payload.

    ``lease_active``: tri-state proof from the business Store for ``active``
    checkpoints. ``None`` (unproven) never maps to a resumable ``running``.
    ``backend_round_trip(thread_id, graph_state)`` must raise when the state
    cannot be written and read back on the selected backend.
    """

    if not isinstance(payload, dict):
        return CheckpointMigrationResult(outcome="legacy_terminal",
                                         migration_reason=LEGACY_REASON_INVALID_PAYLOAD)
    if any(not payload.get(key) for key in _REQUIRED_IDENTITY):
        return CheckpointMigrationResult(outcome="legacy_terminal",
                                         migration_reason=LEGACY_REASON_MISSING_IDENTITY)

    status = payload.get("status") or "active"
    if status == "active":
        if lease_active is not True:
            reason = LEGACY_REASON_ACTIVE_LEASE_EXPIRED if lease_active is False \
                else LEGACY_REASON_ACTIVE_LEASE_UNVERIFIABLE
            return CheckpointMigrationResult(
                outcome="legacy_terminal",
                migration_reason=reason,
                harness_run_fields=_business_fields(payload, "paused"),
            )
        business_status = "running"
    elif status in _PRESERVED_STATUSES:
        business_status = status
    else:
        return CheckpointMigrationResult(outcome="legacy_terminal",
                                         migration_reason=LEGACY_REASON_INVALID_PAYLOAD,
                                         harness_run_fields=_business_fields(payload, "paused"))

    fields = _business_fields(payload, business_status)
    if status == "active" and lease_active is True:
        fields["migration_state"] = "converted"
    graph_state = _graph_state(payload)
    try:
        serialize_graph_state(graph_state)
    except ValueError:
        return CheckpointMigrationResult(outcome="legacy_terminal",
                                         migration_reason=LEGACY_REASON_STATE_TOO_LARGE,
                                         harness_run_fields=fields)

    if backend_round_trip is not None:
        try:
            backend_round_trip(payload["harness_run_id"], graph_state)
        except Exception:
            return CheckpointMigrationResult(outcome="legacy_terminal",
                                             migration_reason=LEGACY_REASON_ROUND_TRIP_FAILED,
                                             harness_run_fields=fields)

    return CheckpointMigrationResult(outcome="converted",
                                     harness_run_fields=fields,
                                     graph_state=graph_state)
