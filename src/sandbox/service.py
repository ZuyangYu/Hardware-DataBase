"""HDB orchestration adapter for the external Sandbox Runner.

This module is deliberately a control-plane adapter.  It never starts a local
process and it never accepts a command, runtime socket, host path, or container
argument.  Every remote side effect is fenced by the durable task ledger; an
ambiguous response is retained as an uncertain/reconciliation state.
"""

from __future__ import annotations

import base64
import binascii
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .artifact_broker import (
    ArtifactBroker,
    ArtifactBrokerError,
    ArtifactStateConflict,
)
from .catalog import BundleNotAuthorized, BundleNotFound, SandboxCatalog
from .client import SandboxServiceClient
from .models import SandboxTask, SandboxTaskSpec
from .policy import require_execution_enabled
from .protocol import RequestEnvelope, RunnerObservation
from .task_store import (
    InvalidTransition,
    LeaseLost,
    SandboxTaskStore,
    TaskConflict,
    TaskNotFound,
)


class SandboxExecutionError(RuntimeError):
    """Bounded adapter error; callers must not expose Runner internals."""


def _record_task_metric(*, operation: str, status: str, duration_s: float = 0.0) -> None:
    """Best-effort low-cardinality metrics; never affect the control plane."""

    try:
        from src.observability.metrics import record_sandbox_task

        record_sandbox_task(operation=operation, status=status, duration_s=duration_s)
    except Exception:
        pass


def _safe_summary(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only non-sensitive, low-cardinality Runner response metadata."""

    raw = value or {}
    result: dict[str, Any] = {}
    for key in (
        "status",
        "observed_state",
        "cleanup_state",
        "execution_id",
        "backend_ref",
        "error_code",
    ):
        if key in raw and raw[key] not in (None, ""):
            result[key] = str(raw[key])[:256]
    return result


def _remote_status(value: RunnerObservation | Mapping[str, Any]) -> str:
    if isinstance(value, RunnerObservation):
        raw = value.status or value.observed_state
    elif isinstance(value, Mapping):
        raw = value.get("status") or value.get("observed_state") or value.get("state")
    else:
        raw = getattr(value, "status", "") or getattr(value, "observed_state", "") or getattr(value, "state", "")
    return str(raw or "").strip().lower()


def _is_success_status(status: str) -> bool:
    return status in {"succeeded", "success", "completed", "complete", "finished", "done"}


def _is_failure_status(status: str) -> bool:
    return status in {"failed", "failure", "error", "exception", "rejected"}


def _is_cancel_status(status: str) -> bool:
    return status in {"cancelled", "canceled", "stopped", "terminated"}


@dataclass
class DispatchReport:
    submitted: int = 0
    observed: int = 0
    collected: int = 0
    failed: int = 0
    uncertain: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "observed": self.observed,
            "collected": self.collected,
            "failed": self.failed,
            "uncertain": self.uncertain,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


class SandboxExecutionAdapter:
    """Submit/observe/cancel/collect through a durable task ledger."""

    def __init__(
        self,
        *,
        store: SandboxTaskStore | None = None,
        client: SandboxServiceClient | None = None,
        catalog: SandboxCatalog | None = None,
        broker: ArtifactBroker | None = None,
        worker_id: str = "hdb-sandbox-dispatcher",
        authorization_checker: Any | None = None,
    ) -> None:
        self.store = store or SandboxTaskStore()
        self.client = client
        self.catalog = catalog or SandboxCatalog(self.store.db_path)
        self.broker = broker
        self.worker_id = str(worker_id or "hdb-sandbox-dispatcher")[:128]
        self.authorization_checker = authorization_checker

    def _require_client(self) -> SandboxServiceClient:
        if self.client is None:
            raise SandboxExecutionError("sandbox Runner client is not configured")
        return self.client

    @staticmethod
    def _envelope(task: SandboxTask, *, audience: str = "sandbox-runner") -> RequestEnvelope:
        return RequestEnvelope.create(
            actor_id=task.owner_id,
            department_id=task.department_id,
            policy_snapshot_hash=task.policy_snapshot_hash,
            capability_snapshot=task.required_capabilities,
            capability_snapshot_hash=task.capability_snapshot_hash,
            idempotency_key=task.idempotency_key,
            deadline=task.deadline_at,
            audience=audience,
        )

    @staticmethod
    def _check_envelope(task: SandboxTask, envelope: RequestEnvelope) -> None:
        """Prevent a caller from replacing a task's immutable identity scope."""

        if envelope.actor_id != task.owner_id or envelope.department_id != task.department_id:
            raise SandboxExecutionError("remote request identity does not match the task")
        if envelope.policy_snapshot_hash != task.policy_snapshot_hash:
            raise SandboxExecutionError("remote request policy snapshot does not match the task")
        if envelope.capability_snapshot_hash and envelope.capability_snapshot_hash != task.capability_snapshot_hash:
            raise SandboxExecutionError("remote request capability snapshot does not match the task")
        if envelope.idempotency_key and envelope.idempotency_key != task.idempotency_key:
            raise SandboxExecutionError("remote request idempotency key does not match the task")

    def _authorize_task(self, task: SandboxTask) -> None:
        if self.authorization_checker is not None:
            try:
                allowed = self.authorization_checker(task)
            except Exception as exc:  # noqa: BLE001 - authorization is fail-closed
                raise SandboxExecutionError("task authorization snapshot could not be verified") from exc
            if allowed is False:
                raise SandboxExecutionError("task authorization snapshot is no longer valid")
        try:
            bundle = self.catalog.authorize(
                bundle_ref=task.bundle_ref,
                bundle_digest=task.bundle_digest,
                profile_ref=task.profile_ref,
                required_capabilities=task.required_capabilities,
                network_policy_ref=task.network_policy_ref,
                parameters=task.parameters,
            )
        except (BundleNotAuthorized, BundleNotFound) as exc:
            raise SandboxExecutionError("reviewed bundle is no longer authorized") from exc
        if task.policy_snapshot_hash != bundle.policy_snapshot_hash:
            raise SandboxExecutionError("task policy snapshot is stale")
        if self.broker is not None:
            for artifact_id in task.input_artifact_ids:
                self.broker.authorize_input(
                    artifact_id,
                    principal_id=task.owner_id,
                    department_id=task.department_id,
                )

    def admit(self, spec: SandboxTaskSpec, *, actor_id: str = "") -> SandboxTask:
        """Validate the reviewed bundle and persist an idempotent admission."""

        bundle = self.catalog.authorize(
            bundle_ref=spec.bundle_ref,
            bundle_digest=spec.bundle_digest,
            profile_ref=spec.profile_ref,
            required_capabilities=spec.required_capabilities,
            network_policy_ref=spec.network_policy_ref,
            parameters=spec.parameters,
        )
        if spec.policy_snapshot_hash != bundle.policy_snapshot_hash:
            raise SandboxExecutionError("task policy snapshot does not match the reviewed bundle")
        if self.broker is not None:
            for artifact_id in spec.input_artifact_ids:
                self.broker.authorize_input(
                    artifact_id,
                    principal_id=spec.owner_id,
                    department_id=spec.department_id,
                )
        return self.store.create(spec, actor_id=actor_id)

    def _claim_for_submit(self, task: SandboxTask, *, lease_seconds: int | None = None) -> SandboxTask:
        current = task
        if current.status == "admitted":
            current = self.store.enqueue(current.task_id, expected_revision=current.revision, actor_id=self.worker_id)
        if current.status == "running" and not current.execution_id:
            if current.lease_owner != self.worker_id:
                raise SandboxExecutionError("task is owned by another dispatcher lease")
            return current
        if current.status != "queued":
            if current.status == "running" and current.execution_id:
                return current
            raise SandboxExecutionError(f"task cannot be submitted from {current.status}")
        claimed = self.store.claim(current.task_id, self.worker_id, lease_seconds=lease_seconds)
        if claimed is None:
            raise SandboxExecutionError("task is already claimed or not runnable")
        return claimed

    def submit(
        self,
        task_id: str,
        *,
        envelope: RequestEnvelope | None = None,
        lease_seconds: int | None = None,
    ) -> SandboxTask:
        require_execution_enabled(operation="sandbox task submission")
        client = self._require_client()
        task = self.store.require(task_id)
        self._authorize_task(task)
        if task.status in {"succeeded", "failed", "cancelled", "expired", "rejected"}:
            return task
        if task.status == "running" and task.execution_id:
            return task
        claimed = self._claim_for_submit(task, lease_seconds=lease_seconds)
        request_envelope = envelope or self._envelope(claimed)
        self._check_envelope(claimed, request_envelope)
        spec = SandboxTaskSpec(
            idempotency_key=claimed.idempotency_key,
            owner_id=claimed.owner_id,
            department_id=claimed.department_id,
            bundle_ref=claimed.bundle_ref,
            bundle_digest=claimed.bundle_digest,
            profile_ref=claimed.profile_ref,
            policy_snapshot_hash=claimed.policy_snapshot_hash,
            capability_snapshot_hash=claimed.capability_snapshot_hash,
            kind=claimed.kind,
            network_policy_ref=claimed.network_policy_ref,
            required_capabilities=claimed.required_capabilities,
            input_artifact_ids=claimed.input_artifact_ids,
            parameters=claimed.parameters,
            deadline_at=claimed.deadline_at,
        )
        started = time.monotonic()
        try:
            response = client.submit(
                envelope=request_envelope,
                task_id=claimed.task_id,
                spec=spec,
            )
        except Exception as exc:  # noqa: BLE001 - preserve uncertainty
            updated = self._mark_submit_unknown(claimed, exc)
            _record_task_metric(
                operation="submit",
                status="unknown",
                duration_s=time.monotonic() - started,
            )
            return updated
        if not isinstance(response, Mapping):
            updated = self._mark_submit_unknown(claimed, ValueError("invalid Runner response"))
            _record_task_metric(operation="submit", status="unknown")
            return updated
        execution_id = str(response.get("execution_id") or "").strip()
        backend_ref = str(response.get("backend_ref") or "").strip()
        observed = str(response.get("observed_state") or response.get("status") or "submitted").strip()
        # A successful HTTP response without a stable execution identity cannot
        # be safely retried or cancelled, so it is explicitly reconciled.
        if not execution_id:
            updated = self._mark_submit_unknown(claimed, ValueError("Runner response omitted execution_id"))
            _record_task_metric(operation="submit", status="unknown")
            return updated
        _record_task_metric(
            operation="submit",
            status="ok",
            duration_s=time.monotonic() - started,
        )
        try:
            return self.store.record_observation(
                claimed.task_id,
                lease_owner=self.worker_id,
                fencing_token=claimed.fencing_token,
                expected_revision=claimed.revision,
                observed_state=observed,
                execution_id=execution_id,
                backend_ref=backend_ref,
                cleanup_state=str(response.get("cleanup_state") or "running"),
                metadata={"accepted": True, "remote_status": _remote_status(response)},
            )
        except (LeaseLost, TaskConflict):
            # A stale worker must not overwrite a newer owner.  Re-read only.
            return self.store.require(claimed.task_id)

    def _mark_submit_unknown(self, task: SandboxTask, exc: Exception) -> SandboxTask:
        try:
            return self.store.transition(
                task.task_id,
                "reconcile_required",
                lease_owner=self.worker_id,
                fencing_token=task.fencing_token,
                require_lease=True,
                expected_revision=task.revision,
                actor_id=self.worker_id,
                action="sandbox.task_reconcile",
                observed_state="submit_unknown",
                cleanup_state="unknown",
                error_code="runner_submit_unknown",
                error_message=type(exc).__name__,
                metadata={"exception_type": type(exc).__name__},
            )
        except (LeaseLost, TaskConflict, TaskNotFound):
            return self.store.require(task.task_id)

    def observe(self, task_id: str, *, envelope: RequestEnvelope | None = None) -> SandboxTask:
        require_execution_enabled(operation="sandbox task observation")
        client = self._require_client()
        task = self.store.require(task_id)
        if task.status in {"succeeded", "failed", "cancelled", "expired", "rejected"}:
            return task
        request_envelope = envelope or self._envelope(task)
        self._check_envelope(task, request_envelope)
        started = time.monotonic()
        try:
            observation = client.observe(task_id, envelope=request_envelope)
        except Exception as exc:  # noqa: BLE001
            # Do not fabricate a terminal state.  If the lease is still ours,
            # make the ambiguity durable; otherwise leave the newer state alone.
            if task.lease_owner == self.worker_id and task.lease_expires_at:
                try:
                    return self.store.transition(
                        task.task_id,
                        "reconcile_required",
                        lease_owner=self.worker_id,
                        fencing_token=task.fencing_token,
                        require_lease=True,
                        expected_revision=task.revision,
                        actor_id=self.worker_id,
                        action="sandbox.task_reconcile",
                        observed_state="observe_unknown",
                        cleanup_state="unknown",
                        error_code="runner_observe_unknown",
                        error_message=type(exc).__name__,
                    )
                except (LeaseLost, TaskConflict):
                    pass
            _record_task_metric(
                operation="observe",
                status="unknown",
                duration_s=time.monotonic() - started,
            )
            return self.store.require(task.task_id)
        _record_task_metric(
            operation="observe",
            status="ok",
            duration_s=time.monotonic() - started,
        )
        return self._apply_observation(task, observation)

    def _apply_observation(self, task: SandboxTask, observation: RunnerObservation) -> SandboxTask:
        status = _remote_status(observation)
        execution_id = observation.execution_id or task.execution_id
        if not task.lease_owner or not task.lease_expires_at:
            return task
        try:
            if _is_failure_status(status):
                return self.store.fail(
                    task.task_id,
                    lease_owner=task.lease_owner,
                    fencing_token=task.fencing_token,
                    error_code=observation.error_code or "runner_failed",
                    error_message="Runner reported a failed execution",
                    expected_revision=task.revision,
                )
            if _is_cancel_status(status):
                return self.store.transition(
                    task.task_id,
                    "cancelled",
                    lease_owner=task.lease_owner,
                    fencing_token=task.fencing_token,
                    require_lease=True,
                    expected_revision=task.revision,
                    actor_id=self.worker_id,
                    action="sandbox.task_cancel_confirmed",
                    observed_state=status,
                    cleanup_state=observation.cleanup_state or "complete",
                    execution_id=execution_id,
                )
            if _is_success_status(status):
                # Collection remains a separate fenced operation.  This avoids
                # marking success from a status message before artifacts pass
                # hash, ACL, scanner, and atomic-commit checks.
                if task.status == "cancel_requested":
                    return self.store.transition(
                        task.task_id,
                        "reconcile_required",
                        lease_owner=task.lease_owner,
                        fencing_token=task.fencing_token,
                        require_lease=True,
                        expected_revision=task.revision,
                        actor_id=self.worker_id,
                        action="sandbox.task_reconcile",
                        observed_state="completed_after_cancel_request",
                        cleanup_state=observation.cleanup_state or "unknown",
                        execution_id=execution_id,
                    )
                return self.store.transition(
                    task.task_id,
                    "collecting",
                    lease_owner=task.lease_owner,
                    fencing_token=task.fencing_token,
                    require_lease=True,
                    expected_revision=task.revision,
                    actor_id=self.worker_id,
                    action="sandbox.task_collect",
                    observed_state=status,
                    cleanup_state=observation.cleanup_state or "running",
                    execution_id=execution_id,
                )
            return self.store.record_observation(
                task.task_id,
                lease_owner=task.lease_owner,
                fencing_token=task.fencing_token,
                expected_revision=task.revision,
                observed_state=observation.observed_state or observation.status,
                execution_id=execution_id,
                cleanup_state=observation.cleanup_state,
                metadata={"remote_status": status},
            )
        except (LeaseLost, TaskConflict, InvalidTransition):
            return self.store.require(task.task_id)

    def cancel(
        self,
        task_id: str,
        *,
        actor_id: str = "",
        envelope: RequestEnvelope | None = None,
        reason: str = "user_requested",
    ) -> SandboxTask:
        task = self.store.require(task_id)
        updated = self.store.request_cancel(task_id, actor_id=actor_id, reason=reason)
        if self.client is None or not task.execution_id:
            return updated
        request_envelope = envelope or self._envelope(task)
        self._check_envelope(task, request_envelope)
        try:
            response = self.client.cancel(task_id, envelope=request_envelope)
        except Exception:
            # The desired state is durable, but the remote side effect is
            # unknown.  Keep cancel_requested/reconciliation semantics.
            _record_task_metric(operation="cancel", status="unknown")
            return self.store.require(task_id)
        status = _remote_status(response)
        _record_task_metric(operation="cancel", status="ok")
        current = self.store.require(task_id)
        if _is_cancel_status(status) and current.lease_owner and current.lease_expires_at:
            try:
                return self.store.transition(
                    task_id,
                    "cancelled",
                    lease_owner=current.lease_owner,
                    fencing_token=current.fencing_token,
                    require_lease=True,
                    expected_revision=current.revision,
                    actor_id=actor_id or self.worker_id,
                    action="sandbox.task_cancel_confirmed",
                    observed_state=status,
                    cleanup_state=str(response.get("cleanup_state") or "complete")
                    if isinstance(response, Mapping)
                    else "complete",
                )
            except (LeaseLost, TaskConflict, InvalidTransition):
                pass
        return self.store.require(task_id)

    def collect(self, task_id: str, *, envelope: RequestEnvelope | None = None) -> dict[str, Any]:
        require_execution_enabled(operation="sandbox artifact collection")
        client = self._require_client()
        task = self.store.require(task_id)
        if task.status in {"succeeded", "failed", "cancelled", "expired", "rejected"}:
            return {"task": task.to_dict(), "output_manifest": dict(task.output_manifest)}
        if task.status == "running":
            if not task.lease_owner or not task.lease_expires_at:
                raise SandboxExecutionError("task does not have a live collection lease")
            try:
                task = self.store.transition(
                    task.task_id,
                    "collecting",
                    lease_owner=task.lease_owner,
                    fencing_token=task.fencing_token,
                    require_lease=True,
                    expected_revision=task.revision,
                    actor_id=self.worker_id,
                    action="sandbox.task_collect",
                    observed_state="collecting",
                    cleanup_state="running",
                )
            except (LeaseLost, TaskConflict) as exc:
                raise SandboxExecutionError("task collection lease was lost") from exc
        if task.status != "collecting" or not task.lease_owner or not task.lease_expires_at:
            raise SandboxExecutionError(f"task cannot be collected from {task.status}")
        request_envelope = envelope or self._envelope(task)
        self._check_envelope(task, request_envelope)
        started = time.monotonic()
        try:
            response = client.collect(task_id, envelope=request_envelope)
        except Exception as exc:  # noqa: BLE001
            _record_task_metric(operation="collect", status="unknown", duration_s=time.monotonic() - started)
            return self._collection_unknown(task, exc)
        if not isinstance(response, Mapping):
            _record_task_metric(operation="collect", status="unknown")
            return self._collection_unknown(task, ValueError("invalid Runner collection response"))
        status = _remote_status(response)
        if status in {"queued", "pending", "running", "started", "collecting", "accepted"}:
            try:
                current = self.store.record_observation(
                    task.task_id,
                    lease_owner=task.lease_owner,
                    fencing_token=task.fencing_token,
                    expected_revision=task.revision,
                    observed_state=status,
                    execution_id=str(response.get("execution_id") or task.execution_id),
                    cleanup_state=str(response.get("cleanup_state") or "running"),
                )
            except (LeaseLost, TaskConflict):
                current = self.store.require(task.task_id)
            return {"task": current.to_dict(), "runner": _safe_summary(response)}
        if _is_failure_status(status):
            try:
                failed = self.store.fail(
                    task.task_id,
                    lease_owner=task.lease_owner,
                    fencing_token=task.fencing_token,
                    expected_revision=task.revision,
                    error_code=str(response.get("error_code") or "runner_failed"),
                    error_message="Runner reported a failed collection",
                )
            except (LeaseLost, TaskConflict):
                failed = self.store.require(task.task_id)
            return {"task": failed.to_dict(), "runner": _safe_summary(response)}
        if _is_cancel_status(status):
            try:
                cancelled = self.store.transition(
                    task.task_id,
                    "cancelled",
                    lease_owner=task.lease_owner,
                    fencing_token=task.fencing_token,
                    require_lease=True,
                    expected_revision=task.revision,
                    actor_id=self.worker_id,
                    action="sandbox.task_cancel_confirmed",
                    observed_state=status,
                    cleanup_state=str(response.get("cleanup_state") or "complete"),
                )
            except (LeaseLost, TaskConflict, InvalidTransition):
                cancelled = self.store.require(task.task_id)
            return {"task": cancelled.to_dict(), "runner": _safe_summary(response)}

        try:
            manifest = self._materialize_output(task, response)
            bundle = self.catalog.authorize(
                bundle_ref=task.bundle_ref,
                bundle_digest=task.bundle_digest,
                profile_ref=task.profile_ref,
                required_capabilities=task.required_capabilities,
                network_policy_ref=task.network_policy_ref,
                parameters=task.parameters,
            )
            self.catalog.validate_output(bundle, manifest)
            current = self.store.require(task.task_id)
            completed = self.store.complete(
                current.task_id,
                lease_owner=current.lease_owner,
                fencing_token=current.fencing_token,
                expected_revision=current.revision,
                output_manifest=manifest,
                actor_id=self.worker_id,
            )
        except (LeaseLost, TaskConflict, InvalidTransition) as exc:
            raise SandboxExecutionError("task collection result lost its fencing lease") from exc
        except Exception as exc:  # noqa: BLE001 - output failures are not success
            _record_task_metric(operation="collect", status="failed")
            self._collection_unknown(task, exc)
            raise SandboxExecutionError("Runner output could not be verified") from exc
        _record_task_metric(
            operation="collect",
            status="ok",
            duration_s=time.monotonic() - started,
        )
        return {
            "task": completed.to_dict(),
            "output_manifest": dict(completed.output_manifest),
            "runner": _safe_summary(response),
        }

    def _collection_unknown(self, task: SandboxTask, exc: Exception) -> dict[str, Any]:
        current = self.store.require(task.task_id)
        if current.lease_owner and current.lease_owner == task.lease_owner and current.lease_expires_at:
            try:
                current = self.store.transition(
                    current.task_id,
                    "reconcile_required",
                    lease_owner=current.lease_owner,
                    fencing_token=current.fencing_token,
                    require_lease=True,
                    expected_revision=current.revision,
                    actor_id=self.worker_id,
                    action="sandbox.task_reconcile",
                    observed_state="collect_unknown",
                    cleanup_state="unknown",
                    error_code="runner_collect_unknown",
                    error_message=type(exc).__name__,
                )
            except (LeaseLost, TaskConflict, InvalidTransition):
                current = self.store.require(task.task_id)
        return {"task": current.to_dict(), "runner": {"status": "unknown"}}

    def _materialize_output(self, task: SandboxTask, response: Mapping[str, Any]) -> dict[str, Any]:
        """Stage/scan/commit every inline output and verify every reference."""

        if self.broker is None:
            raise SandboxExecutionError("Artifact Broker is not configured")
        raw_manifest = response.get("output_manifest") or response.get("manifest")
        if raw_manifest is None:
            raw_manifest = {"items": response.get("artifacts", ())}
        if not isinstance(raw_manifest, Mapping):
            raise SandboxExecutionError("Runner returned an invalid artifact manifest")
        raw_items = raw_manifest.get("items", raw_manifest.get("artifacts", ()))
        if isinstance(raw_items, Mapping):
            raw_items = [raw_items]
        if not isinstance(raw_items, (list, tuple)) or not raw_items:
            raise SandboxExecutionError("Runner returned no output artifact")
        if "sandbox.write_artifact" not in set(task.required_capabilities):
            raise SandboxExecutionError("sandbox.write_artifact capability is required for output artifacts")
        if len(raw_items) > 256:
            raise SandboxExecutionError("Runner returned too many output artifacts")
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise SandboxExecutionError("Runner returned an invalid artifact item")
            forbidden = {"storage_ref", "host_path", "path", "command", "shell", "env"}
            if any(str(key).lower() in forbidden for key in raw):
                raise SandboxExecutionError("Runner artifact contains a forbidden runtime field")
            artifact_id = str(raw.get("artifact_id") or "").strip()
            execution_id = str(raw.get("execution_id") or task.execution_id or "").strip()
            if task.execution_id and execution_id and execution_id != task.execution_id:
                raise SandboxExecutionError("artifact execution binding does not match the task")
            content_b64 = raw.get("content_base64")
            if content_b64 is not None:
                if not isinstance(content_b64, str) or len(content_b64) > 75_000_000:
                    raise SandboxExecutionError("Runner artifact payload is too large")
                try:
                    content = base64.b64decode(content_b64, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise SandboxExecutionError("Runner artifact payload is invalid") from exc
                try:
                    staged = self.broker.stage_output(
                        task_id=task.task_id,
                        owner_id=task.owner_id,
                        department_id=task.department_id,
                        filename=str(raw.get("filename") or "output.txt"),
                        content=content,
                        sha256=str(raw.get("sha256") or ""),
                        execution_id=execution_id,
                        media_type=str(raw.get("media_type") or ""),
                        artifact_id=artifact_id,
                        metadata={"source": "sandbox_runner", "bundle_ref": task.bundle_ref},
                    )
                    self.broker.scan(
                        staged.artifact_id,
                        department_id=task.department_id,
                        task_id=task.task_id,
                    )
                    committed = self.broker.commit(
                        staged.artifact_id,
                        department_id=task.department_id,
                        task_id=task.task_id,
                    )
                except (ArtifactBrokerError, ArtifactStateConflict, ValueError) as exc:
                    raise SandboxExecutionError("Runner output artifact was rejected") from exc
                item = committed.to_dict()
            else:
                if not artifact_id:
                    raise SandboxExecutionError("Runner artifact reference is missing")
                try:
                    committed = self.broker.require(artifact_id)
                except Exception as exc:  # noqa: BLE001
                    raise SandboxExecutionError("Runner artifact reference is not known to the broker") from exc
                if committed.role != "output" or committed.task_id != task.task_id:
                    raise SandboxExecutionError("Runner artifact ownership does not match the task")
                if committed.department_id != task.department_id or committed.owner_id != task.owner_id:
                    raise SandboxExecutionError("Runner artifact tenant ownership does not match the task")
                if task.execution_id and committed.execution_id and committed.execution_id != task.execution_id:
                    raise SandboxExecutionError("Runner artifact execution ownership does not match the task")
                if committed.state not in {"committed", "consumed"}:
                    raise SandboxExecutionError("Runner artifact is not committed")
                item = committed.to_dict()
            item["state"] = "committed"
            items.append(
                {
                    key: item[key]
                    for key in ("artifact_id", "sha256", "state", "role", "filename", "media_type", "size")
                    if key in item
                }
            )
        return {"items": items}

    def reconcile_observation(
        self,
        task: SandboxTask,
        observation: RunnerObservation,
    ) -> SandboxTask | None:
        """Apply a remote observation to an uncertain task conservatively.

        This path has no HDB lease by design: the previous worker lost its
        lease.  It can only finalize a task when the Runner supplies a stable
        execution identity and the returned artifacts can independently pass
        Broker ownership, integrity, scanner, and commit checks.
        """

        status = _remote_status(observation)
        execution_id = observation.execution_id or task.execution_id
        if task.execution_id and execution_id and execution_id != task.execution_id:
            raise SandboxExecutionError("reconciliation execution identity does not match the task")
        if _is_failure_status(status):
            return self.store.reconcile_outcome(
                task.task_id,
                "failed",
                execution_id=execution_id,
                cleanup_state=observation.cleanup_state or "complete",
                proof={"observed_state": observation.observed_state[:120]},
            )
        if _is_cancel_status(status):
            return self.store.reconcile_outcome(
                task.task_id,
                "cancelled",
                execution_id=execution_id,
                cleanup_state=observation.cleanup_state or "complete",
                proof={"observed_state": observation.observed_state[:120]},
            )
        if not _is_success_status(status):
            return None
        response: dict[str, Any] = {
            "status": status,
            "execution_id": execution_id,
            "output_manifest": dict(observation.output_manifest or {}),
        }
        manifest = self._materialize_output(task, response)
        bundle = self.catalog.authorize(
            bundle_ref=task.bundle_ref,
            bundle_digest=task.bundle_digest,
            profile_ref=task.profile_ref,
            required_capabilities=task.required_capabilities,
            network_policy_ref=task.network_policy_ref,
            parameters=task.parameters,
        )
        self.catalog.validate_output(bundle, manifest)
        return self.store.reconcile_outcome(
            task.task_id,
            "succeeded",
            execution_id=execution_id,
            output_manifest=manifest,
            cleanup_state=observation.cleanup_state or "complete",
            proof={"observed_state": observation.observed_state[:120], "artifacts_committed": True},
        )

    def dispatch_once(self, *, limit: int = 16) -> DispatchReport:
        """Advance a bounded batch; safe to call from a dedicated worker loop."""

        report = DispatchReport()
        if self.client is None:
            report.skipped = 1
            report.errors.append("client_not_configured")
            return report
        try:
            require_execution_enabled(operation="sandbox dispatcher")
        except Exception:
            report.skipped = 1
            report.errors.append("execution_disabled")
            return report
        for task in self.store.list(statuses=("admitted", "queued"), limit=max(1, min(limit, 100))):
            try:
                updated = self.submit(task.task_id)
                if updated.status == "reconcile_required":
                    report.uncertain += 1
                else:
                    report.submitted += 1
            except Exception:
                report.errors.append("submit_failed")
        for task in self.store.list(statuses=("running", "collecting", "cancel_requested"), limit=max(1, min(limit, 100))):
            try:
                updated = self.observe(task.task_id)
                report.observed += 1
                if updated.status == "collecting":
                    result = self.collect(task.task_id)
                    if str(result.get("task", {}).get("status") or "") == "succeeded":
                        report.collected += 1
                elif updated.status in {"failed", "cancelled"}:
                    report.failed += 1
            except Exception:
                report.errors.append("observe_or_collect_failed")
        return report


class SandboxDispatcherLoop:
    """Optional background dispatcher; construction never starts a thread."""

    def __init__(self, adapter: SandboxExecutionAdapter, *, interval_seconds: float = 2.0) -> None:
        self.adapter = adapter
        self.interval_seconds = max(0.25, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sandbox-dispatcher", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.adapter.dispatch_once()
            except Exception:
                pass

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, float(timeout)))
        self._thread = None


ExecutionAdapter = SandboxExecutionAdapter


__all__ = [
    "DispatchReport",
    "ExecutionAdapter",
    "SandboxDispatcherLoop",
    "SandboxExecutionAdapter",
    "SandboxExecutionError",
]
