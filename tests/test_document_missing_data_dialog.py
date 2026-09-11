from __future__ import annotations

from types import SimpleNamespace

from src.document_authoring.service import DocumentGenerationService


class _Sessions:
    def __init__(self):
        self.session = SimpleNamespace(
            session_id="gs-1",
            contract_version="output_spec_v1",
            status="planned",
            messages=[],
        )
        self.status_updates = []
        self.messages = []

    def get_session(self, session_id):
        assert session_id == "gs-1"
        return self.session

    def set_status(self, session_id, status):
        self.status_updates.append((session_id, status))
        self.session.status = status
        return self.session

    def append_message(self, session_id, **kwargs):
        self.messages.append((session_id, kwargs))
        message = SimpleNamespace(**kwargs)
        self.session.messages.append(message)
        return message


def test_missing_required_fields_become_a_persisted_conversation_question():
    sessions = _Sessions()
    task_updates = []
    service = object.__new__(DocumentGenerationService)
    service.store = SimpleNamespace(generation_sessions=sessions)
    service.task_service = SimpleNamespace(
        store=SimpleNamespace(update_status=lambda task_id, status: task_updates.append((task_id, status)))
    )
    order = SimpleNamespace(
        generation_session_id="gs-1",
        task_id="task-1",
        work_order_id="wo-1",
    )

    result = service._publish_missing_data_clarification(
        order,
        [{"field_id": "pin-11", "message": "必填内容尚未完成：pin-11"}],
    )

    assert result is True
    assert sessions.status_updates == [("gs-1", "needs_clarification")]
    assert task_updates == [("task-1", "needs_clarification")]
    assert len(sessions.messages) == 1
    payload = sessions.messages[0][1]
    assert payload["question_id"] == "missing_data_resolution"
    assert "pin-11" in payload["content"]
    assert payload["options"] == ["标记为未提供，继续生成", "补充说明", "暂停等待资料"]


def test_missing_data_resolution_answer_reopens_plan_intake():
    """The special missing-data question is answered in chat, not workbench."""
    from src.document_authoring.planning.intake import OutputSpecIntakeService

    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-1", version=1, purpose="生成 ICD",
        document_type="icd", deliverables=[{"format": "xlsx", "role": "primary"}],
        layout_source={"mode": "generated_structure", "constraints_profile_id": "icd", "constraints_profile_version": "1"},
        outline=[{"unit_id": "pin-11", "kind": "field", "title": "Pin 11"}],
        missing_data_policy="block_generation", inference_policy="forbid",
        approval_policy_id="default-document-v1",
    )

    updated = intake.merge_missing_data_resolution(
        draft, expected_version=1, answer="标记为未提供，继续生成",
    )

    assert updated["version"] == 2
    assert updated["missing_data_policy"] == "mark_tbd"
