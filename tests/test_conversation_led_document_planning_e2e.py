"""Phase 0-1 closeout: conversation-led planning end-to-end gates.

Scenario 1 (happy path) exercises the full loop against real stores: a v2
session reaches plan confirmation, the submission outbox, exactly one
template-backed Work Order (v3 fingerprint) and one durable job.  A worker
restart drains nothing new.  Scenario 4 rejects a confirmation after the
frozen template changed.  The rollout flags must default to false.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from src.core.app_pipeline import AppPipeline
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import DocumentSchema
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


def _ctx(*, permission: str = "write") -> RequestContext:
    return RequestContext(
        user_id=USER,
        tenant_id=TENANT,
        metadata={"department_id": "hw"},
        kb_permissions={f"hw:{KB}": permission},
    )


def _real_stack(tmp_path: Path, *, chat_lineage: bool = False):
    env = _confirmable(
        tmp_path,
        **(
            {
                "conversation_id": "conversation-17",
                "initiating_turn_id": "turn-17",
            }
            if chat_lineage
            else {}
        ),
    )
    service = DocumentGenerationService(store=env.store)
    service.register_document_schema(
        DocumentSchema(
            document_schema_id="ts-1",
            version="1",
            document_type="knowledge-base-summary",
            status="approved",
            execution_mode="deterministic_only",
        )
    )
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = service
    pipeline.document_job_store = job_store
    pipeline.backend = object()
    pipeline.spreadsheet_service = None
    pipeline._icd_template_profile = lambda order: None
    pipeline._icd_connector_scope_schema = lambda order: None
    pipeline._icd_front_view_connector_refdes = lambda order: []
    pipeline._document_requirement_resolution_snapshot = lambda *_args, **_kwargs: {
        "unresolved_requirements": [],
        "resolved_fields": {},
    }
    return env, service, job_store, pipeline


def test_rollout_flags_default_false():
    env = os.environ.copy()
    env.pop("DOCUMENT_PLANNING_SHADOW_ENABLED", None)
    env.pop("DOCUMENT_PLANNING_V2_ENABLED", None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import src.settings as s; "
                "assert s.DOCUMENT_PLANNING_SHADOW_ENABLED is False; "
                "assert s.DOCUMENT_PLANNING_V2_ENABLED is False"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_e2e_confirm_dispatch_restart_stays_exactly_one(tmp_path):
    """Confirm → outbox → one Work Order/job; a restarted worker adds nothing."""
    env, service, job_store, pipeline = _real_stack(tmp_path)

    submission = _confirm(env)
    # The happy path mirrors the real worker context rebuild: the fake auth
    # returns an active user and the module-level context builder yields the
    # authorized scope (permission revocation is covered separately below).
    import src.workers.main as worker_module

    original_builder = worker_module.build_context_for_user
    worker_module.build_context_for_user = lambda user, kb_name, auth=None: _ctx()
    worker_one = object.__new__(HardwareWorker)
    worker_one.worker_id = "worker-e2e-1"
    worker_one.auth = _FakeAuth()
    worker_one.pipeline = pipeline
    worker_one.document_jobs = job_store
    worker_one.runtime = None
    worker_one._env_mtime_ns = -1
    worker_one._submission_store = env.planning.submissions

    try:
        assert worker_one._process_document_plan_submissions(limit=2) is True
    finally:
        worker_module.build_context_for_user = original_builder
    dispatched = env.planning.submissions.get(submission.submission_id)
    assert dispatched.status == "dispatched"
    assert dispatched.work_order_id and dispatched.job_id

    orders = service.store.list_work_orders_for_knowledge_base(TENANT, KB)
    assert len(orders) == 1
    order = orders[0]
    assert order.input_fingerprint_version == 3
    assert order.output_spec_hash == env.spec.content_hash
    assert order.document_plan_hash == env.plan.plan_hash
    assert job_store.get_by_work_order(order.work_order_id) is not None

    # Worker restart: the submission is terminal, the drain must be a no-op.
    worker_two = object.__new__(HardwareWorker)
    worker_two.worker_id = "worker-e2e-2"
    worker_two.auth = _FakeAuth()
    worker_two.pipeline = pipeline
    worker_two.document_jobs = job_store
    worker_two.runtime = None
    worker_two._env_mtime_ns = -1
    worker_two._submission_store = env.planning.submissions
    assert worker_two._process_document_plan_submissions(limit=2) is False
    assert len(service.store.list_work_orders_for_knowledge_base(TENANT, KB)) == 1


def test_confirmed_output_spec_is_frozen_into_the_materialized_work_order(tmp_path):
    """A worker must not discard the scope and policies confirmed in Chat."""
    env = _confirmable(
        tmp_path,
        spec_overrides={
            "document_type": "icd",
            "target_identity": {"value": "整个原理图中的模块"},
            "missing_data_policy": "mark_tbd",
            "inference_policy": "forbid",
        },
    )
    service = DocumentGenerationService(store=env.store)
    service.register_document_schema(
        DocumentSchema(
            document_schema_id="ts-1",
            version="1",
            document_type="icd",
            status="approved",
            execution_mode="deterministic_only",
        )
    )
    job_store = DocumentAuthoringJobStore(str(tmp_path / "auth.db"))
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = service
    pipeline.document_job_store = job_store
    pipeline.backend = object()
    pipeline.spreadsheet_service = None
    pipeline._icd_template_profile = lambda order: None
    pipeline._icd_connector_scope_schema = lambda order: None
    pipeline._icd_front_view_connector_refdes = lambda order: []
    pipeline._document_requirement_resolution_snapshot = lambda *_args, **_kwargs: {
        "unresolved_requirements": [],
        "resolved_fields": {},
    }

    submission = _confirm(env)
    result = pipeline.dispatch_confirmed_document_plan(_ctx(), submission)

    order = service.store.get_work_order(result["work_order_id"])
    assert order.generation_brief == {
        "confirmed": True,
        "purpose": "生成接口文档",
        "document_type": "icd",
        "target_identity": {"value": "整个原理图中的模块"},
        "missing_data_policy": "mark_tbd",
        "inference_policy": "forbid",
        "approval_policy_id": "default-document-v1",
        "language": "zh-CN",
        "additional_requirements": [],
    }


def test_e2e_chat_lineage_survives_worker_context_rebuild(tmp_path, monkeypatch):
    """A worker rebuild must preserve chat task identity before dispatch."""
    env, _service, job_store, pipeline = _real_stack(tmp_path, chat_lineage=True)
    submission = _confirm(env)

    import src.workers.main as worker_module

    # This is the ordinary worker context: it re-authorizes the live KB
    # permission but intentionally has no request-local chat metadata.
    monkeypatch.setattr(
        worker_module,
        "build_context_for_user",
        lambda user, kb_name, auth=None: _ctx(),
    )
    worker = object.__new__(HardwareWorker)
    worker.worker_id = "worker-chat-lineage"
    worker.auth = _FakeAuth()
    worker.pipeline = pipeline
    worker.document_jobs = job_store
    worker.runtime = None
    worker._env_mtime_ns = -1
    worker._submission_store = env.planning.submissions

    assert worker._process_document_plan_submissions(limit=2) is True

    dispatched = env.planning.submissions.get(submission.submission_id)
    assert dispatched.status == "dispatched"
    assert dispatched.work_order_id and dispatched.job_id
    task = env.tasks.get(env.task.task_id)
    assert task is not None
    assert task.origin == "chat"
    assert task.conversation_id == "conversation-17"
    assert task.initiating_turn_id == "turn-17"


def test_e2e_template_change_rejects_confirmation(tmp_path):
    """A changed frozen template fails the confirmation closed, with no WO."""
    import json
    import sqlite3

    env, service, job_store, _pipeline = _real_stack(tmp_path)
    with sqlite3.connect(env.db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM template_versions WHERE template_version_id = 'tv-1'",
        ).fetchone()
        payload = json.loads(row[0])
        payload["content_hash"] = "sha256:rotated-template"
        conn.execute(
            "UPDATE template_versions SET content_hash = 'sha256:rotated-template',"
            " payload_json = ? WHERE template_version_id = 'tv-1'",
            (json.dumps(payload),),
        )

    import pytest

    with pytest.raises(ValueError, match="template"):
        _confirm(env)
    assert not service.store.list_work_orders_for_knowledge_base(TENANT, KB)
    assert job_store.list_pending(limit=10) == []


class _FakeAuth:
    def get_user_by_username(self, username):
        user = type("U", (), {})()
        user.is_active = True
        user.username = username
        return user

    def get_user_by_id(self, _user_id):
        raise TypeError


def test_e2e_worker_rebuilds_context_and_revoked_permission_fails_closed(tmp_path):
    """Between confirmation and dispatch, revoking KB permission never runs a job."""
    env, service, job_store, pipeline = _real_stack(tmp_path)
    submission = _confirm(env)

    worker = object.__new__(HardwareWorker)
    worker.worker_id = "worker-e2e-revoke"
    worker.auth = _FakeAuth()
    worker.pipeline = pipeline
    worker.document_jobs = job_store
    worker.runtime = None
    worker._env_mtime_ns = -1
    worker._submission_store = env.planning.submissions

    import src.workers.main as worker_module

    original_builder = worker_module.build_context_for_user

    def revoked_context(user, kb_name, auth=None):
        return RequestContext(
            user_id=USER,
            tenant_id=TENANT,
            metadata={"department_id": "hw"},
            kb_permissions={},
        )

    worker_module.build_context_for_user = revoked_context
    try:
        assert worker._process_document_plan_submissions(limit=2) is True
    finally:
        worker_module.build_context_for_user = original_builder

    drained = env.planning.submissions.get(submission.submission_id)
    assert drained.status == "failed"
    assert "permission" in drained.last_error
    assert not service.store.list_work_orders_for_knowledge_base(TENANT, KB)
