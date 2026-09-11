from __future__ import annotations

from types import SimpleNamespace

from src.core.app_pipeline import AppPipeline
from src.document_authoring.generation_sessions import GenerationSessionStore
from src.document_authoring.tasks import DocumentTaskStore
from src.pipelines.document_rag.schemas import RequestContext


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write", "1:other": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )


def test_document_task_projection_contains_clarification_and_conversation_refs(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    session_store = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = session_store.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        conversation_id="17",
        initiating_turn_id="turn-a",
    )
    session_store.append_message(
        session.session_id,
        role="assistant",
        content="请选择资料版本",
        question_id="scope.revision",
        options=["当前发布版本", "最新上传版本"],
    )
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="chat",
        created_by="user-a",
        conversation_id="17",
        initiating_turn_id="turn-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        generation_session_id=session.session_id,
        status="needs_clarification",
    )
    session_store.bind_document_task(session.session_id, task.task_id)

    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(generation_sessions=session_store),
    )

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["task_id"] == task.task_id
    assert projection["status"] == "needs_clarification"
    assert projection["lifecycle_phase"] == "needs_input"
    assert projection["conversation_refs"] == {
        "conversation_id": "17",
        "initiating_turn_id": "turn-a",
    }
    assert projection["clarification_state"] == {
        "session_id": session.session_id,
        "status": "needs_clarification",
        "contract_version": "legacy_brief_v1",
        "last_question_id": "scope.revision",
        "clarification_revision": 0,
        "pending_question": {
            "question_id": "scope.revision",
            "content": "请选择资料版本",
            "options": ["当前发布版本", "最新上传版本"],
            "reason": None,
        },
    }


def test_output_spec_projection_does_not_resurface_answered_question(tmp_path):
    """Only the latest unanswered intake question is exposed to chat."""
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    session_store = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = session_store.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        contract_version="output_spec_v1",
        status="awaiting_plan",
        output_spec_id="spec-a",
        output_spec_version=1,
        output_spec_draft={"version": 1},
    )
    session_store.append_message(
        session.session_id,
        role="assistant",
        content="是否采用推荐的生成与审核策略？",
        question_id="recommendations",
        options=["采用推荐方案", "逐项设置"],
    )
    session_store.append_message(
        session.session_id,
        role="user",
        content="采用推荐方案",
        question_id="recommendations",
        answer="采用推荐方案",
    )
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="chat",
        created_by="user-a",
        conversation_id="17",
        initiating_turn_id="turn-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        generation_session_id=session.session_id,
        status="planned",
        output_spec_id="spec-a",
        output_spec_version=1,
    )
    task_store.attach_work_order(task.task_id, "wo-a")

    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(
            generation_sessions=session_store,
            get_work_order=lambda _work_order_id: None,
        ),
    )

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["clarification_state"]["status"] == "awaiting_plan"
    assert projection["clarification_state"]["pending_question"] is None


def test_document_task_projection_reconciles_work_order_and_artifact_history(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="running",
    )
    task_store.attach_work_order(task.task_id, "wo-a")
    task_store.attach_run(task.task_id, "run-a")
    task_store.attach_artifact(task.task_id, "artifact-a")

    order = SimpleNamespace(
        work_order_id="wo-a",
        task_id=task.task_id,
        generation_session_id=None,
        knowledge_base_name="hardware",
        status="drafting",
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(
            get_work_order=lambda work_order_id: order if work_order_id == "wo-a" else None,
            list_artifacts=lambda _work_order_id: [
                SimpleNamespace(
                    artifact_id="artifact-a",
                    run_id="run-a",
                    stage="draft_preview",
                    output_format="xlsx",
                    parent_artifact_id=None,
                    validity_status="current",
                    policy_status="active",
                    validation_report_id="report-a",
                )
            ],
        ),
    )
    pipeline.get_document_run_status = lambda _work_order_id, _ctx: {
        "work_order_id": "wo-a",
        "task_id": task.task_id,
        "status": "drafting",
        "phase": "generating",
        "next_actions": ["poll_status"],
        "harness_run": {"run_id": "run-a", "status": "running"},
        "artifacts": [{"artifact_id": "artifact-a", "stage": "draft_preview"}],
    }

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["work_order_id"] == "wo-a"
    assert projection["current_run_id"] == "run-a"
    assert projection["current_artifact_id"] == "artifact-a"
    assert projection["work_order"]["status"] == "drafting"
    assert projection["artifacts"] == [{
        "artifact_id": "artifact-a",
        "run_id": "run-a",
        "stage": "draft_preview",
        "output_format": "xlsx",
        "parent_artifact_id": None,
        "validity_status": "current",
        "policy_status": "active",
        "validation_report_id": "report-a",
    }]


def test_blocking_validation_artifact_is_not_user_visible():
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        store=SimpleNamespace(
            get_validation_report=lambda _report_id: SimpleNamespace(
                status="requires_human",
                issues=[{"code": "draft_unit_mismatch", "blocking": True}],
            ),
        ),
    )
    artifact = SimpleNamespace(
        artifact_id="artifact-blocked",
        stage="approved_release",
        validation_report_id="report-blocked",
    )

    assert pipeline._document_artifact_is_user_visible(artifact) is False


def test_document_task_projection_surfaces_failed_execution_job_over_stale_queued_task(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="chat",
        created_by="user-a",
        conversation_id="17",
        initiating_turn_id="turn-failed",
        knowledge_base_name="hardware",
        status="queued",
    )
    task_store.attach_work_order(task.task_id, "wo-failed")
    order = SimpleNamespace(
        work_order_id="wo-failed",
        task_id=task.task_id,
        generation_session_id=None,
        knowledge_base_name="hardware",
        status="planned",
        target_format="xlsx",
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(
            get_work_order=lambda work_order_id: order if work_order_id == "wo-failed" else None,
            list_artifacts=lambda _work_order_id: [],
        ),
    )
    pipeline.get_document_run_status = lambda _work_order_id, _ctx: {
        "work_order_id": "wo-failed",
        "task_id": task.task_id,
        "status": "planned",
        "phase": "planned",
        "next_actions": [],
        "job": {
            "job_id": "job-failed",
            "status": "failed",
            "attempt": 1,
            "last_error": "plan execution scope is not present in the configured allowlist",
        },
        "artifacts": [],
    }

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["status"] == "failed"
    assert projection["lifecycle_phase"] == "failed"
    assert projection["error_code"] == "document_job_failed"
    assert projection["error_message"] == (
        "plan execution scope is not present in the configured allowlist"
    )


def test_document_task_projection_preserves_release_block_over_pending_review(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="chat",
        created_by="user-a",
        conversation_id="85",
        initiating_turn_id="turn-review",
        knowledge_base_name="hardware",
        status="failed",
    )
    task_store.attach_work_order(task.task_id, "wo-review")
    order = SimpleNamespace(
        work_order_id="wo-review",
        task_id=task.task_id,
        generation_session_id=None,
        knowledge_base_name="hardware",
        status="blocked",
        target_format="xlsx",
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(
            get_work_order=lambda work_order_id: order if work_order_id == "wo-review" else None,
            list_artifacts=lambda _work_order_id: [],
        ),
    )
    pipeline.get_document_run_status = lambda _work_order_id, _ctx: {
        "status": "blocked",
        "phase": "blocked",
        "error_code": "plan_release_blocked",
        "error_message": "release is blocked by deterministic review gates",
        "next_actions": [],
        "job": {"status": "succeeded", "last_error": ""},
    }
    pipeline._document_reviews_for_task = lambda _ctx, _task, _order: [{
        "review_id": "review-a",
        "review_kind": "artifact_approval:artifact-a",
        "status": "pending",
    }]

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["status"] == "blocked"
    assert projection["lifecycle_phase"] == "blocked"
    assert projection["next_actions"] == ["view_error", "provide_value"]
    assert projection["error_code"] == "plan_release_blocked"
    assert projection["error_message"] == "release is blocked by deterministic review gates"


def test_document_task_projection_allows_pending_review_for_non_blocked_candidate(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a", user_id="user-a", origin="chat", created_by="user-a",
        conversation_id="86", initiating_turn_id="turn-review", knowledge_base_name="hardware",
        status="waiting_human",
    )
    task_store.attach_work_order(task.task_id, "wo-review-ok")
    order = SimpleNamespace(
        work_order_id="wo-review-ok", task_id=task.task_id, generation_session_id=None,
        knowledge_base_name="hardware", status="waiting_human_approval", target_format="xlsx",
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(
            get_work_order=lambda work_order_id: order if work_order_id == "wo-review-ok" else None,
            list_artifacts=lambda _work_order_id: [],
        ),
    )
    pipeline.get_document_run_status = lambda _work_order_id, _ctx: {
        "status": "waiting_human_approval", "phase": "waiting_human_approval",
        "next_actions": [], "job": {"status": "succeeded"},
    }
    pipeline._document_reviews_for_task = lambda _ctx, _task, _order: [{
        "review_id": "review-ok", "review_kind": "artifact_approval:artifact-a", "status": "pending",
    }]

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["status"] == "needs_review"
    assert projection["next_actions"] == ["review_document", "open_document_workbench"]


def test_document_task_projection_prefers_pending_conversation_question_over_review(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a", user_id="user-a", origin="chat", created_by="user-a",
        conversation_id="87", initiating_turn_id="turn-clarify", knowledge_base_name="hardware",
        status="needs_clarification",
    )
    task_store.attach_work_order(task.task_id, "wo-clarify")
    order = SimpleNamespace(
        work_order_id="wo-clarify", task_id=task.task_id, generation_session_id="gs-clarify",
        knowledge_base_name="hardware", status="blocked", target_format="xlsx",
    )
    clarification = {
        "session_id": "gs-clarify",
        "status": "needs_clarification",
        "contract_version": "output_spec_v1",
        "pending_question": {
            "question_id": "missing_data_resolution",
            "content": "知识库中暂未找到必填字段的可靠资料，请选择处理方式。",
            "options": ["标记为未提供，继续生成", "补充说明", "暂停等待资料"],
            "reason": "required evidence is missing",
        },
    }
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(
            get_work_order=lambda work_order_id: order if work_order_id == "wo-clarify" else None,
            list_artifacts=lambda _work_order_id: [],
        ),
    )
    pipeline.get_document_run_status = lambda _work_order_id, _ctx: {
        "status": "blocked", "phase": "blocked", "error_code": "plan_release_blocked",
        "error_message": "release is blocked", "next_actions": ["view_error", "provide_value"],
        "job": {"status": "succeeded"},
    }
    pipeline._document_clarification_projection = lambda _ctx, _task: clarification
    pipeline._document_reviews_for_task = lambda _ctx, _task, _order: [{
        "review_id": "review-clarify", "review_kind": "artifact_approval:artifact-a", "status": "pending",
    }]

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["status"] == "needs_clarification"
    assert projection["lifecycle_phase"] == "needs_input"
    assert projection["next_actions"] == ["answer_clarification"]
    assert projection["clarification_state"]["pending_question"]["question_id"] == "missing_data_resolution"


def test_current_chat_projection_uses_the_conversation_pointer_not_updated_order(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    first = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="chat",
        created_by="user-a",
        conversation_id="17",
        initiating_turn_id="turn-1",
        knowledge_base_name="hardware",
        status="planned",
    )
    second = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="chat",
        created_by="user-a",
        conversation_id="17",
        initiating_turn_id="turn-2",
        knowledge_base_name="hardware",
        status="queued",
    )
    # A late update to the historical task must not steal the current pointer.
    task_store.update_status(first.task_id, "running")

    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(generation_sessions=None),
    )

    projection = pipeline.get_current_chat_document_task_projection(
        _ctx(),
        conversation_id="17",
        knowledge_base_name="hardware",
    )

    assert projection is not None
    assert projection["task_id"] == second.task_id
    assert projection["status"] == "queued"


def test_document_task_projection_prioritizes_pending_revision_review(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="waiting_human",
    )
    task_store.attach_work_order(task.task_id, "wo-revision")
    order = SimpleNamespace(
        work_order_id="wo-revision",
        task_id=task.task_id,
        generation_session_id=None,
        knowledge_base_name="hardware",
        status="complete",
        target_format="xlsx",
    )
    revision = SimpleNamespace(
        model_dump=lambda mode="json": {
            "revision_id": "revision-1",
            "task_id": task.task_id,
            "status": "planned",
            "impact_scope": {"kind": "fields", "fields": ["pcb_revision"]},
        },
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        revision_service=SimpleNamespace(list_revisions=lambda _ctx, _task_id: [revision]),
        store=SimpleNamespace(
            get_work_order=lambda work_order_id: order if work_order_id == order.work_order_id else None,
            list_artifacts=lambda _work_order_id: [],
        ),
    )
    pipeline.get_document_run_status = lambda _work_order_id, _ctx: {
        "work_order_id": order.work_order_id,
        "task_id": task.task_id,
        "status": "complete",
        "phase": "completed",
        "next_actions": ["view_result"],
        "artifacts": [],
    }

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["pending_review"]["revision_id"] == "revision-1"
    assert projection["next_actions"] == ["review_revision", "open_document_workbench"]


# ---------------------------------------------------------------------------
# Phase 1 Task 12: planning state projection and v2 lifecycle next actions
# ---------------------------------------------------------------------------


def _v2_pipeline(env):
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=env.tasks),
        store=SimpleNamespace(generation_sessions=env.sessions),
        planning=SimpleNamespace(store=env.store.planning),
    )
    return pipeline


def _projection_ctx():
    from src.pipelines.document_rag.schemas import RequestContext

    return RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "read"},
        metadata={"resource_department_id": 1},
    )


def test_projection_exposes_planning_state_and_submission_for_v2_task(tmp_path):
    from tests.test_document_plan_confirmation import _confirm, _confirmable

    env = _confirmable(tmp_path)
    submission = _confirm(env)
    pipeline = _v2_pipeline(env)

    projection = pipeline.get_document_task_projection(
        _projection_ctx(), env.task.task_id,
    )

    planning = projection["planning_state"]
    assert planning["output_spec_id"] == "spec-confirm"
    assert planning["output_spec_version"] == 1
    assert planning["output_spec_hash"] == env.spec.content_hash
    assert planning["document_plan_id"] == "plan:spec-confirm"
    assert planning["plan_hash"] == env.plan.plan_hash
    assert planning["proposal_status"] == "accepted"
    assert "purpose" not in planning  # safe projection: identifiers/hashes only

    assert projection["submission"] == {
        "submission_id": submission.submission_id,
        "status": "pending",
        "work_order_id": None,
        "job_id": None,
    }
    assert projection["status"] == "planned"
    assert "get_document_task_status" in projection["next_actions"]


def test_projection_projects_awaiting_plan_confirmation_next_actions(tmp_path):
    from tests.test_document_plan_confirmation import _confirmable

    env = _confirmable(tmp_path)
    pipeline = _v2_pipeline(env)

    projection = pipeline.get_document_task_projection(
        _projection_ctx(), env.task.task_id,
    )

    assert projection["status"] == "awaiting_plan_confirmation"
    assert projection["next_actions"] == [
        "propose_document_plan", "confirm_document_plan",
    ]
    assert projection["planning_state"]["proposal_status"] == "proposed"
    assert projection["submission"] is None


def test_projection_maps_internal_awaiting_plan_to_draft(tmp_path):
    import json
    import sqlite3

    from tests.test_document_plan_confirmation import _confirmable

    env = _confirmable(tmp_path)
    pipeline = _v2_pipeline(env)
    session = env.sessions.get_session(env.task.generation_session_id)
    raw = session.model_copy(update={
        "document_plan_id": None,
        "document_plan_version": None,
        "status": "awaiting_plan",
    })
    task_row = env.tasks.get(env.task.task_id)
    reset_task = task_row.model_copy(update={
        "status": "needs_clarification",
        "document_plan_id": None,
        "document_plan_version": None,
    })
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "UPDATE document_generation_sessions SET status = 'awaiting_plan',"
            " document_plan_id = NULL, document_plan_version = NULL,"
            " payload_json = ? WHERE session_id = ?",
            (
                json.dumps(raw.model_dump(mode="json", exclude={"messages"})),
                session.session_id,
            ),
        )
        conn.execute(
            "UPDATE document_tasks SET status = 'needs_clarification',"
            " document_plan_id = NULL, document_plan_version = NULL,"
            " payload_json = ? WHERE task_id = ?",
            (
                json.dumps(reset_task.model_dump(mode="json")),
                task_row.task_id,
            ),
        )

    projection = pipeline.get_document_task_projection(
        _projection_ctx(), env.task.task_id,
    )

    # The internal awaiting_plan state projects as a user-visible draft.
    assert projection["status"] == "draft"
    assert "propose_document_plan" in projection["next_actions"]
    assert projection["planning_state"] is None


def test_pending_icd_scope_review_routes_to_scope_resolution_not_resume(tmp_path):
    """A frozen-scope stop must never advertise ``resume_document_task``."""
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a", user_id="user-a", origin="chat", created_by="user-a",
        conversation_id="88", initiating_turn_id="turn-icd-scope", knowledge_base_name="hardware",
        status="planned",
    )
    task_store.attach_work_order(task.task_id, "wo-icd-scope")
    order = SimpleNamespace(
        work_order_id="wo-icd-scope", task_id=task.task_id, generation_session_id=None,
        knowledge_base_name="hardware", status="planned", target_format="xlsx",
    )
    scope_review = SimpleNamespace(
        status="pending",
        pending_count=1,
        exceptions=[SimpleNamespace(
            kind="connector_mapping_missing",
            refdes="X302",
            pin_name=None,
            recommended_action="check_edf_mapping",
            user_instruction="已确定接插件 X302，但当前冻结来源中未找到其 EDF 管脚映射。",
        )],
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        task_service=SimpleNamespace(store=task_store),
        store=SimpleNamespace(
            get_work_order=lambda work_order_id: order if work_order_id == "wo-icd-scope" else None,
            list_artifacts=lambda _work_order_id: [],
            get_icd_scope_review=lambda work_order_id: scope_review if work_order_id == "wo-icd-scope" else None,
        ),
    )
    pipeline.get_document_run_status = lambda _work_order_id, _ctx: {
        "status": "planned", "phase": "planned", "next_actions": [],
        "job": {"status": "succeeded"},
    }
    pipeline._document_reviews_for_task = lambda _ctx, _task, _order: [{
        "review_id": "review-icd-scope", "review_kind": "icd_scope", "status": "pending",
    }]

    projection = pipeline.get_document_task_projection(_ctx(), task.task_id)

    assert projection["status"] == "needs_review"
    assert projection["lifecycle_phase"] == "needs_review"
    assert projection["next_actions"] == [
        "submit_icd_scope_resolution",
        "open_document_workbench",
    ]
    assert "resume_document_task" not in projection["next_actions"]
    scope = projection["pending_review"]["scope_review"]
    assert scope["blocking"] is True
    assert scope["exceptions"][0]["refdes"] == "X302"
    assert "EDF" in scope["exceptions"][0]["user_instruction"]
