"""Typed HDB-side models for sandbox task admission and observation.

The models describe references and structured metadata only.  They intentionally
have no command, shell, volume, environment snapshot, or runtime-specific
fields.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


TASK_STATUSES = (
    "admitted",
    "queued",
    "running",
    "collecting",
    "succeeded",
    "rejected",
    "timed_out",
    "cancel_requested",
    "cancelled",
    "failed",
    "expired",
    "unknown",
    "cleanup_pending",
    "reconcile_required",
)

TERMINAL_TASK_STATUSES = frozenset({"succeeded", "rejected", "cancelled", "failed", "expired"})
UNCERTAIN_TASK_STATUSES = frozenset({"unknown", "cleanup_pending", "reconcile_required"})


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value deterministically."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SandboxTaskSpec:
    """Admission request accepted by the HDB control plane.

    ``parameters`` must already be validated against the reviewed bundle
    schema.  The policy layer rejects runtime-control keys before persistence.
    """

    idempotency_key: str
    owner_id: str
    department_id: str
    bundle_ref: str
    bundle_digest: str
    profile_ref: str
    policy_snapshot_hash: str
    # Snapshot of user/department/task capability authorization.  It is kept
    # separate from the reviewed bundle policy hash so a bundle can be shared
    # without pretending that every principal has the same permissions.
    capability_snapshot_hash: str = ""
    kind: str = "task"
    network_policy_ref: str = ""
    required_capabilities: tuple[str, ...] = ()
    input_artifact_ids: tuple[str, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    deadline_at: str = ""
    skill_ref: str = ""
    skill_content_digest: str = ""

    def fingerprint(self) -> str:
        """Return a stable request identity without exposing parameter values."""

        payload = {
            "owner_id": self.owner_id,
            "department_id": self.department_id,
            "bundle_ref": self.bundle_ref,
            "bundle_digest": self.bundle_digest,
            "profile_ref": self.profile_ref,
            "policy_snapshot_hash": self.policy_snapshot_hash,
            "capability_snapshot_hash": self.capability_snapshot_hash,
            "kind": self.kind,
            "network_policy_ref": self.network_policy_ref,
            "required_capabilities": sorted(set(self.required_capabilities)),
            "input_artifact_ids": list(self.input_artifact_ids),
            "parameters": self.parameters,
            "deadline_at": self.deadline_at,
            "skill_ref": self.skill_ref,
            "skill_content_digest": self.skill_content_digest,
        }
        return sha256_text(canonical_json(payload))


@dataclass(frozen=True)
class SandboxTask:
    task_id: str
    idempotency_key: str
    request_fingerprint: str
    owner_id: str
    department_id: str
    kind: str
    bundle_ref: str
    bundle_digest: str
    profile_ref: str
    policy_snapshot_hash: str
    capability_snapshot_hash: str = ""
    network_policy_ref: str = ""
    required_capabilities: tuple[str, ...] = ()
    input_artifact_ids: tuple[str, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    output_manifest: Mapping[str, Any] = field(default_factory=dict)
    status: str = "admitted"
    desired_state: str = "running"
    observed_state: str = ""
    cleanup_state: str = "pending"
    execution_id: str = ""
    backend_ref: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    fencing_token: int = 0
    attempt: int = 0
    deadline_at: str = ""
    error_code: str = ""
    error_message: str = ""
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    skill_ref: str = ""
    skill_content_digest: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def is_uncertain(self) -> bool:
        return self.status in UNCERTAIN_TASK_STATUSES

    @property
    def parameters_digest(self) -> str:
        return sha256_text(canonical_json(self.parameters))

    def to_dict(self, *, include_control: bool = False) -> dict[str, Any]:
        """External-safe view; structured parameters are represented by a digest.

        Lease owners and fencing tokens are control-plane credentials.  They
        are available on the typed object for workers, but are omitted from
        ordinary API/catalog projections unless an explicitly internal caller
        requests the control view.
        """

        result = {
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "owner_id": self.owner_id,
            "department_id": self.department_id,
            "kind": self.kind,
            "bundle_ref": self.bundle_ref,
            "bundle_digest": self.bundle_digest,
            "profile_ref": self.profile_ref,
            "policy_snapshot_hash": self.policy_snapshot_hash,
            "capability_snapshot_hash": self.capability_snapshot_hash,
            "network_policy_ref": self.network_policy_ref,
            "required_capabilities": list(self.required_capabilities),
            "input_artifact_ids": list(self.input_artifact_ids),
            "output_manifest": dict(self.output_manifest),
            "parameters_digest": self.parameters_digest,
            "skill_ref": self.skill_ref,
            "status": self.status,
            "desired_state": self.desired_state,
            "observed_state": self.observed_state,
            "cleanup_state": self.cleanup_state,
            "execution_id": self.execution_id,
            "backend_ref": self.backend_ref,
            "attempt": self.attempt,
            "deadline_at": self.deadline_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if include_control:
            result.update(
                {
                    "lease_owner": self.lease_owner,
                    "lease_expires_at": self.lease_expires_at,
                    "fencing_token": self.fencing_token,
                }
            )
        return result


__all__ = [
    "TERMINAL_TASK_STATUSES",
    "TASK_STATUSES",
    "UNCERTAIN_TASK_STATUSES",
    "SandboxTask",
    "SandboxTaskSpec",
    "canonical_json",
    "sha256_text",
]
