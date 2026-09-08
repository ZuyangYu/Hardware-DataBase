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
    assert projection["conversation_refs"] == {
        "conversation_id": "17",
        "initiating_turn_id": "turn-a",
    }
    assert projection["clarification_state"] == {
        "session_id": session.session_id,
        "status": "needs_clarification",
        "last_question_id": "scope.revision",
        "clarification_revision": 0,
        "pending_question": {
            "question_id": "scope.revision",
            "content": "请选择资料版本",
            "options": ["当前发布版本", "最新上传版本"],
            "reason": None,
        },
    }


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
