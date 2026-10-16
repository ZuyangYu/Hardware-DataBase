"""Automatic authority decisions obey rollout without blocking comparisons."""

from types import SimpleNamespace

import pytest

import src.settings as settings
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.provider import WriterRequest


def _request(*, reference_value="12V", reference_scope="target"):
    items = []
    for identity, kb, role, value, approval, scope in (
        ("primary", 1, "primary", "5V", "approved", "target"),
        ("reference", 2, "reference", reference_value, "draft", reference_scope),
    ):
        items.append({
            "id": identity, "content": f"Input voltage {value}", "source_name": f"{identity}.pdf",
            "metadata": {
                "knowledge_source": {"kb_id": kb, "role": role},
                "approval_status": approval,
                "normalized_claim": {
                    "claim_id": identity, "field_key": "operating_voltage",
                    "subject": "input", "predicate": "nominal", "scope_key": scope,
                    "value": value,
                },
            },
        })
    return WriterRequest(work_order_id="wo", run_id="run", unit_id="field:power",
                         unit_label="Input voltage", prompt_version="1", evidence=items)


@pytest.mark.parametrize("enabled", [False, True])
def test_authoritative_resolution_requires_enabled_rollout(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(settings, "MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED", enabled)
    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / "authoring.db")))
    order = SimpleNamespace(work_order_id="wo", tenant_id="default", kb_scope_snapshot={"purpose": "compose"})

    prepared = service.prepare_conflict_writer_request(order, SimpleNamespace(content_hash="frozen"), _request())

    case = service.conflict_store.list_cases(tenant_id="default", work_order_id="wo", statuses=("open", "resolved"))[0]
    if enabled:
        assert case.status == "resolved"
        assert prepared.conflict_context[0]["blocking"] is False
        assert prepared.conflict_context[0]["action"] == "retain_primary"
        assert [item["id"] for item in prepared.evidence] == ["primary"]
    else:
        assert case.status == "open"
        assert "choose_claim" in case.allowed_actions
        assert "defer" in case.allowed_actions
        assert prepared.conflict_context[0]["blocking"] is True
        assert prepared.conflict_context[0]["selected_claim"] is None
        assert [item["id"] for item in prepared.evidence] == ["primary", "reference"]


def test_disabled_auto_resolution_keeps_comparison_differences_nonblocking(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED", False)
    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / "authoring.db")))
    request = _request()
    for item in request.evidence:
        item["metadata"]["knowledge_source"]["role"] = "peer"
    order = SimpleNamespace(work_order_id="wo", tenant_id="default", kb_scope_snapshot={"purpose": "compare"})

    prepared = service.prepare_conflict_writer_request(order, SimpleNamespace(content_hash="frozen"), request)

    assert prepared.conflict_context[0]["blocking"] is False
    assert prepared.conflict_context[0]["action"] == "report_difference"
    assert [item["id"] for item in prepared.evidence] == ["primary", "reference"]


@pytest.mark.parametrize("writer_request", [
    _request(reference_value="5000mV"),
    _request(reference_scope="other_project"),
])
def test_disabled_auto_resolution_does_not_create_review_for_nonconflicts(tmp_path, monkeypatch, writer_request):
    monkeypatch.setattr(settings, "MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED", False)
    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / "authoring.db")))
    order = SimpleNamespace(work_order_id="wo", tenant_id="default", kb_scope_snapshot={"purpose": "compose"})

    prepared = service.prepare_conflict_writer_request(order, SimpleNamespace(content_hash="frozen"), writer_request)

    assert prepared.conflict_context == []
    assert len(prepared.evidence) == 2
    assert service.conflict_store.list_cases(tenant_id="default", work_order_id="wo") == []
