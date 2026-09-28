"""Conservative reconciliation for lost Runner leases and artifacts."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .artifact_broker import ArtifactBroker
from .client import SandboxServiceClient
from .models import UNCERTAIN_TASK_STATUSES
from .service import SandboxExecutionAdapter
from .task_store import SandboxTaskStore, TaskConflict, TaskNotFound


@dataclass
class ReconcileReport:
    deadline_expired: int = 0
    lease_expired: int = 0
    observed: int = 0
    completed: int = 0
    kept_unknown: int = 0
    artifacts_expired: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deadline_expired": self.deadline_expired,
            "lease_expired": self.lease_expired,
            "observed": self.observed,
            "completed": self.completed,
            "kept_unknown": self.kept_unknown,
            "artifacts_expired": self.artifacts_expired,
            "errors": list(self.errors),
        }


class SandboxReconciler:
    """Poll the Runner only through its authenticated client.

    A missing or ambiguous observation is deliberately retained as unknown;
    the reconciler never guesses that an external side effect did or did not
    happen.
    """

    def __init__(
        self,
        *,
        store: SandboxTaskStore | None = None,
        client: SandboxServiceClient | None = None,
        broker: ArtifactBroker | None = None,
    ) -> None:
        self.store = store or SandboxTaskStore()
        self.client = client
        self.broker = broker

    def run_once(self, *, limit: int = 100) -> ReconcileReport:
        report = ReconcileReport()
        try:
            report.deadline_expired = len(self.store.expire_deadlines(limit=limit))
        except Exception:  # noqa: BLE001 - reconciliation remains best effort
            report.errors.append("deadline_scan_failed")
        try:
            expired = self.store.reconcile_expired_leases(limit=limit)
            report.lease_expired = len(expired)
        except Exception:  # noqa: BLE001 - reconciliation is best effort and audited by caller
            report.errors.append("lease_scan_failed")

        if self.client is not None:
            adapter = SandboxExecutionAdapter(
                store=self.store,
                client=self.client,
                broker=self.broker,
            )
            for task in self.store.list(statuses=UNCERTAIN_TASK_STATUSES, limit=limit):
                try:
                    from .protocol import RequestEnvelope

                    envelope = RequestEnvelope.create(
                        actor_id=task.owner_id,
                        department_id=task.department_id,
                        policy_snapshot_hash=task.policy_snapshot_hash,
                        capability_snapshot=task.required_capabilities,
                        capability_snapshot_hash=task.capability_snapshot_hash,
                        idempotency_key=task.idempotency_key,
                        deadline=task.deadline_at,
                    )
                    observation = self.client.observe(task.task_id, envelope=envelope)
                    report.observed += 1
                    status = str(observation.status or observation.observed_state or "").lower()
                    if status not in {
                        "succeeded",
                        "completed",
                        "success",
                        "failed",
                        "error",
                        "cancelled",
                        "canceled",
                        "stopped",
                    }:
                        report.kept_unknown += 1
                        continue
                    reconciled = adapter.reconcile_observation(task, observation)
                    if reconciled is None:
                        report.kept_unknown += 1
                        continue
                    report.completed += 1
                except (TaskConflict, TaskNotFound):
                    report.kept_unknown += 1
                except Exception:  # noqa: BLE001 - never expose Runner internals
                    report.errors.append("runner_observation_failed")

        if self.broker is not None:
            try:
                report.artifacts_expired = len(self.broker.expire(limit=limit))
            except Exception:  # noqa: BLE001
                report.errors.append("artifact_expiry_failed")
        return report


class SandboxReconcilerLoop:
    """Optional background wrapper; construction never starts a thread."""

    def __init__(self, reconciler: SandboxReconciler, *, interval_seconds: float = 30.0) -> None:
        self.reconciler = reconciler
        self.interval_seconds = max(1.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sandbox-reconciler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.reconciler.run_once()
            except Exception:
                # The next cycle retries; the task ledger remains conservative.
                pass

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, float(timeout)))
        self._thread = None


__all__ = ["ReconcileReport", "SandboxReconciler", "SandboxReconcilerLoop"]
