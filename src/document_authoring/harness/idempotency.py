"""Deterministic idempotency keys for receipts, events and human decisions.

All preimages are encoded with :func:`canonical_json` (UTF-8, recursively
key-sorted, no whitespace, NaN/Infinity rejected) so every worker and language
runtime derives byte-identical keys. ``action`` entries must be versioned
allowlist operation names or small parameter objects; prompts, raw evidence,
file contents, credentials and wall-clock values are forbidden.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

_FORBIDDEN_ACTION_KEYS = {
    "prompt", "content", "evidence", "evidence_content", "file", "path",
    "credential", "password", "api_key", "secret", "token", "wall_clock",
    "occurred_at", "created_at", "timestamp", "now",
}


def canonical_json(value: Any) -> str:
    """Serialize ``value`` as UTF-8, key-sorted, whitespace-free JSON."""

    def reject(value: Any) -> Any:
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("canonical_json forbids NaN/Infinity")
            return value
        if isinstance(value, dict):
            return {str(k): reject(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [reject(v) for v in value]
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        raise ValueError(f"canonical_json only accepts JSON types, got: {type(value)!r}")

    return json.dumps(
        reject(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _validate_action(action: Any) -> Any:
    if isinstance(action, str):
        if not action.strip():
            raise ValueError("action must be a non-empty allowlist operation name")
        return action
    if isinstance(action, dict):
        if not action:
            raise ValueError("action parameter object must not be empty")
        for key in action:
            if str(key).casefold() in _FORBIDDEN_ACTION_KEYS:
                raise ValueError(f"action preimage must not contain key: {key}")
        return action
    raise ValueError("action must be a versioned operation name or small parameter object")


def _sha256(preimage: str) -> str:
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def receipt_action_key(
    *,
    harness_run_id: str,
    node_name: str,
    unit_id: str,
    attempt: int,
    input_fingerprint: str,
    action: Any,
) -> str:
    """``sha256(canonical_json({harness_run_id,node_name,unit_id,attempt,input_fingerprint,action}))``."""

    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return _sha256(canonical_json({
        "harness_run_id": harness_run_id,
        "node_name": node_name,
        "unit_id": unit_id,
        "attempt": attempt,
        "input_fingerprint": input_fingerprint,
        "action": _validate_action(action),
    }))


def execution_event_key(action_key: str, event_type: str) -> str:
    """``sha256(canonical_json({action_key,event_type}))`` so lifecycle facts never overwrite each other."""

    if not action_key:
        raise ValueError("event key requires a receipt action_key")
    if not event_type:
        raise ValueError("event key requires an event type")
    return _sha256(canonical_json({"action_key": action_key, "event_type": event_type}))


def human_decision_key(
    *,
    harness_run_id: str,
    pending_event_id: str,
    proposal_hash: str,
    decision: str,
) -> str:
    """Key for human approve/reject decisions on pending agent proposals."""

    normalized = str(decision).strip().casefold()
    if normalized not in {"approve", "reject"}:
        raise ValueError("decision must be 'approve' or 'reject'")
    return _sha256(canonical_json({
        "harness_run_id": harness_run_id,
        "pending_event_id": pending_event_id,
        "proposal_hash": proposal_hash,
        "decision": normalized,
    }))


def agent_thread_id(harness_run_id: str, field_id: str) -> str:
    """Stable per-field agent thread id; never derive this from ``hash()``."""

    if not harness_run_id or not field_id:
        raise ValueError("agent thread id requires harness_run_id and field_id")
    return _sha256(canonical_json({"harness_run_id": harness_run_id, "field_id": field_id}))
