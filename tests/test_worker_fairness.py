"""Worker scheduling contracts for chat/document queue fairness."""

from __future__ import annotations

import time
from types import SimpleNamespace

from src.workers.main import HardwareWorker


class _DocumentJobStore:
    def __init__(self):
        self.jobs = [
            SimpleNamespace(
                job_id="job-1",
                operation="generate_work_order",
                user_id="user-1",
                work_order_id="wo-1",
                lease_token=1,
                payload={"work_order_id": "wo-1", "knowledge_base_name": "hardware"},
            ),
            SimpleNamespace(
                job_id="job-2",
                operation="generate_work_order",
                user_id="user-1",
                work_order_id="wo-2",
                lease_token=1,
                payload={"work_order_id": "wo-2", "knowledge_base_name": "hardware"},
            ),
        ]
        self.claimed: list[str] = []
        self.completed: list[str] = []

    def list_pending(self, limit=16):
        return self.jobs[:limit]

    def queue_state(self):
        return len(self.jobs), 0.0

    def claim(self, job_id, worker_id, lease_seconds):
        self.claimed.append(job_id)
        return next(job for job in self.jobs if job.job_id == job_id)

    def get(self, job_id):
        return next(job for job in self.jobs if job.job_id == job_id)

    def heartbeat(self, *args, **kwargs):
        return None

    def complete(self, job_id, *args, **kwargs):
        self.completed.append(job_id)

    def fail(self, *args, **kwargs):
        raise AssertionError("the fairness fixture should not fail a job")


def _document_worker(store, pipeline):
    worker = object.__new__(HardwareWorker)
    worker.worker_id = "worker-test"
    worker.document_jobs = store
    worker.pipeline = pipeline
    worker.auth = SimpleNamespace(
        get_user_by_username=lambda _value: SimpleNamespace(user_id="user-1", is_active=True),
        get_user_by_id=lambda _value: None,
    )
    return worker


def test_document_batch_time_budget_does_not_claim_another_job_after_a_long_job(monkeypatch):
    from src.workers import main as worker_module

    store = _DocumentJobStore()
    processed: list[str] = []

    class _Pipeline:
        def continue_knowledge_base_document_generation(self, _ctx, work_order_id, should_cancel=None):
            processed.append(work_order_id)
            time.sleep(0.02)
            return SimpleNamespace(artifact_id=None, stage="completed")

    worker = _document_worker(store, _Pipeline())
    monkeypatch.setattr(worker_module, "build_context_for_user", lambda *_args, **_kwargs: SimpleNamespace(metadata={}))
    monkeypatch.setattr(worker_module, "record_worker", lambda **_kwargs: None)
    monkeypatch.setattr(worker_module, "heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_module, "set_queue_state", lambda *args, **kwargs: None)

    assert worker._process_document_authoring_jobs(limit=4, time_budget_seconds=0.005) is True
    assert processed == ["wo-1"]
    assert store.claimed == ["job-1"]
    assert store.completed == ["job-1"]


def test_run_once_uses_shared_pipeline_for_general_chat_after_document_batch(monkeypatch):
    from src.workers import main as worker_module

    worker = object.__new__(HardwareWorker)
    worker.worker_id = "worker-test"
    worker.pipeline = object()
    worker.runtime = None
    worker.auth = SimpleNamespace(
        get_user_by_id=lambda _value: SimpleNamespace(id=1, is_active=True),
    )
    turn = SimpleNamespace(id="turn-1", kb_name="")

    class _Conversations:
        def requeue_stale_turns(self):
            return None

        def pending_turn_queue_state(self):
            return 0, 0.0

        def list_pending_turn_work(self, limit=8):
            return [(turn, 1)]

    worker.conversations = _Conversations()
    worker._reload_runtime_settings_if_changed = lambda: None
    worker._process_document_authoring_jobs = lambda **_kwargs: True
    monkeypatch.setattr(worker_module, "build_context_for_user", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(worker_module, "heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_module, "set_queue_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_module, "record_worker", lambda **_kwargs: None)
    calls = []
    monkeypatch.setattr(
        worker_module,
        "_run_turn",
        lambda **kwargs: calls.append(kwargs),
    )

    assert worker.run_once() is True
    assert calls and calls[0]["pipeline"] is worker.pipeline
