"""Durable chat-authoring job contracts: idempotency, leases and retries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.pipelines.document_rag.schemas import RequestContext
from src.workers.main import HardwareWorker


def _store(tmp_path) -> DocumentAuthoringJobStore:
    return DocumentAuthoringJobStore(str(tmp_path / "jobs.db"))


def _create(store: DocumentAuthoringJobStore, *, request_id: str = "request-1", **kwargs):
    return store.create_job(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="chat-1",
        client_request_id=request_id,
        operation="generate_work_order",
        work_order_id=kwargs.pop("work_order_id", "wo-1"),
        payload=kwargs.pop("payload", {"work_order_id": "wo-1", "knowledge_base_name": "hardware"}),
        **kwargs,
    )


def test_create_job_is_idempotent_and_conflicts_fail_closed(tmp_path):
    store = _store(tmp_path)
    first = _create(store)
    replay = _create(store)

    assert replay.job_id == first.job_id
    assert replay.status == "queued"
    assert len(store.list_pending_outbox()) == 1

    with pytest.raises(ValueError, match="idempotency key conflicts"):
        _create(store, payload={"work_order_id": "wo-other", "knowledge_base_name": "hardware"})


def test_claim_heartbeat_and_lease_expired_takeover_are_atomic(tmp_path):
    store = _store(tmp_path)
    job = _create(store)

    claimed = store.claim(job.job_id, "worker-a", lease_seconds=5)
    assert claimed is not None
    assert (claimed.status, claimed.attempt, claimed.lease_token) == ("running", 1, 1)
    assert store.claim(job.job_id, "worker-b", lease_seconds=5) is None

    renewed = store.heartbeat(job.job_id, "worker-a", claimed.lease_token, lease_seconds=30)
    assert renewed.lease_owner == "worker-a"
    with pytest.raises(RuntimeError, match="lease lost"):
        store.heartbeat(job.job_id, "worker-b", claimed.lease_token, lease_seconds=30)

    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE document_authoring_jobs SET lease_expires_at = ? WHERE job_id = ?",
            (expired, job.job_id),
        )
    adopted = store.claim(job.job_id, "worker-b", lease_seconds=5)
    assert adopted is not None
    assert adopted.lease_owner == "worker-b"
    assert adopted.attempt == 2
    assert adopted.lease_token == 2


def test_retry_backoff_reaches_dead_letter_and_outbox_is_observable(tmp_path):
    store = _store(tmp_path)
    job = _create(store, request_id="request-retry", max_attempts=2)

    first = store.claim(job.job_id, "worker-a", lease_seconds=5)
    assert first is not None
    queued = store.fail(
        job.job_id, "worker-a", first.lease_token, "provider unavailable",
        retryable=True, backoff_seconds=0,
    )
    assert queued.status == "queued"
    with store._connect() as conn:
        conn.execute(
            "UPDATE document_authoring_jobs SET available_at = ? WHERE job_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), job.job_id),
        )

    second = store.claim(job.job_id, "worker-b", lease_seconds=5)
    assert second is not None and second.attempt == 2
    dead = store.fail(
        job.job_id, "worker-b", second.lease_token, "provider still unavailable",
        retryable=True,
    )
    assert dead.status == "dead_letter"
    assert dead.dead_letter is True
    assert store.list_pending(limit=10) == []
    outbox = store.list_pending_outbox()
    assert outbox == []
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, attempt, last_error FROM document_authoring_job_outbox WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["attempt"] >= 1
    assert "still unavailable" in row["last_error"]


def test_cancel_is_idempotent_and_blocks_late_completion(tmp_path):
    store = _store(tmp_path)
    job = _create(store, request_id="request-cancel")
    claimed = store.claim(job.job_id, "worker-a", lease_seconds=30)
    assert claimed is not None

    cancelled = store.cancel(job.job_id, reason="user requested cancellation")
    replay = store.cancel(job.job_id, reason="same cancellation")
    assert cancelled is not None and cancelled.status == "cancelled"
    assert replay is not None and replay.status == "cancelled"
    with pytest.raises(RuntimeError, match="lease lost"):
        store.complete(job.job_id, "worker-a", claimed.lease_token, {"status": "completed"})


def test_status_lookup_and_queue_metrics_are_scope_safe(tmp_path):
    store = _store(tmp_path)
    job = _create(store, request_id="request-status")
    assert store.get_by_work_order("wo-1", tenant_id="tenant-a", user_id="user-a").job_id == job.job_id
    assert store.get_by_work_order("wo-1", tenant_id="tenant-b") is None
    depth, oldest_age = store.queue_state()
    assert depth == 1
    assert oldest_age >= 0


def test_worker_dispatches_resume_job_after_restartable_claim(tmp_path, monkeypatch):
    store = _store(tmp_path)
    job = store.create_job(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="chat-1",
        client_request_id="resume-1",
        operation="resume_work_order",
        work_order_id="wo-1",
        payload={
            "work_order_id": "wo-1",
            "knowledge_base_name": "hardware",
            "harness_run_id": "run-1",
        },
    )
    calls = []
    worker = object.__new__(HardwareWorker)
    worker.worker_id = "worker-resume"
    worker.document_jobs = store
    worker.auth = type("Auth", (), {
        "get_user_by_username": lambda _self, _value: type(
            "User", (), {"username": "user-a", "is_active": True}
        )(),
        "get_user_by_id": lambda _self, _value: None,
    })()
    worker.pipeline = type("Pipeline", (), {
        "resume_knowledge_base_document_generation_run": lambda _self, ctx, work_order_id, run_id, should_cancel=None: (
            calls.append((ctx, work_order_id, run_id, should_cancel))
            or type("Artifact", (), {"artifact_id": "artifact-1", "stage": "candidate"})()
        ),
    })()
    monkeypatch.setattr(
        "src.workers.main.build_context_for_user",
        lambda _user, _kb_name, auth=None: RequestContext(
            user_id="user-a",
            tenant_id="tenant-a",
            metadata={"resource_department_id": 7},
            kb_permissions={"7:hardware": "write"},
        ),
    )

    assert worker._process_document_authoring_jobs(limit=1) is True
    completed = store.get(job.job_id)
    assert completed is not None and completed.status == "succeeded"
    assert calls and calls[0][1:] == ("wo-1", "run-1", calls[0][3])
    assert callable(calls[0][3])
