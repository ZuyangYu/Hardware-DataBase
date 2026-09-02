"""Serializable LangGraph state for document authoring.

The state is deliberately a small control-plane record.  Business objects are
reloaded from the AuthoringStore by their IDs at node boundaries; raw evidence,
file contents, paths and live connections never become checkpoint data.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from src.document_authoring.harness.agent_contracts import (
    GRAPH_STATE_VERSION,
    serialize_graph_state,
)


def _merge_dict(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _append_unique(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    result = list(left or [])
    for value in right or []:
        if value not in result:
            result.append(value)
    return result


def _merge_telemetry(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(left or {})
    for key, value in (right or {}).items():
        if isinstance(value, (int, float)) and isinstance(result.get(key), (int, float)):
            result[key] = result[key] + value
        else:
            result[key] = value
    return result


def _sum_int(left: int | None, right: int | None) -> int:
    """Add branch-local counters when a Send fan-out joins."""
    return int(left or 0) + int(right or 0)


def _max_int(left: int | None, right: int | None) -> int:
    """Keep the furthest deterministic dispatch cursor at a join."""
    return max(int(left or 0), int(right or 0))


def _last_value(left: Any, right: Any) -> Any:
    """Allow branch-local context markers without LastValue collisions."""
    return right if right is not None else left


class DocumentAuthoringState(TypedDict, total=False):
    """The persisted graph contract.

    ``Annotated`` reducers make parallel Send branches deterministic: maps are
    merged by ID and evidence/issue lists are appended once.  Final rendering
    uses the schema's unit order rather than completion order.
    """

    graph_state_version: int
    schema_version: str
    work_order_id: str
    harness_run_id: str
    run_manifest_id: str
    source_set_snapshot_id: str
    input_fingerprint: str
    unit_ids: list[str]
    unit_statuses: Annotated[dict[str, str], _merge_dict]
    unit_attempts: Annotated[dict[str, int], _merge_dict]
    dispatch_cursor: Annotated[int, _max_int]
    in_flight_unit_ids: Annotated[list[str], _append_unique]
    current_unit_id: Annotated[str | None, _last_value]
    completed_units: Annotated[int, _sum_int]
    total_units: Annotated[int, _max_int]
    current_node: Annotated[str, _last_value]
    evidence_registry_ids: Annotated[list[str], _append_unique]
    evidence_summaries: Annotated[dict[str, str], _merge_dict]
    proposal_statuses: Annotated[dict[str, str], _merge_dict]
    draft_ids: Annotated[list[str], _append_unique]
    issues: Annotated[list[dict[str, Any]], _append_unique]
    pending_human_action: dict[str, Any] | None
    retry_count: int
    retrieval_round_count: Annotated[int, _sum_int]
    step_count: Annotated[int, _sum_int]
    telemetry: Annotated[dict[str, Any], _merge_telemetry]
    paused: bool
    cancelled: bool
    completed: bool
    last_error: dict[str, Any] | None


def initial_authoring_state(
    *,
    work_order_id: str,
    harness_run_id: str,
    run_manifest_id: str,
    source_set_snapshot_id: str,
    input_fingerprint: str,
    schema_version: str,
    unit_ids: list[str],
    unit_statuses: dict[str, str] | None = None,
    unit_attempts: dict[str, int] | None = None,
    dispatch_cursor: int = 0,
) -> DocumentAuthoringState:
    """Create a fully-versioned, JSON-safe initial state."""
    return {
        "graph_state_version": GRAPH_STATE_VERSION,
        "schema_version": schema_version,
        "work_order_id": work_order_id,
        "harness_run_id": harness_run_id,
        "run_manifest_id": run_manifest_id,
        "source_set_snapshot_id": source_set_snapshot_id,
        "input_fingerprint": input_fingerprint,
        "unit_ids": list(unit_ids),
        "unit_statuses": dict(unit_statuses or {unit_id: "planned" for unit_id in unit_ids}),
        "unit_attempts": dict(unit_attempts or {unit_id: 1 for unit_id in unit_ids}),
        "dispatch_cursor": int(dispatch_cursor),
        "in_flight_unit_ids": [],
        "current_unit_id": None,
        "completed_units": 0,
        "total_units": len(unit_ids),
        "current_node": "load_context",
        "evidence_registry_ids": [],
        "evidence_summaries": {},
        "proposal_statuses": {},
        "draft_ids": [],
        "issues": [],
        "pending_human_action": None,
        "retry_count": 0,
        "retrieval_round_count": 0,
        "step_count": 0,
        "telemetry": {},
        "paused": False,
        "cancelled": False,
        "completed": False,
        "last_error": None,
    }


def _json_safe(value: Any) -> Any:
    """Reject live/domain objects instead of serializing them opportunistically."""
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    raise ValueError(f"graph state contains a non-serializable value: {type(value).__name__}")


def normalize_graph_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe state copy and apply the stable version contract."""
    normalized = _json_safe(dict(state))
    if not isinstance(normalized, dict):  # pragma: no cover - defensive
        raise ValueError("graph state must be an object")
    normalized.setdefault("graph_state_version", GRAPH_STATE_VERSION)
    # A checkpoint should not retain an unbounded set of arbitrary channel
    # names.  Unknown keys are allowed for forward-compatible node metadata,
    # but all values still pass the JSON/size gate below.
    serialize_graph_state(normalized)
    return normalized


def serialize_authoring_state(state: dict[str, Any]) -> bytes:
    """Serialize a state for tests, migration and checkpointer preflight."""
    normalized = normalize_graph_state(state)
    return serialize_graph_state(normalized)


def state_from_checkpoint(channel_values: dict[str, Any]) -> DocumentAuthoringState:
    """Validate/reload a checkpoint's channel values by ID-only contract."""
    normalized = normalize_graph_state(channel_values)
    return normalized  # type: ignore[return-value]


__all__ = [
    "DocumentAuthoringState", "GRAPH_STATE_VERSION", "initial_authoring_state",
    "normalize_graph_state", "serialize_authoring_state", "state_from_checkpoint",
]
