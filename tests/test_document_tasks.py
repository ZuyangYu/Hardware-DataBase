from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import DocumentWorkOrder
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.tasks import DocumentTaskService, DocumentTaskStore
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext
from src.api.routes.query import _bind_document_task_trace


def _store(tmp_path) -> DocumentTaskStore:
    return DocumentTaskStore(str(tmp_path / "document-tasks.db"))


def _create(store: DocumentTaskStore, **updates):
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "origin": "chat",
        "created_by": "user-a",
        "conversation_id": "conversation-1",
        "initiating_turn_id": "turn-1",
        "knowledge_base_name": "hardware",
        "template_version_id": "template-1",
        "idempotency_key": "turn-1:document-generation",
    }
    values.update(updates)
    return store.create_task(**values)


def test_create_task_is_idempotent_and_keeps_chat_trace(tmp_path):
    store = _store(tmp_path)

    first = _create(store)
    replay = _create(store)

    assert replay.task_id == first.task_id
    assert replay.origin == "chat"
    assert replay.conversation_id == "conversation-1"
    assert replay.initiating_turn_id == "turn-1"

    with pytest.raises(ValueError, match="idempotency key conflicts"):
        _create(store, template_version_id="template-2")


def test_task_can_be_bound_to_only_one_work_order(tmp_path):
    store = _store(tmp_path)
    task = _create(store)

    bound = store.attach_work_order(task.task_id, "wo-1")
    replay = store.attach_work_order(task.task_id, "wo-1")

    assert bound.work_order_id == "wo-1"
    assert replay.task_id == task.task_id
    with pytest.raises(ValueError, match="already bound"):
        store.attach_work_order(task.task_id, "wo-2")


def test_restarting_a_work_order_advances_the_task_binding_with_an_expected_parent(tmp_path):
    store = _store(tmp_path)
    task = _create(store)
    store.attach_work_order(task.task_id, "wo-1")

    advanced = store.advance_work_order(
        task.task_id,
        "wo-2",
        expected_work_order_id="wo-1",
    )

    assert advanced.work_order_id == "wo-2"
    assert store.get(task.task_id).work_order_id == "wo-2"

    with pytest.raises(ValueError, match="expected work order"):
        store.advance_work_order(
            task.task_id,
            "wo-3",
            expected_work_order_id="wo-1",
        )


def test_task_tracks_the_current_run_and_all_artifact_versions(tmp_path):
    store = _store(tmp_path)
    task = _create(store)

    with_run = store.attach_run(task.task_id, "run-1")
    with_same_run = store.attach_run(task.task_id, "run-1")
    with_first_artifact = store.attach_artifact(task.task_id, "artifact-1")
    with_second_artifact = store.attach_artifact(task.task_id, "artifact-2")
    with_status = store.update_status(task.task_id, "running")

    assert with_run.current_run_id == "run-1"
    assert with_same_run.current_run_id == "run-1"
    assert with_first_artifact.current_artifact_id == "artifact-1"
    assert with_second_artifact.current_artifact_id == "artifact-2"
    assert with_second_artifact.artifact_ids == ["artifact-1", "artifact-2"]
    assert with_status.artifact_ids == ["artifact-1", "artifact-2"]
    assert with_status.status == "running"


def test_status_changes_are_recorded_as_idempotent_task_events(tmp_path):
    store = _store(tmp_path)
    task = _create(store)

    store.update_status(task.task_id, "queued")
    store.update_status(task.task_id, "queued")
    store.update_status(task.task_id, "running")

    events = store.list_events(task.task_id)
    assert [event["event_type"] for event in events] == [
        "status_changed",
        "status_changed",
    ]
    assert events[0]["payload"] == {"from": "planned", "to": "queued"}
    assert events[1]["payload"] == {"from": "queued", "to": "running"}


def test_chat_task_creation_atomically_advances_the_conversation_current_task(tmp_path):
    """Catches regressions where refresh chooses an older task by timestamp."""
    store = _store(tmp_path)
    first = _create(store)
    second = _create(
        store,
        initiating_turn_id="turn-2",
        idempotency_key="turn-2:document-generation",
    )

    current = store.get_current_chat_task(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        knowledge_base_name="hardware",
    )

    assert current is not None
    assert current.task_id == second.task_id
    state = store.get_conversation_document_state(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
    )
    assert state == {
        "current_task_id": second.task_id,
        "revision": 2,
    }

    # Replaying the first request is idempotent and must not reactivate it.
    assert _create(store).task_id == first.task_id
    assert store.get_current_chat_task(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
    ).task_id == second.task_id


def test_chat_task_stream_events_are_ordered_and_scoped_to_the_owner(tmp_path):
    """Catches missing/out-of-order event cursors and cross-owner leakage."""
    store = _store(tmp_path)
    task = _create(store)
    store.update_status(task.task_id, "queued")
    store.update_status(task.task_id, "running")

    events = store.list_conversation_stream_events(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        after_seq=0,
    )

    assert [event["event_type"] for event in events] == [
        "task_activated",
        "status_changed",
        "status_changed",
    ]
    assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
    assert events[-1]["payload"] == {"from": "queued", "to": "running"}
    assert store.list_conversation_stream_events(
        tenant_id="tenant-a",
        user_id="other-user",
        conversation_id="conversation-1",
        after_seq=0,
    ) == []


def test_projection_snapshots_are_deduplicated_and_keep_measurable_progress(tmp_path):
    store = _store(tmp_path)
    task = _create(store)
    first_projection = {
        "task_id": task.task_id,
        "status": "running",
        "progress": {"completed_units": 2, "total_units": 10},
    }

    first = store.append_projection_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        task_id=task.task_id,
        projection=first_projection,
    )
    duplicate = store.append_projection_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        task_id=task.task_id,
        projection=dict(first_projection),
    )
    second = store.append_projection_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        task_id=task.task_id,
        projection={
            **first_projection,
            "progress": {"completed_units": 3, "total_units": 10},
        },
    )

    assert first is not None
    assert duplicate is None
    assert second is not None
    assert second["seq"] > first["seq"]
    events = store.list_conversation_stream_events(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        after_seq=first["seq"],
    )
    assert events[-1]["event_type"] == "projection"
    assert events[-1]["payload"]["projection"]["progress"] == {
        "completed_units": 3,
        "total_units": 10,
    }


def test_chat_task_association_changes_are_visible_to_the_stream(tmp_path):
    store = _store(tmp_path)
    task = _create(store)
    store.bind_plan(
        task.task_id,
        output_spec_id="spec-1",
        output_spec_version=1,
        document_plan_id="plan-1",
        document_plan_version=1,
    )
    store.attach_work_order(task.task_id, "wo-1")
    store.attach_run(task.task_id, "run-1")
    store.attach_artifact(task.task_id, "artifact-1")
    store.clear_plan(task.task_id)

    event_types = [
        event["event_type"]
        for event in store.list_conversation_stream_events(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id="conversation-1",
        )
    ]

    assert event_types == [
        "task_activated",
        "plan_bound",
        "work_order_bound",
        "run_bound",
        "artifact_bound",
        "plan_cleared",
    ]


def test_current_chat_task_backfills_legacy_conversations_without_a_pointer(tmp_path):
    """Catches legacy sessions staying empty after the current-task migration."""
    store = _store(tmp_path)
    first = _create(store)
    second = _create(
        store,
        initiating_turn_id="turn-2",
        idempotency_key="turn-2:document-generation",
    )
    with store._connect() as conn:  # migration fixture: simulate a pre-pointer database
        conn.execute("DELETE FROM conversation_document_states")

    current = store.get_current_chat_task(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        knowledge_base_name="hardware",
    )

    assert current is not None
    assert current.task_id == second.task_id
    assert current.task_id != first.task_id
    assert store.get_conversation_document_state(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
    ) == {"current_task_id": second.task_id, "revision": 1}


def test_kb_filter_never_reactivates_an_older_task_over_the_current_pointer(tmp_path):
    store = _store(tmp_path)
    older = _create(store)
    current = store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="chat",
        created_by="user-a",
        conversation_id="conversation-1",
        initiating_turn_id="turn-other-kb",
        knowledge_base_name="other-kb",
        status="queued",
        idempotency_key="turn-other-kb:document-generation",
    )

    assert store.get_current_chat_task(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
        knowledge_base_name="hardware",
    ) is None
    state = store.get_conversation_document_state(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-1",
    )
    assert state == {"current_task_id": current.task_id, "revision": 2}
    assert state["current_task_id"] != older.task_id


def test_task_supports_planning_confirmation_and_release_statuses(tmp_path):
    store = _store(tmp_path)
    task = _create(store)
    assert store.update_status(task.task_id, "awaiting_plan_confirmation").status == "awaiting_plan_confirmation"
    assert store.update_status(task.task_id, "awaiting_release").status == "awaiting_release"


def test_task_planning_pointer_conflict_is_rejected(tmp_path):
    store = _store(tmp_path)
    task = _create(store)
    bound = store.bind_plan(
        task.task_id,
        output_spec_id="spec-1", output_spec_version=1,
        document_plan_id="plan-1", document_plan_version=1,
    )
    assert bound.output_spec_id == "spec-1"
    assert bound.document_plan_id == "plan-1"
    with pytest.raises(ValueError, match="already bound"):
        store.bind_plan(
            task.task_id,
            output_spec_id="spec-2", output_spec_version=1,
            document_plan_id="plan-2", document_plan_version=1,
        )


def test_non_chat_task_does_not_fabricate_a_turn(tmp_path):
    store = _store(tmp_path)

    task = store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-1",
    )

    assert task.conversation_id is None
    assert task.initiating_turn_id is None
    assert task.origin == "workbench"


def test_work_order_task_association_does_not_change_frozen_input_fingerprint():
    values = {
        "work_order_id": "wo-1",
        "tenant_id": "tenant-a",
        "scope_type": "knowledge_base",
        "knowledge_base_name": "hardware",
        "project_id": None,
        "baseline_id": None,
        "baseline_content_hash": "",
        "source_set_snapshot_id": "snapshot-1",
        "template_version_id": "template-1",
        "document_schema_id": "schema-1",
        "document_schema_version": "1",
        "template_schema_id": "template-schema-1",
        "template_schema_version": "1",
        "retrieval_policy_version": "1",
        "renderer_policy_version": "1",
        "target_format": "xlsx",
        "execution_mode": "deterministic_only",
        "created_by": "user-a",
    }
    without_task = DocumentWorkOrder(**values)
    with_task = DocumentWorkOrder(**values, task_id="document-task-1")

    assert with_task.task_id == "document-task-1"
    assert with_task.input_fingerprint == without_task.input_fingerprint


def test_job_task_association_round_trips_and_remains_optional(tmp_path):
    store = DocumentAuthoringJobStore(str(tmp_path / "jobs.db"))

    job = store.create_job(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="chat-1",
        client_request_id="request-1",
        operation="generate_work_order",
        task_id="document-task-1",
        work_order_id="wo-1",
        payload={"work_order_id": "wo-1", "knowledge_base_name": "hardware"},
    )

    assert job.task_id == "document-task-1"
    assert store.get(job.job_id).task_id == "document-task-1"


def test_replaying_a_legacy_job_can_backfill_its_task_association(tmp_path):
    store = DocumentAuthoringJobStore(str(tmp_path / "jobs.db"))
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": "chat-1",
        "client_request_id": "request-legacy",
        "operation": "generate_work_order",
        "work_order_id": "wo-1",
        "payload": {"work_order_id": "wo-1", "knowledge_base_name": "hardware"},
    }

    legacy = store.create_job(**values)
    replay = store.create_job(**values, task_id="document-task-1")

    assert replay.job_id == legacy.job_id
    assert replay.task_id == "document-task-1"


def test_task_service_derives_chat_origin_from_request_context(tmp_path):
    service = DocumentTaskService(DocumentTaskStore(str(tmp_path / "document-tasks.db")))
    ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={"initiating_turn_id": "turn-1"},
    )

    task = service.ensure_task(
        ctx,
        template_version_id="template-1",
        idempotency_key="turn-1:document-generation",
    )

    assert task.origin == "chat"
    assert task.conversation_id == "conversation-1"
    assert task.initiating_turn_id == "turn-1"


def test_task_service_reuses_generation_session_task_before_creating_another(tmp_path):
    service = DocumentTaskService(DocumentTaskStore(str(tmp_path / "document-tasks.db")))
    ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={"document_task_origin": "workbench"},
    )

    first = service.ensure_task(
        ctx,
        template_version_id="template-1",
        generation_session_id="generation-session-1",
    )
    replay = service.ensure_task(
        ctx,
        template_version_id="template-1",
        generation_session_id="generation-session-1",
    )

    assert replay.task_id == first.task_id


def test_task_service_does_not_reuse_a_key_across_different_origins(tmp_path):
    service = DocumentTaskService(DocumentTaskStore(str(tmp_path / "document-tasks.db")))
    chat_ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={
            "document_task_origin": "chat",
            "initiating_turn_id": "turn-1",
        },
    )
    workbench_ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        metadata={"document_task_origin": "workbench"},
    )
    service.ensure_task(
        chat_ctx,
        template_version_id="template-1",
        idempotency_key="shared-request",
    )

    with pytest.raises(ValueError, match="origin"):
        service.ensure_task(
            workbench_ctx,
            template_version_id="template-1",
            idempotency_key="shared-request",
        )


def test_legacy_work_order_gets_a_non_persisted_task_view_without_chat_provenance(tmp_path):
    service = DocumentTaskService(DocumentTaskStore(str(tmp_path / "document-tasks.db")))
    order = SimpleNamespace(
        work_order_id="wo-legacy",
        tenant_id="tenant-a",
        created_by="user-a",
        template_version_id="template-1",
        generation_session_id=None,
        project_id=None,
        knowledge_base_name="hardware",
        status="complete",
    )

    task = service.legacy_view_for_work_order(order)

    assert task.task_id == "legacy-document-task-wo-legacy"
    assert task.origin == "legacy"
    assert task.status == "completed"
    assert task.conversation_id is None
    assert task.initiating_turn_id is None
    assert service.store.get(task.task_id) is None


def test_document_generation_service_uses_the_authoring_database_for_tasks(tmp_path):
    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={"document_task_origin": "workbench"},
    )

    task = service.ensure_document_task(
        ctx,
        template_version_id="template-1",
        generation_session_id="generation-session-1",
    )

    assert task.task_id.startswith("document-task-")
    assert service.task_service.store.get(task.task_id).generation_session_id == "generation-session-1"


def test_document_generation_service_projects_runs_artifacts_and_status_to_the_task(tmp_path):
    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={"document_task_origin": "workbench"},
    )
    task = service.ensure_document_task(ctx, template_version_id="template-1")
    order = SimpleNamespace(task_id=task.task_id)
    persisted_artifact = SimpleNamespace(artifact_id="artifact-1", run_id="run-1")
    service.store.save_artifact = Mock(return_value=persisted_artifact)

    service._associate_task_run(order, "run-1")
    saved = service._save_artifact_for_task(order, persisted_artifact, b"content", "xlsx")
    service._sync_task_status(order, "waiting_human_approval")

    assert saved is persisted_artifact
    projected = service.task_service.store.get(task.task_id)
    assert projected.current_run_id == "run-1"
    assert projected.current_artifact_id == "artifact-1"
    assert projected.artifact_ids == ["artifact-1"]
    assert projected.status == "waiting_human"


def test_queueing_a_generation_job_moves_the_user_task_to_queued(tmp_path):
    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={"document_task_origin": "workbench"},
    )
    task = service.ensure_document_task(ctx, template_version_id="template-1")

    queued = service.mark_document_task_queued(task.task_id)

    assert queued is not None
    assert queued.status == "queued"
    assert service.task_service.store.get(task.task_id).status == "queued"


def test_task_write_flag_can_roll_back_to_the_legacy_generation_path(tmp_path, monkeypatch):
    import src.settings

    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    ctx = RequestContext(user_id="user-a", tenant_id="tenant-a")
    monkeypatch.setattr(src.settings, "DOCUMENT_TASK_WRITE_ENABLED", False, raising=False)

    task = service.ensure_document_task(
        ctx,
        template_version_id="template-1",
    )

    assert task is None


def test_new_work_order_is_linked_to_the_single_user_task(tmp_path):
    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    service._template = lambda _template_id: SimpleNamespace(
        status="approved",
        template_version_id="template-1",
        template_schema_id="template-schema-1",
        template_schema_version="1",
        format="xlsx",
    )
    service._schema = lambda _schema_id, _version: SimpleNamespace(
        status="approved",
        execution_mode="deterministic_only",
        fields=[],
        review_items=[],
        document_schema_id="schema-1",
        version="1",
    )
    service._policy = lambda _template: SimpleNamespace(version="1")
    ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={
            "document_task_origin": "chat",
            "initiating_turn_id": "turn-1",
        },
    )
    snapshot = SimpleNamespace(
        tenant_id="tenant-a",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id="snapshot-1",
    )

    order = service._create_frozen_work_order(
        ctx,
        scope_type="knowledge_base",
        snapshot=snapshot,
        knowledge_base_name="hardware",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        idempotency_key="turn-1:document-generation",
        execution_mode="deterministic_only",
    )

    assert order.task_id
    persisted = authoring_store.get_work_order(order.work_order_id)
    assert persisted is not None and persisted.task_id == order.task_id
    task = service.task_service.store.get(order.task_id)
    assert task is not None
    assert task.work_order_id == order.work_order_id


def test_chat_turn_binds_real_conversation_and_turn_to_request_context():
    ctx = RequestContext(user_id="user-a", session_id="conversation-1")
    turn = SimpleNamespace(id="turn-1", session_id=42)

    _bind_document_task_trace(ctx, turn)

    assert ctx.metadata["document_task_origin"] == "chat"
    assert ctx.metadata["conversation_id"] == "42"
    assert ctx.metadata["initiating_turn_id"] == "turn-1"


def test_queued_generation_job_carries_the_work_order_task_id():
    pipeline = object.__new__(__import__("src.core.app_pipeline", fromlist=["AppPipeline"]).AppPipeline)
    order = SimpleNamespace(
        work_order_id="wo-1",
        task_id="document-task-1",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
    )
    job_store = Mock()
    job_store.create_job.return_value = SimpleNamespace(job_id="job-1")
    pipeline.document_generation = SimpleNamespace(
        store=SimpleNamespace(get_work_order=lambda _id: order),
        require_work_order_capability=Mock(),
    )
    pipeline.document_job_store = job_store
    ctx = RequestContext(
        user_id="user-a",
        session_id="conversation-1",
        tenant_id="tenant-a",
        metadata={"department_id": "dept-a"},
        kb_permissions={"dept-a:hardware": "write"},
    )

    result = pipeline.submit_knowledge_base_document_generation(ctx, "wo-1")

    assert result == "job-1"
    assert job_store.create_job.call_args.kwargs["task_id"] == "document-task-1"


def test_document_status_projects_the_user_task_id():
    from src.core.app_pipeline import AppPipeline

    pipeline = object.__new__(AppPipeline)
    order = SimpleNamespace(
        work_order_id="wo-1",
        task_id="document-task-1",
        status="planned",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        target_format="xlsx",
        unit_statuses={},
        validation_report_id=None,
        run_manifest_id=None,
        generation_session_id=None,
        generation_brief={},
        error_code=None,
        error_message=None,
        retryable=None,
        next_actions=[],
    )
    pipeline.document_generation = SimpleNamespace(
        store=SimpleNamespace(
            get_work_order=lambda _id: order,
            list_harness_runs=lambda _id: [],
            list_artifacts=lambda _id: [],
        ),
        require_work_order_capability=Mock(),
    )

    status = pipeline.get_document_run_status(
        "wo-1",
        RequestContext(
            user_id="user-a",
            tenant_id="tenant-a",
            metadata={"department_id": "dept-a"},
            kb_permissions={"dept-a:hardware": "read"},
        ),
    )

    assert status["task_id"] == "document-task-1"


def test_document_status_exposes_legacy_task_projection_for_old_work_orders(tmp_path):
    from src.core.app_pipeline import AppPipeline

    pipeline = object.__new__(AppPipeline)
    order = SimpleNamespace(
        work_order_id="wo-legacy",
        task_id=None,
        tenant_id="tenant-a",
        created_by="user-a",
        status="complete",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        target_format="xlsx",
        unit_statuses={},
        validation_report_id=None,
        run_manifest_id=None,
        generation_session_id=None,
        generation_brief={},
        error_code=None,
        error_message=None,
        retryable=None,
        next_actions=[],
    )
    pipeline.document_generation = SimpleNamespace(
        store=SimpleNamespace(
            get_work_order=lambda _id: order,
            list_harness_runs=lambda _id: [],
            list_artifacts=lambda _id: [],
        ),
        require_work_order_capability=Mock(),
        task_service=DocumentTaskService(DocumentTaskStore(str(tmp_path / "document-tasks.db"))),
    )

    status = pipeline.get_document_run_status(
        "wo-legacy",
        RequestContext(user_id="user-a", tenant_id="tenant-a"),
    )

    assert status["task_id"] == "legacy-document-task-wo-legacy"
    assert status["task"]["origin"] == "legacy"
    assert status["task"]["initiating_turn_id"] is None


def test_disabling_task_reads_hides_task_associations_from_compatibility_status(tmp_path, monkeypatch):
    import src.settings
    from src.core.app_pipeline import AppPipeline

    pipeline = object.__new__(AppPipeline)
    order = SimpleNamespace(
        work_order_id="wo-1",
        task_id="document-task-1",
        status="planned",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        target_format="xlsx",
        unit_statuses={},
        validation_report_id=None,
        run_manifest_id=None,
        generation_session_id=None,
        generation_brief={},
        error_code=None,
        error_message=None,
        retryable=None,
        next_actions=[],
    )
    pipeline.document_generation = SimpleNamespace(
        store=SimpleNamespace(
            get_work_order=lambda _id: order,
            list_harness_runs=lambda _id: [],
            list_artifacts=lambda _id: [],
        ),
        require_work_order_capability=Mock(),
        task_service=DocumentTaskService(DocumentTaskStore(str(tmp_path / "document-tasks.db"))),
    )
    pipeline.document_job_store = SimpleNamespace(
        get_by_work_order=lambda *_args, **_kwargs: SimpleNamespace(
            job_id="job-1", task_id="document-task-1", operation="generate",
            status="queued", attempt=0, last_error=None, result={},
        ),
    )
    monkeypatch.setattr(src.settings, "DOCUMENT_TASK_READ_ENABLED", False, raising=False)

    status = pipeline.get_document_run_status(
        "wo-1",
        RequestContext(
            user_id="user-a",
            tenant_id="tenant-a",
            metadata={"department_id": "dept-a"},
            kb_permissions={"dept-a:hardware": "read"},
        ),
    )

    assert status["task_id"] is None
    assert "task" not in status
    assert "task_id" not in status["job"]
