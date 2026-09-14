from __future__ import annotations

from types import SimpleNamespace

from src.document_authoring.generation_sessions import (
    GenerationBrief,
    GenerationSessionStore,
)
from src.document_authoring.requirement_clarifier import RequirementClarifier
from src.pipelines.document_rag.schemas import RequestContext


def test_generation_session_keeps_conversation_task_and_pending_question_refs(tmp_path):
    store = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = store.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        conversation_id="17",
        initiating_turn_id="turn-a",
        document_task_id="document-task-a",
    )

    message = store.append_message(
        session.session_id,
        role="assistant",
        content="请选择版本",
        question_id="scope.revision",
    )
    loaded = store.get_session(session.session_id)

    assert message.question_id == "scope.revision"
    assert loaded.conversation_id == "17"
    assert loaded.initiating_turn_id == "turn-a"
    assert loaded.document_task_id == "document-task-a"
    assert loaded.last_question_id == "scope.revision"
    assert loaded.clarification_revision == 0


def test_clarification_command_is_atomic_and_idempotent(tmp_path):
    store = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = store.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )
    store.append_message(
        session.session_id,
        role="assistant",
        content="请选择版本",
        question_id="scope.revision",
    )
    brief = RequirementClarifier().apply_answer(
        GenerationBrief(),
        question_id="scope.revision",
        answer="当前发布版本",
    )
    next_message = RequirementClarifier().next_message({}, brief)

    updated, applied = store.apply_clarification_answer(
        session.session_id,
        question_id="scope.revision",
        answer="当前发布版本",
        brief=brief,
        next_message=next_message,
        client_request_id="clarification-request-1",
    )
    repeated, repeated_applied = store.apply_clarification_answer(
        session.session_id,
        question_id="scope.revision",
        answer="当前发布版本",
        brief=brief,
        next_message=next_message,
        client_request_id="clarification-request-1",
    )

    assert applied is True
    assert repeated_applied is False
    assert repeated.model_dump(mode="json") == updated.model_dump(mode="json")
    assert updated.clarification_revision == 1
    assert updated.last_question_id == "missing_data_policy"
    assert len(updated.messages) == 3
    assert [message.client_request_id for message in updated.messages if message.role == "user"] == [
        "clarification-request-1"
    ]
    assert updated.brief.clarification_answers[-1].client_request_id == "clarification-request-1"


def test_clarification_command_rejects_stale_question_and_request_conflict(tmp_path):
    store = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = store.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )
    store.append_message(
        session.session_id,
        role="assistant",
        content="请选择版本",
        question_id="scope.revision",
    )
    brief = RequirementClarifier().apply_answer(
        GenerationBrief(),
        question_id="scope.revision",
        answer="当前发布版本",
    )
    next_message = RequirementClarifier().next_message({}, brief)

    try:
        store.apply_clarification_answer(
            session.session_id,
            question_id="missing_data_policy",
            answer="标记未提供",
            brief=brief,
            next_message=next_message,
            client_request_id="clarification-request-1",
        )
    except ValueError as exc:
        assert "current question" in str(exc)
    else:
        raise AssertionError("stale clarification question should be rejected")

    store.apply_clarification_answer(
        session.session_id,
        question_id="scope.revision",
        answer="当前发布版本",
        brief=brief,
        next_message=next_message,
        client_request_id="clarification-request-1",
    )
    try:
        store.apply_clarification_answer(
            session.session_id,
            question_id="scope.revision",
            answer="最新上传版本",
            brief=brief,
            next_message=next_message,
            client_request_id="clarification-request-1",
        )
    except ValueError as exc:
        assert "idempotency" in str(exc)
    else:
        raise AssertionError("conflicting clarification retry should be rejected")


def test_pipeline_binds_task_and_projects_clarification_to_chat_turn(tmp_path, monkeypatch):
    import src.core.app_pipeline as app_pipeline_module

    events: list[tuple[str, dict]] = []

    class FakeConversation:
        def get_turn_unscoped(self, turn_id):
            return SimpleNamespace(session_id=17, id=turn_id)

        def append_turn_event(self, turn_id, event_type, payload):
            events.append((event_type, payload))

    monkeypatch.setattr(app_pipeline_module, "ConversationService", FakeConversation)
    sessions = GenerationSessionStore(str(tmp_path / "authoring.db"))
    task = SimpleNamespace(task_id="document-task-a")
    template = SimpleNamespace(
        knowledge_base_name="hardware",
        tenant_id="tenant-a",
        resource_department_id=1,
        knowledge_base_id=1,
    )
    analysis = SimpleNamespace(
        format="xlsx",
        model_dump=lambda mode="json": {"format": "xlsx", "units": []},
    )
    document_generation = SimpleNamespace(
        store=SimpleNamespace(
            get_template=lambda _template_id: template,
            get_template_analysis=lambda _template_id: analysis,
            generation_sessions=sessions,
        ),
        _require_template_kb_scope=lambda *_args: None,
        ensure_document_task=lambda *_args, **_kwargs: task,
    )
    pipeline = SimpleNamespace(
        document_generation=document_generation,
        requirement_clarifier=RequirementClarifier(),
        get_document_generation_session=lambda ctx, session_id: sessions.get_session(session_id),
    )
    # Use the real methods while keeping the test fixture's dependencies
    # narrow; this catches the task/conversation wiring at the boundary.
    from src.core.app_pipeline import AppPipeline
    pipeline.create_document_generation_session = AppPipeline.create_document_generation_session.__get__(pipeline)
    pipeline._project_generation_session_event = AppPipeline._project_generation_session_event

    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={
            "document_template_kb_name": "hardware",
            "resource_department_id": 1,
            "kb_id": 1,
            "document_task_origin": "chat",
            "conversation_id": "17",
            "initiating_turn_id": "turn-a",
        },
    )
    session = pipeline.create_document_generation_session(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )

    assert session.document_task_id == "document-task-a"
    assert events[0][0] == "document_clarification_question"
    assert events[0][1]["document_task_id"] == "document-task-a"
    assert events[0][1]["initiating_turn_id"] == "turn-a"


def test_enabled_requirement_resolution_is_snapshotted_into_generation_brief(tmp_path):
    from src.core.app_pipeline import AppPipeline

    class Resolution:
        def to_dict(self):
            return {
                "unresolved_requirements": [{
                    "field": "pcb_revision",
                    "field_label": "PCB Revision",
                    "candidate_values": ["A3", "A4"],
                    "requires_clarification": True,
                    "reason": "multiple_candidates",
                }],
                "clarification_requirements": [{"field": "pcb_revision"}],
                "resolved_fields": {},
                "coverage_by_field": {},
            }

    sessions = GenerationSessionStore(str(tmp_path / "authoring.db"))
    template = SimpleNamespace(
        knowledge_base_name="hardware",
        tenant_id="tenant-a",
        resource_department_id=1,
        knowledge_base_id=1,
    )
    analysis = SimpleNamespace(
        format="xlsx",
        model_dump=lambda mode="json": {"format": "xlsx", "units": []},
    )
    store = SimpleNamespace(
        get_template=lambda _template_id: template,
        get_template_analysis=lambda _template_id: analysis,
        get_document_schema=lambda _schema_id, _version: object(),
        generation_sessions=sessions,
    )
    task = SimpleNamespace(task_id="document-task-a")
    document_generation = SimpleNamespace(
        store=store,
        _require_template_kb_scope=lambda *_args: None,
        ensure_document_task=lambda *_args, **_kwargs: task,
        task_service=SimpleNamespace(store=SimpleNamespace(update_status=lambda *_args: None)),
    )
    pipeline = SimpleNamespace(
        document_generation=document_generation,
        requirement_clarifier=RequirementClarifier(),
        requirement_resolver=SimpleNamespace(resolve=lambda **_kwargs: Resolution()),
        get_document_generation_session=lambda ctx, session_id: sessions.get_session(session_id),
    )
    pipeline.create_document_generation_session = AppPipeline.create_document_generation_session.__get__(pipeline)
    pipeline.resolve_document_requirements = AppPipeline.resolve_document_requirements.__get__(pipeline)
    pipeline._document_requirement_resolution_snapshot = AppPipeline._document_requirement_resolution_snapshot.__get__(pipeline)
    pipeline._document_requirement_resolution_enabled = AppPipeline._document_requirement_resolution_enabled
    pipeline._project_generation_session_event = lambda *_args, **_kwargs: None

    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={
            "document_template_kb_name": "hardware",
            "resource_department_id": 1,
            "kb_id": 1,
            "document_requirement_resolution_enabled": True,
        },
    )
    session = pipeline.create_document_generation_session(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
    )

    assert session.brief.unresolved_requirements[0]["field"] == "pcb_revision"
    assert next(
        message.question_id
        for message in reversed(session.messages)
        if message.role == "assistant" and message.question_id
    ) == "scope.revision"


def test_pipeline_retries_same_clarification_request_without_recomputing_state(tmp_path):
    from src.core.app_pipeline import AppPipeline

    sessions = GenerationSessionStore(str(tmp_path / "authoring.db"))
    created = sessions.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )
    sessions.append_message(
        created.session_id,
        role="assistant",
        content="请选择版本",
        question_id="scope.revision",
    )
    analysis = SimpleNamespace(
        model_dump=lambda mode="json": {"format": "xlsx", "units": []},
    )
    task_service = SimpleNamespace(store=SimpleNamespace(update_status=lambda *_args: None))
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        store=SimpleNamespace(
            generation_sessions=sessions,
            get_template_analysis=lambda _template_id: analysis,
        ),
        task_service=task_service,
    )
    pipeline.requirement_clarifier = RequirementClarifier()
    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={
            "document_template_kb_name": "hardware",
            "resource_department_id": 1,
            "kb_id": 1,
        },
    )

    first = pipeline.answer_document_generation_session(
        ctx,
        created.session_id,
        question_id="scope.revision",
        answer="当前发布版本",
        client_request_id="clarification-request-1",
    )
    repeated = pipeline.answer_document_generation_session(
        ctx,
        created.session_id,
        question_id="scope.revision",
        answer="当前发布版本",
        client_request_id="clarification-request-1",
    )

    assert first.clarification_revision == 1
    assert repeated.clarification_revision == 1
    assert len(repeated.messages) == 3
