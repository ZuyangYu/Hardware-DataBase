"""Reviewed facts affect only their units and are part of writer identity."""
from types import SimpleNamespace

from src.document_authoring.conflicts.models import ConflictDecision
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.provider import WriterRequest
from tests.test_grounded_claim_extraction import evidence


def test_review_blocks_before_writer_and_resolution_changes_writer_input(tmp_path):
    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / "a.db")))
    order = SimpleNamespace(work_order_id="wo", tenant_id="default", kb_scope_snapshot={"purpose": "compose"})
    snapshot = SimpleNamespace(content_hash="frozen")
    request = WriterRequest(work_order_id="wo", run_id="run", unit_id="field:power",
                            unit_label="输入电压", prompt_version="1",
                            evidence=[evidence("输入电压5V。", identity="a"),
                                      evidence("输入电压12V。", identity="b")])
    assert callable(getattr(service, "prepare_conflict_writer_request", None))
    first = service.prepare_conflict_writer_request(order, snapshot, request)
    assert first.conflict_context[0]["blocking"] is True
    case = service.conflict_store.list_cases(tenant_id="default", work_order_id="wo")[0]
    service.conflict_store.submit_decision(
        case.conflict_id, tenant_id="default", actor_id="engineer",
        expected_fingerprint=case.fingerprint, expected_version=case.row_version,
        expected_snapshot_hash=case.snapshot_hash, expected_evidence_set_hash=case.evidence_set_hash,
        decision=ConflictDecision(conflict_id=case.conflict_id, action="choose_claim",
                                  selected_claim_id=case.claims[0].claim_id, requires_human=True, blocking=False),
        idempotency_key="review1")
    resumed = service.prepare_conflict_writer_request(order, snapshot, request)
    assert resumed.conflict_context[0]["blocking"] is False
    assert resumed.conflict_context[0]["selected_claim"]["display_value"] == "5V"
    assert resumed.conflict_context[0]["approval_status"] != "approved"
    assert resumed.decision_fingerprint != first.decision_fingerprint
    assert request.evidence[1]["content"] == "输入电压12V。"
    unrelated = request.model_copy(update={"unit_id": "field:title", "evidence": [evidence("项目名称EPS。", identity="title")]})
    assert service.prepare_conflict_writer_request(order, snapshot, unrelated).conflict_context == []


def test_repeated_fact_keeps_one_case_and_links_all_units(tmp_path):
    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / "a.db")))
    order = SimpleNamespace(work_order_id="wo", tenant_id="default", kb_scope_snapshot={"purpose": "compose"})
    snapshot = SimpleNamespace(content_hash="frozen")
    for unit in ("field:power", "field:test"):
        request = WriterRequest(work_order_id="wo", run_id="run", unit_id=unit,
                                unit_label="输入电压", prompt_version="1",
                                evidence=[evidence("输入电压5V。", identity="a"),
                                          evidence("输入电压12V。", identity="b")])
        assert callable(getattr(service, "prepare_conflict_writer_request", None))
        service.prepare_conflict_writer_request(order, snapshot, request)
    cases = service.conflict_store.list_cases(tenant_id="default", work_order_id="wo")
    assert len(cases) == 1
    assert set(cases[0].affected_unit_ids) == {"field:power", "field:test"}


def test_missing_primary_cannot_turn_reference_value_into_target_requirement(tmp_path, monkeypatch):
    import src.settings
    monkeypatch.setattr(src.settings, "MULTI_KB_HUMAN_REVIEW_ENABLED", True)
    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / "a.db")))
    order = SimpleNamespace(work_order_id="wo", tenant_id="default", kb_scope_snapshot={
        "purpose": "compose", "sources": [{"kb_id": 1, "role": "primary"}, {"kb_id": 2, "role": "reference"}],
    })
    request = WriterRequest(work_order_id="wo", run_id="run", unit_id="field:voltage",
                            unit_label="输入电压", prompt_version="1",
                            evidence=[evidence("输入电压12V。", kb=2, role="reference")])
    prepared = service.prepare_conflict_writer_request(order, SimpleNamespace(content_hash="s"), request)
    assert prepared.conflict_context[0]["blocking"] is True
    case = service.conflict_store.list_cases(tenant_id="default", work_order_id="wo")[0]
    assert case.conflict_kind == "critical_source_gap"
    assert set(case.allowed_actions) == {"choose_design_candidate", "defer"}


def test_duplicate_approved_primary_occurrences_do_not_cause_human_prompt():
    from src.document_authoring.conflicts.detector import ConflictDetector
    from src.document_authoring.conflicts.normalizer import normalize_claim
    from src.document_authoring.conflicts.policy import ConflictPolicy
    claims = [normalize_claim(claim_id=identity, field_key="voltage", subject="input", predicate="nominal",
                              value=value, source_kb_id=kb, source_role=role, approval_status=approval)
              for identity, value, kb, role, approval in [
                  ("a1", "5V", 1, "primary", "approved"), ("a2", "5000mV", 1, "primary", "approved"),
                  ("b", "12V", 2, "reference", "draft"),
              ]]
    decision = ConflictPolicy().evaluate(ConflictDetector().detect(claims)[0], purpose="compose")
    assert decision.action == "retain_primary"
    assert decision.requires_human is False
