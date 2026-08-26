from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
import hashlib

import pytest

import src.settings
from src.document_authoring.service import (
    DocumentGenerationService,
    _automatic_release_allowed,
    _requires_human_review,
)


def _service_with_completed_harness(finalize_error: Exception):
    service = object.__new__(DocumentGenerationService)
    order = SimpleNamespace(
        work_order_id="wo-1",
        status="retrieving",
        execution_mode="internal_harness",
        harness_policy_id="policy-1",
        harness_policy_version="1",
        document_schema_id="schema-1",
        document_schema_version="1",
        template_version_id="template-1",
    )
    policy = SimpleNamespace(
        status="approved",
        writer_provider_id="managed",
    )
    run = SimpleNamespace(harness_run_id="harness-1")
    manifest = SimpleNamespace(run_manifest_id="manifest-1")
    writer = SimpleNamespace(provider=SimpleNamespace(provider_id="managed"))
    replacements: list[dict] = []

    service._order = Mock(return_value=order)
    service.store = SimpleNamespace(
        get_icd_scope_review=Mock(return_value=None),
        get_harness_policy=Mock(return_value=policy),
        list_legacy_template_claims=Mock(return_value=[]),
        get_harness_run=Mock(return_value=SimpleNamespace(status="waiting_human")),
    )
    service._writer_for_policy = Mock(return_value=writer)
    service._rewriter_for_policy = Mock(return_value=None)
    service._reranker_for_policy = Mock(return_value=None)
    service._fit_checker_for_policy = Mock(return_value=None)
    service._schema = Mock(return_value=SimpleNamespace())
    service.resolve_source_snapshot = Mock(return_value=SimpleNamespace())
    service._template = Mock(return_value=SimpleNamespace(template_version_id="template-1"))
    service.harness_runtime = SimpleNamespace(
        create_run=Mock(return_value=(run, manifest)),
        execute=Mock(return_value=SimpleNamespace()),
    )

    def replace(current, **updates):
        replacements.append(updates)
        return SimpleNamespace(**{**vars(current), **updates})

    service._replace_order = Mock(side_effect=replace)
    service._finalize_internal_harness_result = Mock(side_effect=finalize_error)
    return service, replacements


def test_finalize_renderer_error_persists_blocked_status():
    service, replacements = _service_with_completed_harness(
        ValueError("abnormal duplicate long value fan-out is not allowed"),
    )

    with pytest.raises(ValueError, match="duplicate long value"):
        service.run_internal_harness("ctx", "wo-1", retrieve=Mock())

    assert replacements[-1]["status"] == "blocked"
    assert replacements[-1]["error_code"] == "renderer_safety_violation"
    assert "duplicate long value" in replacements[-1]["error_message"]
    assert replacements[-1]["next_actions"] == ["view_error", "replace_template", "retry_generation"]


def test_unexpected_finalize_error_persists_failed_status():
    service, replacements = _service_with_completed_harness(RuntimeError("artifact store offline"))

    with pytest.raises(RuntimeError, match="artifact store offline"):
        service.run_internal_harness("ctx", "wo-1", retrieve=Mock())

    assert replacements[-1]["status"] == "failed"
    assert replacements[-1]["error_code"] == "finalization_failed"
    assert replacements[-1]["retryable"] is True


def test_verified_candidate_is_auto_published_without_human_event(monkeypatch):
    monkeypatch.setattr(src.settings, "DOCUMENT_AUTO_PUBLISH_VERIFIED", True)
    service = object.__new__(DocumentGenerationService)
    content = b"verified workbook"
    candidate = SimpleNamespace(
        artifact_id="candidate-1",
        tenant_id="tenant-a",
        work_order_id="wo-1",
        run_id="harness-1",
        content_hash=hashlib.sha256(content).hexdigest(),
        validation_report_id="report-1",
        integrity_manifest_id="manifest-1",
    )
    report = SimpleNamespace(status="passed", content_hash="report-hash", issues=[])
    order = SimpleNamespace(
        work_order_id="wo-1",
        target_format="xlsx",
    )
    saved = []
    service.store = SimpleNamespace(
        save_artifact=Mock(side_effect=lambda artifact, data, suffix: saved.append((artifact, data, suffix)) or artifact),
        list_human_events=Mock(return_value=[]),
    )
    service.resolve_source_snapshot = Mock(return_value=SimpleNamespace(content_hash="snapshot-hash"))
    service._replace_order = Mock()

    released = service._auto_publish_verified_candidate(
        order,
        candidate,
        report,
        content,
        unit_statuses={"field:project_name": "complete"},
        evidence_matrix_id="matrix-wo-1",
        validation_report_id="report-1",
    )

    assert released.stage == "approved_release"
    assert released.parent_artifact_id == "candidate-1"
    assert released.approval_event_ids == []
    assert released.status_reasons[0]["code"] == "auto_published_verified"
    service._replace_order.assert_called_once_with(
        order,
        status="complete",
        error_code=None,
        error_message=None,
        retryable=None,
        next_actions=["view_result"],
        unit_statuses={"field:project_name": "complete"},
        evidence_matrix_id="matrix-wo-1",
        validation_report_id="report-1",
    )


def test_auto_publish_policy_does_not_route_missing_tbd_fields_to_human_review():
    assert _requires_human_review(
        {"field:unavailable_pin": "tbd", "field:verified": "ready_to_render"},
        auto_publish_verified=True,
    ) is False
    assert _requires_human_review(
        {"field:unavailable_pin": "tbd", "field:verified": "ready_to_render"},
        auto_publish_verified=False,
    ) is True


def test_direct_verified_work_order_can_be_auto_released_without_chat_session(monkeypatch):
    monkeypatch.setattr(src.settings, "DOCUMENT_AUTO_PUBLISH_VERIFIED", True)
    order = SimpleNamespace(generation_session_id=None, generation_brief={})
    report = SimpleNamespace(status="passed")

    assert _automatic_release_allowed(order, report, requires_review=False) is True
