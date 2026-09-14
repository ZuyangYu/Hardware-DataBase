"""Phase 1 Task 10: durable submission outbox dispatch and worker materialization.

The worker drains confirmed plan submissions before normal document jobs.  It
rebuilds the auth context, revalidates permissions, materializes exactly one
template-backed Work Order from the accepted plan's frozen snapshot (v3
fingerprint) and creates the ``generate_work_order`` job only when preflight
is ready.  Preflight human gates park the submission in ``waiting_human``.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.app_pipeline import AppPipeline
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import (
    DocumentSchema,
)
from src.document_authoring.service import DocumentGenerationService
from src.pipelines.document_rag.schemas import RequestContext
from src.workers.main import HardwareWorker
from tests.test_document_plan_confirmation import (
    KB,
    TENANT,
    USER,
    _confirm,
    _confirmable,
)


@pytest.fixture
def ctx():
    return RequestContext(
        user_id=USER,
        tenant_id=TENANT,
        metadata={"department_id": "hw"},
        kb_permissions={f"hw:{KB}": "write"},
    )


def _service_env(tmp_path: Path):
    env = _confirmable(tmp_path)
    service = DocumentGenerationService(store=env.store)
    # The confirmation fixture wrote the template directly through the
    # store; register the matching approved document schema (the template
    # binds template_schema_id "ts-1") so the frozen Work Order can resolve
    # it.
    service.register_document_schema(
        DocumentSchema(
            document_schema_id="ts-1",
            version="1",
            document_type="knowledge-base-summary",
            status="approved",
            execution_mode="deterministic_only",
        )
    )
    return env, service


def _pipeline(env, service, job_store) -> AppPipeline:
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = service
    pipeline.document_job_store = job_store
    pipeline.backend = Mock()
    pipeline.spreadsheet_service = None
    pipeline._icd_template_profile = lambda order: None
    pipeline._icd_connector_scope_schema = lambda order: None
    pipeline._icd_front_view_connector_refdes = lambda order: []
    pipeline._document_requirement_resolution_snapshot = Mock(
        return_value={"unresolved_requirements": [], "resolved_fields": {}}
    )
    return pipeline


def test_dispatch_materializes_exactly_one_plan_backed_work_order_and_job(tmp_path, ctx):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)

    result = pipeline.dispatch_confirmed_document_plan(ctx, submission)

    assert result["status"] == "dispatched"
    work_order_id = result["work_order_id"]
    order = service.store.get_work_order(work_order_id)
    assert order is not None
    assert order.scope_type == "knowledge_base"
    assert order.source_set_snapshot_id == env.snapshot.source_set_snapshot_id
    # v3 planning references are frozen into the Work Order.
    assert order.output_spec_id == "spec-confirm"
    assert order.output_spec_hash == env.spec.content_hash
    assert order.document_plan_id == "plan:spec-confirm"
    assert order.document_plan_hash == env.plan.plan_hash
    assert order.input_fingerprint_version == 3

    # Exactly one generate job exists for the work order.
    job = job_store.get_by_work_order(work_order_id)
    assert job is not None and job.operation == "generate_work_order"
    assert result["job_id"] == job.job_id

    # A retry dispatch (worker crash before mark_dispatched) must not fork.
    replay = pipeline.dispatch_confirmed_document_plan(ctx, submission)
    assert replay["work_order_id"] == work_order_id
    assert replay["job_id"] == result["job_id"]
    assert job_store.get_by_work_order(work_order_id) is not None
    assert len(service.store.list_work_orders_for_knowledge_base(TENANT, KB)) == 1


def test_dispatch_parks_on_preflight_human_gate_without_job(tmp_path, ctx):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)
    pipeline._icd_template_profile = lambda order: SimpleNamespace(
        kind="icd_sample", issues=[], connector_blocks=[]
    )

    result = pipeline.dispatch_confirmed_document_plan(ctx, submission)

    assert result["status"] == "waiting_human"
    assert result["work_order_id"]
    assert "job_id" not in result
    assert job_store.get(result.get("job_id") or "") is None
    # The Work Order itself exists so a human can resolve it in the workbench.
    assert service.store.get_work_order(result["work_order_id"]) is not None


def test_dispatch_rejects_without_live_kb_permission(tmp_path):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)
    revoked = RequestContext(
        user_id=USER,
        tenant_id=TENANT,
        metadata={"department_id": "hw"},
        kb_permissions={},
    )

    with pytest.raises(PermissionError):
        pipeline.dispatch_confirmed_document_plan(revoked, submission)


def test_dispatch_rejects_missing_or_changed_snapshot(tmp_path, ctx):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)

    from src.document_authoring.planning.submissions import DocumentPlanSubmission

    tampered = DocumentPlanSubmission(
        **{
            **submission.__dict__,
            "source_snapshot_hash": "sha256:changed",
        }
    )
    with pytest.raises(ValueError, match="snapshot"):
        pipeline.dispatch_confirmed_document_plan(ctx, tampered)


def _worker(tmp_path, pipeline, submissions, worker_id: str, monkeypatch, *, kb_permissions=None) -> HardwareWorker:
    if kb_permissions is None:
        kb_permissions = {f"hw:{KB}": "write"}
    from src.pipelines.document_rag.schemas import RequestContext as _Ctx

    import src.workers.main as worker_module

    def _fake_context(user, kb_name, auth=None):
        return _Ctx(
            user_id=USER,
            tenant_id=TENANT,
            metadata={"department_id": "hw"},
            kb_permissions=dict(kb_permissions),
        )

    monkeypatch.setattr(worker_module, "build_context_for_user", _fake_context)
    worker = object.__new__(HardwareWorker)
    worker.running = True
    worker.worker_id = worker_id
    worker.auth = Mock()
    worker.pipeline = pipeline
    worker.document_jobs = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    worker.runtime = None
    worker._env_mtime_ns = -1
    worker._submission_store = submissions
    return worker


def test_worker_drains_confirmed_submissions_before_document_jobs(tmp_path, ctx, monkeypatch):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)
    submissions = env.planning.submissions

    worker = _worker(tmp_path, pipeline, submissions, "worker-1", monkeypatch)
    order = ["plan-submissions", "document-jobs"]
    real_plan_batch = HardwareWorker._process_document_plan_submissions

    def plan_batch(limit=2):
        order[0] = "ran"
        return real_plan_batch(worker, limit=limit)

    def document_batch(limit=4, time_budget_seconds=None):
        assert order[0] == "ran", "plan submissions must drain first"
        return False

    worker._process_document_plan_submissions = plan_batch  # type: ignore[method-assign]
    worker._process_document_authoring_jobs = document_batch  # type: ignore[method-assign]
    worker.conversations = Mock()
    worker.conversations.pending_turn_queue_state.return_value = (0, 0.0)
    worker.conversations.list_pending_turn_work.return_value = []
    worker.conversations.requeue_stale_turns = Mock()
    worker._reload_runtime_settings_if_changed = Mock()  # type: ignore[method-assign]

    assert worker.run_once() is True
    drained = submissions.get(submission.submission_id)
    assert drained.status == "dispatched"
    assert drained.work_order_id
    assert drained.job_id


def test_worker_marks_waiting_human_submission(tmp_path, ctx, monkeypatch):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)
    pipeline._icd_template_profile = lambda order: SimpleNamespace(
        kind="icd_sample", issues=[], connector_blocks=[]
    )
    submissions = env.planning.submissions

    user = Mock()
    user.is_active = True
    worker = _worker(tmp_path, pipeline, submissions, "worker-1", monkeypatch)
    worker.auth.get_user_by_username.return_value = user
    worker.auth.get_user_by_id.side_effect = TypeError

    assert worker._process_document_plan_submissions(limit=2) is True
    drained = submissions.get(submission.submission_id)
    assert drained.status == "waiting_human"
    assert drained.work_order_id
    assert drained.job_id is None


def test_worker_fails_non_retryable_on_permission_error(tmp_path, monkeypatch):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)
    submissions = env.planning.submissions

    user = Mock()
    user.is_active = True
    worker = _worker(
        tmp_path, pipeline, submissions, "worker-1", monkeypatch,
        kb_permissions={},
    )
    worker.auth.get_user_by_username.return_value = user
    worker.auth.get_user_by_id.side_effect = TypeError

    assert worker._process_document_plan_submissions(limit=2) is True
    drained = submissions.get(submission.submission_id)
    assert drained.status == "failed"
    assert "permission" in drained.last_error


def test_worker_retries_retryable_failures_then_dead_letters(tmp_path, monkeypatch):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)
    attempts = {"count": 0}

    def flaky_dispatch(ctx, sub):
        attempts["count"] += 1
        raise RuntimeError("retriever unavailable")

    pipeline.dispatch_confirmed_document_plan = flaky_dispatch
    submissions = env.planning.submissions

    user = Mock()
    user.is_active = True
    worker = _worker(tmp_path, pipeline, submissions, "worker-1", monkeypatch)
    worker.auth.get_user_by_username.return_value = user
    worker.auth.get_user_by_id.side_effect = TypeError

    for _ in range(5):
        worker._process_document_plan_submissions(limit=2)
        if submissions.get(submission.submission_id).status == "dead_letter":
            break
        time.sleep(1.1)  # retry backoff is 1s for the first attempt

    drained = submissions.get(submission.submission_id)
    assert drained.status == "dead_letter"
    assert attempts["count"] == 3


def test_worker_adopts_expired_lease_after_crash(tmp_path, ctx, monkeypatch):
    env, service = _service_env(tmp_path)
    submission = _confirm(env)
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = _pipeline(env, service, job_store)
    submissions = env.planning.submissions

    # Worker-1 claims then dies without dispatching; its lease expires.
    claimed = submissions.claim(submission.submission_id, "worker-1", lease_seconds=5)
    assert claimed is not None
    import sqlite3
    from datetime import timedelta

    from src.document_authoring.planning.submissions import _now

    expired = _now() - timedelta(seconds=30)
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "UPDATE document_plan_submissions SET lease_expires_at = ?"
            " WHERE submission_id = ?",
            (expired.isoformat(), submission.submission_id),
        )

    user = Mock()
    user.is_active = True
    worker = _worker(tmp_path, pipeline, submissions, "worker-2", monkeypatch)
    worker.auth.get_user_by_username.return_value = user
    worker.auth.get_user_by_id.side_effect = TypeError

    assert worker._process_document_plan_submissions(limit=2) is True
    drained = submissions.get(submission.submission_id)
    assert drained.status == "dispatched"
    assert drained.lease_owner is None
