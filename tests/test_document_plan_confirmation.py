"""Phase 1 Task 10: hash-bound plan confirmation with a durable submission outbox.

The confirmation transaction lives entirely in ``document_authoring.db``.  It
must accept the proposed OutputSpec/DocumentPlan, bind the versions to the
session/task, write append-only events and insert exactly one submission row.
The separate auth/job database is touched only by the later worker.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from src.document_authoring.generation_sessions import GenerationSessionStore
from src.document_authoring.models import (
    KnowledgeBaseSourceSnapshot,
    RendererPolicy,
    TemplateSecurityReport,
    TemplateVersion,
)
from src.document_authoring.planning.models import DocumentPlan, OutputSpec
from src.document_authoring.planning.store import DocumentPlanningStore
from src.document_authoring.tasks import DocumentTaskStore
from src.document_authoring.work_order_store import DocumentAuthoringStore

TENANT = "tenant-a"
USER = "user-a"
TASK_ID = "task-confirm-1"
SESSION_ID = "generation-session-confirm-1"
KB = "hardware"

TEMPLATE_HASH = hashlib.sha256(b"template-bytes").hexdigest()


def _save_template(store: DocumentAuthoringStore) -> TemplateVersion:
    store.save_renderer_policy(RendererPolicy(renderer_policy_id="renderer-1"))
    return store.save_template(
        TemplateVersion(
            template_version_id="tv-1",
            template_id="t-1",
            format="xlsx",
            content_hash=TEMPLATE_HASH,
            template_schema_id="ts-1",
            template_schema_version="1",
            renderer_policy_id="renderer-1",
            tenant_id=TENANT,
            knowledge_base_name=KB,
            status="approved",
        ),
        b"template-bytes",
        TemplateSecurityReport(
            report_id="rep-1", content_hash=TEMPLATE_HASH, format="xlsx"
        ),
    )


def _save_snapshot(store: DocumentAuthoringStore) -> KnowledgeBaseSourceSnapshot:
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id=TENANT,
        knowledge_base_name=KB,
        source_names=["spec.pdf"],
        created_by=USER,
    )
    return store.create_knowledge_base_source_snapshot(snapshot)


def _plan_payload(spec: OutputSpec, snapshot) -> dict:
    return {
        "document_plan_id": "plan:spec-confirm",
        "version": 1,
        "output_spec_id": spec.output_spec_id,
        "output_spec_version": spec.version,
        "output_spec_hash": spec.content_hash,
        "output_spec_summary": {
            "template_content_hash": TEMPLATE_HASH,
            "source_scope_hash": snapshot.content_hash,
        },
        "source_snapshot_id": snapshot.source_set_snapshot_id,
        "source_snapshot_hash": snapshot.content_hash,
        "domain_strategy_id": "generic_report",
        "domain_strategy_version": "1",
        "layout_adapter_id": "provided_template",
        "layout_adapter_version": "1",
        "renderer_capability_id": "xlsx",
        "renderer_capability_version": "1",
        "layout_contract": {
            "kind": "template",
            "template_version_id": "tv-1",
            "template_schema_id": "ts-1",
            "template_schema_version": "1",
        },
        "semantic_units": [{"unit_id": "summary", "kind": "section"}],
        "coverage_contract": {
            "requirements": [
                {"requirement_id": "summary", "unit_id": "summary", "kind": "section"}
            ]
        },
        "unit_tasks": [{
            "task_id": "unit-task:summary", "unit_id": "summary", "plan_version": 1,
            "action_key": "document-plan:summary:v1",
        }],
    }


def _confirmable(tmp_path, *, plan_overrides: dict | None = None):
    """Create a fully proposed, template-backed, confirmable state."""
    db = str(tmp_path / "authoring.db")
    store = DocumentAuthoringStore(db, str(tmp_path / "files"))
    template = _save_template(store)
    snapshot = _save_snapshot(store)

    spec = OutputSpec.model_validate({
        "output_spec_id": "spec-confirm",
        "version": 1,
        "status": "proposed",
        "purpose": "生成接口文档",
        "document_type": "report",
        "artifact": {"deliverables": [
            {"format": "xlsx", "role": "primary", "required": True}
        ]},
        "layout_source": {
            "mode": "provided_template",
            "template_version_id": "tv-1",
            "template_schema_id": "ts-1",
            "template_schema_version": "1",
        },
        "outline": [{"unit_id": "summary", "kind": "section", "required": True}],
        "language": "zh-CN",
        "approval_policy_id": "default-document-v1",
    })
    plan_data = _plan_payload(spec, snapshot)
    if callable(plan_overrides):
        plan_overrides(plan_data, snapshot)
    elif plan_overrides:
        plan_data.update(plan_overrides)
    plan = DocumentPlan.model_validate(plan_data)

    planning = DocumentPlanningStore(db)
    planning.create_output_spec(spec, tenant_id=TENANT, user_id=USER, task_id=TASK_ID)
    planning.create_plan(plan, tenant_id=TENANT, user_id=USER, task_id=TASK_ID)

    tasks = DocumentTaskStore(db)
    task = tasks.create_task(
        tenant_id=TENANT,
        user_id=USER,
        origin="api",
        created_by=USER,
        knowledge_base_name=KB,
        template_version_id="tv-1",
        generation_session_id=SESSION_ID,
        status="awaiting_plan_confirmation",
    )

    sessions = GenerationSessionStore(db)
    from src.document_authoring.generation_sessions import GenerationSession
    session = GenerationSession(
        session_id=SESSION_ID,
        tenant_id=TENANT,
        user_id=USER,
        knowledge_base_name=KB,
        template_version_id="tv-1",
        contract_version="output_spec_v1",
        status="awaiting_plan_confirmation",
        document_task_id=task.task_id,
        output_spec_id=spec.output_spec_id,
        output_spec_version=spec.version,
        document_plan_id=plan.document_plan_id,
        document_plan_version=plan.version,
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO document_generation_sessions (
                   session_id, tenant_id, user_id, knowledge_base_name,
                   template_version_id, contract_version, output_spec_id,
                   output_spec_version, output_spec_draft_json,
                   document_plan_id, document_plan_version,
                   status, created_at, updated_at, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id, session.tenant_id, session.user_id,
                session.knowledge_base_name, session.template_version_id,
                session.contract_version, session.output_spec_id,
                session.output_spec_version, None,
                session.document_plan_id, session.document_plan_version,
                session.status, session.created_at.isoformat(),
                session.updated_at.isoformat(),
                json.dumps(
                    session.model_dump(mode="json", exclude={"messages"}),
                ),
            ),
        )

    return SimpleNamespace(
        store=store,
        planning=planning,
        sessions=sessions,
        tasks=tasks,
        task=task,
        spec=spec,
        plan=plan,
        template=template,
        snapshot=snapshot,
        db=db,
    )


def _confirm(env, **overrides):
    return env.planning.confirm_submission(
        session_id=SESSION_ID,
        tenant_id=TENANT,
        user_id=USER,
        expected_output_spec_hash=overrides.pop(
            "expected_output_spec_hash", env.spec.content_hash
        ),
        expected_plan_hash=overrides.pop("expected_plan_hash", env.plan.plan_hash),
        client_request_id=overrides.pop("client_request_id", "confirm-request-1"),
        **overrides,
    )


def test_confirmation_accepts_and_enqueues_exactly_one_submission(tmp_path):
    env = _confirmable(tmp_path)

    submission = _confirm(env)

    assert submission.status == "pending"
    assert submission.document_plan_hash == env.plan.plan_hash
    assert submission.output_spec_hash == env.spec.content_hash
    assert submission.knowledge_base_name == KB
    assert submission.task_id == env.task.task_id

    assert env.planning.get_output_spec("spec-confirm", 1, tenant_id=TENANT, user_id=USER).status == "accepted"
    accepted_plan = env.planning.get_plan("plan:spec-confirm", 1, tenant_id=TENANT, user_id=USER)
    assert accepted_plan.status == "accepted"
    assert env.sessions.get_session(SESSION_ID).status == "planned"
    assert env.sessions.get_session(SESSION_ID).document_plan_id == "plan:spec-confirm"
    task = env.tasks.get(env.task.task_id)
    assert task.status == "planned"
    assert task.document_plan_id == "plan:spec-confirm"

    # One outbox row and one planning event, exactly.
    with sqlite3.connect(env.db) as conn:
        rows = conn.execute("SELECT * FROM document_plan_submissions").fetchall()
        assert len(rows) == 1
        events = conn.execute(
            "SELECT * FROM document_planning_events WHERE event_type = 'document_plan_confirmed'"
        ).fetchall()
        assert len(events) == 1
    # No auth.db job exists yet: the submission is pending worker dispatch.
    assert submission.job_id is None


def test_confirmation_replays_are_idempotent(tmp_path):
    env = _confirmable(tmp_path)

    first = _confirm(env, client_request_id="request-a")
    replay_same_key = _confirm(env, client_request_id="request-a")
    replay_other_key = _confirm(env, client_request_id="request-b")

    assert replay_same_key.submission_id == first.submission_id
    assert replay_other_key.submission_id == first.submission_id
    with sqlite3.connect(env.db) as conn:
        rows = conn.execute("SELECT * FROM document_plan_submissions").fetchall()
        assert len(rows) == 1


def test_confirmation_rejects_wrong_or_stale_hashes(tmp_path):
    env = _confirmable(tmp_path)

    with pytest.raises(ValueError, match="output spec hash|plan hash"):
        _confirm(env, expected_output_spec_hash="sha256:wrong")
    with pytest.raises(ValueError, match="plan hash|expected plan hash"):
        _confirm(env, expected_plan_hash="sha256:wrong")

    # Nothing was accepted or enqueued by the failed attempts.
    assert env.planning.get_output_spec("spec-confirm", 1, tenant_id=TENANT, user_id=USER).status == "proposed"
    assert env.planning.get_plan("plan:spec-confirm", 1, tenant_id=TENANT, user_id=USER).status == "proposed"
    assert env.sessions.get_session(SESSION_ID).status == "awaiting_plan_confirmation"
    with sqlite3.connect(env.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM document_plan_submissions").fetchone()[0] == 0


def test_confirmation_requires_v2_session(tmp_path):
    env = _confirmable(tmp_path)
    with sqlite3.connect(env.db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM document_generation_sessions WHERE session_id = ?",
            (SESSION_ID,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["contract_version"] = "legacy_brief_v1"
        conn.execute(
            "UPDATE document_generation_sessions SET contract_version = 'legacy_brief_v1',"
            " payload_json = ? WHERE session_id = ?",
            (json.dumps(payload), SESSION_ID),
        )

    with pytest.raises(ValueError, match="output_spec_v1"):
        _confirm(env)


def test_confirmation_rejects_template_free_plans_in_phase_one(tmp_path):
    env = _confirmable(tmp_path, plan_overrides={
        "layout_contract": {
            "kind": "structure",
            "structure_profile_id": "generic-report",
            "structure_profile_version": "1",
        },
        "layout_adapter_id": "generated_structure",
    })

    with pytest.raises(ValueError, match="template-free|not confirmable"):
        _confirm(env)
    assert env.planning.get_plan("plan:spec-confirm", 1, tenant_id=TENANT, user_id=USER).status == "proposed"


def test_confirmation_rejects_when_template_content_changed(tmp_path):
    def stale_template(plan_data, snapshot):
        plan_data["output_spec_summary"] = {
            "template_content_hash": "sha256:stale-template",
            "source_scope_hash": snapshot.content_hash,
        }

    env = _confirmable(tmp_path, plan_overrides=stale_template)

    with pytest.raises(ValueError, match="template"):
        _confirm(env)


def test_confirmation_rolls_back_when_outbox_insert_fails(tmp_path):
    env = _confirmable(tmp_path)
    original = env.planning.submissions.insert_or_get

    def boom(submission, *, connection=None):
        raise RuntimeError("disk full")

    env.planning.submissions.insert_or_get = boom
    try:
        with pytest.raises(RuntimeError):
            _confirm(env)
    finally:
        env.planning.submissions.insert_or_get = original

    # The whole acceptance transaction rolled back.
    assert env.planning.get_output_spec("spec-confirm", 1, tenant_id=TENANT, user_id=USER).status == "proposed"
    assert env.planning.get_plan("plan:spec-confirm", 1, tenant_id=TENANT, user_id=USER).status == "proposed"
    assert env.sessions.get_session(SESSION_ID).status == "awaiting_plan_confirmation"
    with sqlite3.connect(env.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM document_plan_submissions").fetchone()[0] == 0


def test_concurrent_confirmations_produce_one_submission(tmp_path):
    env = _confirmable(tmp_path)
    results: list = []
    errors: list = []

    def worker(request_key: str):
        try:
            results.append(_confirm(env, client_request_id=request_key))
        except Exception as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"request-{index}",))
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len({submission.submission_id for submission in results}) == 1
    with sqlite3.connect(env.db) as conn:
        rows = conn.execute("SELECT * FROM document_plan_submissions").fetchall()
        assert len(rows) == 1
        assert conn.execute("SELECT COUNT(*) FROM document_planning_events WHERE event_type = 'document_plan_confirmed'").fetchone()[0] == 1
