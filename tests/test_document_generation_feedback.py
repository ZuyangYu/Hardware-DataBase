from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.document_authoring.service import DocumentGenerationService
from src.pipelines.document_rag.schemas import RequestContext


def _service_for_auto_run() -> DocumentGenerationService:
    service = object.__new__(DocumentGenerationService)
    service.create_document_work_order = Mock(return_value=SimpleNamespace(work_order_id="wo-1"))
    service.run_internal_harness = Mock(return_value=SimpleNamespace(artifact_id="candidate-1", stage="review_candidate"))
    service.store = SimpleNamespace(get_work_order=Mock(return_value=SimpleNamespace(status="waiting_human_approval")))
    service.approve_document_artifact = Mock()
    return service


def test_auto_generation_returns_review_candidate_without_implicit_approval():
    service = _service_for_auto_run()

    result = service.auto_generate_document(
        "ctx",
        project_id="project-1",
        baseline_id="baseline-1",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        retrieve=Mock(),
    )

    assert result.artifact_id == "candidate-1"
    service.approve_document_artifact.assert_not_called()


def test_feedback_event_is_hash_bound_but_does_not_approve_or_publish():
    service = object.__new__(DocumentGenerationService)
    artifact = SimpleNamespace(
        work_order_id="wo-1",
        artifact_id="candidate-1",
        run_id="run-1",
        content_hash="artifact-hash",
        validation_report_id="report-1",
    )
    report = SimpleNamespace(content_hash="report-hash")
    order = SimpleNamespace(work_order_id="wo-1", scope_type="knowledge_base")
    event_store = Mock(side_effect=lambda event: event)
    service._artifact_for_context = Mock(return_value=artifact)
    service._order_raw = Mock(return_value=order)
    service.require_work_order_capability = Mock()
    service.approve_document_artifact = Mock()
    service.store = SimpleNamespace(get_validation_report=Mock(return_value=report), save_human_event=event_store)
    service.resolve_source_snapshot = Mock(return_value=SimpleNamespace(content_hash="snapshot-hash"))
    ctx = RequestContext(user_id="alice", kb_permissions={"hardware": "read"})

    event = service.submit_document_human_event(
        ctx,
        artifact_id="candidate-1",
        unit_id="artifact",
        event_type="feedback",
        comment="请补充连接器型号的来源。",
    )

    assert event.event_type == "feedback"
    assert event.subject_artifact_content_hash == "artifact-hash"
    assert event.approval_subject_hash is None
    service.require_work_order_capability.assert_called_once_with(ctx, order, "submit_human_event")
    service.approve_document_artifact.assert_not_called()


def test_feedback_requires_non_empty_comment():
    service = object.__new__(DocumentGenerationService)
    service.submit_document_human_event = Mock()

    with pytest.raises(ValueError, match="feedback comment"):
        service.submit_document_feedback("ctx", "candidate-1", comment=" ")


def test_candidate_preview_requires_the_same_review_access_as_download():
    service = object.__new__(DocumentGenerationService)
    artifact = SimpleNamespace(work_order_id="wo-1", stage="review_candidate")
    order = SimpleNamespace(work_order_id="wo-1", target_format="xlsx")
    service._artifact_for_context = Mock(return_value=artifact)
    service._order_raw = Mock(return_value=order)
    service.require_work_order_capability = Mock()
    service.store = SimpleNamespace(read_artifact_content=Mock(return_value=b"not an office package"))

    preview = service.preview_document_artifact("ctx", "candidate-1")

    assert preview["format"] == "xlsx"
    assert preview["warnings"]
    service.require_work_order_capability.assert_called_once_with("ctx", order, "download_review_candidate")
