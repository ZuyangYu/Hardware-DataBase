"""R3 T9.5-A/B: normalized claims and deterministic conflict policy."""

from __future__ import annotations

from src.document_authoring.conflicts.detector import ConflictDetector
from src.document_authoring.conflicts.normalizer import normalize_claim
from src.document_authoring.conflicts.policy import ConflictPolicy
from src.document_authoring.conflicts.store import ConflictDecisionStore


def _claim(
    claim_id: str,
    value: str,
    *,
    unit: str,
    role: str,
    approval_status: str = "",
    scope_key: str = "target-A",
):
    return normalize_claim(
        claim_id=claim_id,
        field_key="input_voltage",
        subject="power module",
        predicate="requires",
        value=value,
        unit=unit,
        scope_key=scope_key,
        source_kb_id=101 if role == "primary" else 205,
        source_role=role,
        approval_status=approval_status,
        evidence_id=f"evidence:{claim_id}",
    )


def test_equivalent_units_do_not_create_a_conflict():
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="approved"),
        _claim("b", "5000", unit="mV", role="reference"),
    ]

    assert ConflictDetector().detect(claims) == []


def test_different_project_scope_is_not_a_cross_source_conflict():
    claims = [
        _claim("a", "5", unit="V", role="primary", scope_key="target-A"),
        _claim("b", "12", unit="V", role="reference", scope_key="historical-B"),
    ]

    assert ConflictDetector().detect(claims) == []


def test_approved_primary_fact_is_retained_when_reference_differs():
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="approved"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]

    decision = ConflictPolicy().evaluate(case, purpose="compose")

    assert decision.action == "retain_primary"
    assert decision.requires_human is False
    assert decision.blocking is False


def test_unapproved_primary_conflict_requires_a_human_decision():
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]

    decision = ConflictPolicy().evaluate(case, purpose="compose")

    assert decision.action == "choose_claim"
    assert decision.requires_human is True
    assert decision.blocking is True


def test_compare_peers_report_difference_without_blocking_generation():
    claims = [
        _claim("a", "5", unit="V", role="peer"),
        _claim("b", "12", unit="V", role="peer"),
    ]
    case = ConflictDetector().detect(claims)[0]

    decision = ConflictPolicy().evaluate(case, purpose="compare")

    assert decision.action == "report_difference"
    assert decision.requires_human is False
    assert decision.blocking is False


def test_conflict_decision_store_binds_snapshot_and_is_idempotent(tmp_path):
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    store = ConflictDecisionStore(str(tmp_path / "conflicts.db"))
    store.save_case(case, tenant_id="default", work_order_id="work-1")

    decision = ConflictPolicy().evaluate(case, purpose="compose")
    first = store.submit_decision(
        case.conflict_id,
        tenant_id="default",
        actor_id="reviewer-7",
        expected_fingerprint=case.fingerprint,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=decision.model_copy(update={"selected_claim_id": "a"}),
        idempotency_key="choose-1",
    )
    replay = store.submit_decision(
        case.conflict_id,
        tenant_id="default",
        actor_id="reviewer-7",
        expected_fingerprint=case.fingerprint,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=decision.model_copy(update={"selected_claim_id": "a"}),
        idempotency_key="choose-1",
    )

    assert first == replay
    assert store.get_case(case.conflict_id).status == "resolved"


def test_conflict_decision_store_rejects_stale_source_snapshot(tmp_path):
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    store = ConflictDecisionStore(str(tmp_path / "conflicts.db"))
    store.save_case(case, tenant_id="default", work_order_id="work-1")
    decision = ConflictPolicy().evaluate(case, purpose="compose").model_copy(
        update={"selected_claim_id": "a"}
    )

    import pytest

    with pytest.raises(ValueError, match="source_snapshot_changed"):
        store.submit_decision(
            case.conflict_id,
            tenant_id="default",
            actor_id="reviewer-7",
            expected_fingerprint=case.fingerprint,
            expected_snapshot_hash="changed",
            expected_evidence_set_hash=case.evidence_set_hash,
            decision=decision,
            idempotency_key="choose-2",
        )
