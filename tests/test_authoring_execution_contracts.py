"""Phase 0 contract tests: idempotency keys, state machine, state size caps,
agent tool contracts, evidence registry access and legacy checkpoint migration."""

from __future__ import annotations

import pytest

from src.document_authoring.harness.agent_contracts import (
    ERROR_STATE_SIZE_EXCEEDED,
    ERROR_STATE_VERSION_INCOMPATIBLE,
    GRAPH_STATE_VERSION,
    STATE_PAYLOAD_HARD_LIMIT_BYTES,
    AgentToolResult,
    FieldProposalResult,
    serialize_graph_state,
    validate_state_payload_size,
)
from src.document_authoring.harness.evidence_registry import (
    ERROR_CROSS_RUN,
    ERROR_CROSS_TENANT,
    ERROR_EXPIRED,
    ERROR_SNAPSHOT_MISMATCH,
    ERROR_UNAVAILABLE,
    EvidenceAccessError,
    validate_evidence_access,
)
from src.document_authoring.harness.idempotency import (
    agent_thread_id,
    canonical_json,
    execution_event_key,
    human_decision_key,
    receipt_action_key,
)
from src.document_authoring.harness.state_machine import (
    ERROR_RETRYING_NOT_TRANSIENT,
    ERROR_TERMINAL_STATE,
    InvalidRunTransition,
    is_legal_transition,
    validate_transition,
)
from src.document_authoring.migrations.harness_run_state import (
    LEGACY_REASON_ACTIVE_LEASE_UNVERIFIABLE,
    LEGACY_REASON_MISSING_IDENTITY,
    LEGACY_REASON_ROUND_TRIP_FAILED,
    LEGACY_REASON_STATE_TOO_LARGE,
    migrate_checkpoint_payload,
)
from src.document_authoring.models import (
    DocumentWorkOrder,
    EvidenceRegistryEntry,
    compute_input_fingerprint_v2,
)
from datetime import datetime, timedelta, timezone


# ── canonical_json / keys ────────────────────────────────────────────────────


def test_canonical_json_is_sorted_whitespace_free_and_type_strict():
    assert canonical_json({"b": 1, "a": [2, {"z": 1, "y": 2}]}) == '{"a":[2,{"y":2,"z":1}],"b":1}'
    assert canonical_json({"score": 0.5}) == '{"score":0.5}'
    with pytest.raises(ValueError):
        canonical_json(float("nan"))
    with pytest.raises(ValueError):
        canonical_json(float("inf"))
    with pytest.raises(ValueError):
        canonical_json({"x": object()})


def test_receipt_action_key_is_deterministic_and_attempt_scoped():
    base = dict(harness_run_id="run-1", node_name="generate_draft", unit_id="field-0",
                attempt=1, input_fingerprint="fp-1", action="retrieve_evidence")
    key1 = receipt_action_key(**base)
    assert key1 == receipt_action_key(**base)
    assert key1 != receipt_action_key(**{**base, "attempt": 2})
    assert key1 != receipt_action_key(**{**base, "action": "propose_field_value"})


def test_receipt_action_key_rejects_unsafe_action_preimages():
    base = dict(harness_run_id="run-1", node_name="n", unit_id="u", attempt=1,
                input_fingerprint="fp")
    with pytest.raises(ValueError):
        receipt_action_key(**base, action={"prompt": "raw prompt text"})
    with pytest.raises(ValueError):
        receipt_action_key(**base, action={"wall_clock": "2026-08-31T00:00:00Z"})
    with pytest.raises(ValueError):
        receipt_action_key(**base, action="")
    with pytest.raises(ValueError):
        receipt_action_key(**base, action=123)


def test_event_key_separates_lifecycle_facts():
    action_key = receipt_action_key(harness_run_id="r", node_name="n", unit_id="u",
                                    attempt=1, input_fingerprint="fp", action="a")
    assert execution_event_key(action_key, "tool_called") != execution_event_key(action_key, "tool_succeeded")
    assert execution_event_key(action_key, "tool_called") == execution_event_key(action_key, "tool_called")
    with pytest.raises(ValueError):
        execution_event_key("", "tool_called")


def test_human_decision_key_normalizes_and_rejects_unknown_decisions():
    base = dict(harness_run_id="r", pending_event_id="e", proposal_hash="h")
    assert human_decision_key(**base, decision="Approve") == human_decision_key(**base, decision="approve")
    assert human_decision_key(**base, decision="approve") != human_decision_key(**base, decision="reject")
    with pytest.raises(ValueError):
        human_decision_key(**base, decision="maybe")


def test_agent_thread_id_is_stable_and_not_python_hash_derived():
    assert agent_thread_id("run-1", "field-0") == agent_thread_id("run-1", "field-0")
    assert agent_thread_id("run-1", "field-0") != agent_thread_id("run-1", "field-1")
    assert len(agent_thread_id("run-1", "field-0")) == 64
    with pytest.raises(ValueError):
        agent_thread_id("", "field-0")


# ── state machine ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("current,target", [
    ("planned", "queued"), ("queued", "running"), ("running", "paused"),
    ("running", "waiting_human"), ("running", "failed"), ("running", "completed"),
    ("waiting_human", "running"), ("paused", "queued"), ("failed", "queued"),
])
def test_legal_transitions(current, target):
    assert is_legal_transition(current, target)


@pytest.mark.parametrize("current,target", [
    ("planned", "running"), ("queued", "completed"), ("completed", "queued"),
    ("cancelled", "running"), ("planned", "planned"),
])
def test_illegal_transitions(current, target):
    assert not is_legal_transition(current, target)


def test_transition_error_codes_are_stable():
    with pytest.raises(InvalidRunTransition) as terminal:
        validate_transition("completed", "queued")
    assert terminal.value.error_code == ERROR_TERMINAL_STATE
    with pytest.raises(InvalidRunTransition) as transient:
        validate_transition("running", "retrying", lease_released=False)
    assert transient.value.error_code == ERROR_RETRYING_NOT_TRANSIENT
    assert is_legal_transition("running", "retrying", lease_released=True) is False


# ── graph state size / version ───────────────────────────────────────────────


def test_state_payload_size_contract():
    serialize_graph_state({"graph_state_version": GRAPH_STATE_VERSION, "unit_statuses": {}})
    big = "x" * (STATE_PAYLOAD_HARD_LIMIT_BYTES + 1)
    with pytest.raises(ValueError) as exc:
        validate_state_payload_size(big)
    assert ERROR_STATE_SIZE_EXCEEDED in str(exc.value)


def test_state_version_mismatch_is_rejected():
    with pytest.raises(ValueError) as exc:
        serialize_graph_state({"graph_state_version": GRAPH_STATE_VERSION + 1})
    assert ERROR_STATE_VERSION_INCOMPATIBLE in str(exc.value)


# ── agent tool contracts ─────────────────────────────────────────────────────


def test_agent_tool_results_reject_extra_fields_and_unknown_status():
    result = FieldProposalResult(status="succeeded", field_id="f", proposal_hash="h", accepted=True)
    assert result.model_dump()["accepted"] is True
    with pytest.raises(Exception):
        AgentToolResult.model_validate({"status": "succeeded", "field_id": "f", "surprise": 1})
    with pytest.raises(Exception):
        AgentToolResult.model_validate({"status": "exploded", "field_id": "f"})


# ── evidence registry access ─────────────────────────────────────────────────


def _entry(**overrides) -> EvidenceRegistryEntry:
    payload = dict(
        evidence_id="ev-1", tenant_id="t1", harness_run_id="run-1", work_order_id="wo-1",
        knowledge_base_id="kb-1", source_set_snapshot_id="snap-1",
        snapshot_content_hash="sh-1", content_hash="ch-1", source_identity="ragflow://kb-1/doc-1",
        redacted_summary="MCU part number", reload_handle="handle-abc-123",
    )
    payload.update(overrides)
    return EvidenceRegistryEntry.model_validate(payload)


def test_evidence_registry_entry_requires_exactly_one_scope_and_opaque_handle():
    assert _entry().knowledge_base_id == "kb-1"
    with pytest.raises(Exception):
        _entry(project_id="p-1")
    with pytest.raises(Exception):
        _entry(reload_handle="/etc/passwd")


def test_evidence_access_validation_returns_stable_error_codes():
    entry = _entry()
    ok = validate_evidence_access(entry, tenant_id="t1", harness_run_id="run-1",
                                  source_set_snapshot_id="snap-1")
    assert ok is entry
    for kwargs, code in [
        (dict(tenant_id="t2", harness_run_id="run-1", source_set_snapshot_id="snap-1"), ERROR_CROSS_TENANT),
        (dict(tenant_id="t1", harness_run_id="run-2", source_set_snapshot_id="snap-1"), ERROR_CROSS_RUN),
        (dict(tenant_id="t1", harness_run_id="run-1", source_set_snapshot_id="snap-2"), ERROR_SNAPSHOT_MISMATCH),
    ]:
        with pytest.raises(EvidenceAccessError) as exc:
            validate_evidence_access(entry, **kwargs)
        assert exc.value.error_code == code
    with pytest.raises(EvidenceAccessError) as missing:
        validate_evidence_access(None, tenant_id="t1", harness_run_id="run-1",
                                 source_set_snapshot_id="snap-1")
    assert missing.value.error_code == ERROR_UNAVAILABLE


def test_expired_evidence_is_rejected_not_deleted():
    entry = _entry(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    with pytest.raises(EvidenceAccessError) as exc:
        validate_evidence_access(entry, tenant_id="t1", harness_run_id="run-1",
                                 source_set_snapshot_id="snap-1")
    assert exc.value.error_code == ERROR_EXPIRED


# ── legacy checkpoint migration ──────────────────────────────────────────────


def _legacy_payload(status: str = "paused") -> dict:
    return dict(
        checkpoint_id="cp-1", harness_run_id="run-1", work_order_id="wo-1",
        input_fingerprint="fp-1", source_set_snapshot_id="snap-1", fencing_token=3,
        status=status, current_node="generate_draft", step_count=4, retrieval_round_count=1,
        completed_units=2, total_units=5, retry_count=0, max_retries=1,
        unit_statuses={"field-0": "committed", "field-1": "planned"},
        evidence_matrix_hash="mh-1", draft_ids=["d1", "d2"], pending_human_event=None,
        tenant_id="t1",
    )


def test_paused_legacy_checkpoint_converts_with_business_fields_intact():
    result = migrate_checkpoint_payload(_legacy_payload())
    assert result.outcome == "converted"
    assert result.harness_run_fields["status"] == "paused"
    assert result.harness_run_fields["unit_statuses"]["field-0"] == "committed"
    assert result.harness_run_fields["pending_human_event"] is None
    assert result.harness_run_fields["draft_ids"] == ["d1", "d2"]
    assert result.harness_run_fields["fencing_token"] == 3
    assert result.graph_state["graph_state_version"] == 1
    assert result.graph_state["current_node"] == "generate_draft"


def test_missing_unit_attempts_and_cursor_are_safely_initialized():
    payload = _legacy_payload()
    payload.pop("dispatch_cursor", None)
    result = migrate_checkpoint_payload(payload)
    assert result.outcome == "converted"
    assert result.harness_run_fields["dispatch_cursor"] == 0
    assert result.harness_run_fields["unit_attempts"] == {"field-0": 1, "field-1": 1}


def test_active_checkpoint_requires_lease_proof():
    unverifiable = migrate_checkpoint_payload(_legacy_payload("active"), lease_active=None)
    assert unverifiable.outcome == "legacy_terminal"
    assert unverifiable.migration_reason == LEGACY_REASON_ACTIVE_LEASE_UNVERIFIABLE
    assert unverifiable.harness_run_fields["status"] == "paused"
    expired = migrate_checkpoint_payload(_legacy_payload("active"), lease_active=False)
    assert expired.outcome == "legacy_terminal"
    active = migrate_checkpoint_payload(_legacy_payload("active"), lease_active=True)
    assert active.outcome == "converted"
    assert active.harness_run_fields["status"] == "running"
    assert active.harness_run_fields["migration_state"] == "converted"


def test_unconvertible_payloads_are_marked_legacy_terminal():
    missing = migrate_checkpoint_payload({"checkpoint_id": "cp-2"})
    assert missing.outcome == "legacy_terminal"
    assert missing.migration_reason == LEGACY_REASON_MISSING_IDENTITY
    garbage = migrate_checkpoint_payload("not-a-dict")
    assert garbage.outcome == "legacy_terminal"


def test_backend_round_trip_failure_blocks_conversion():
    def failing_round_trip(thread_id, state):
        raise RuntimeError("backend write failed")

    result = migrate_checkpoint_payload(_legacy_payload(), backend_round_trip=failing_round_trip)
    assert result.outcome == "legacy_terminal"
    assert result.migration_reason == LEGACY_REASON_ROUND_TRIP_FAILED

    seen = {}
    def ok_round_trip(thread_id, state):
        seen["thread_id"] = thread_id
        seen["state"] = state

    result = migrate_checkpoint_payload(_legacy_payload(), backend_round_trip=ok_round_trip)
    assert result.outcome == "converted"
    assert seen["thread_id"] == "run-1"


def test_oversized_legacy_state_is_legacy_terminal():
    payload = _legacy_payload()
    payload["pending_human_event"] = {"blob": "x" * 2_000_000}
    result = migrate_checkpoint_payload(payload)
    assert result.outcome == "legacy_terminal"
    assert result.migration_reason == LEGACY_REASON_STATE_TOO_LARGE


# ── work order executor / fingerprint versioning ─────────────────────────────


def _work_order(**overrides) -> DocumentWorkOrder:
    payload = dict(
        work_order_id="wo-fp", project_id="p-1", baseline_id="b-1",
        baseline_content_hash="bh", source_set_snapshot_id="snap-1",
        template_version_id="tv-1", document_schema_id="ds-1", document_schema_version="1",
        template_schema_id="ts-1", template_schema_version="1",
        retrieval_policy_version="1", renderer_policy_version="1", target_format="xlsx",
        execution_mode="internal_harness", created_by="admin",
        harness_policy_id="hp-1", harness_policy_version="1",
    )
    payload.update(overrides)
    return DocumentWorkOrder.model_validate(payload)


def test_requested_executor_conflicts_are_request_errors():
    assert _work_order(requested_executor="internal_harness").input_fingerprint
    with pytest.raises(Exception):
        _work_order(requested_executor="external_agent")


def test_historical_fingerprints_survive_new_contract_fields():
    legacy = _work_order().model_dump(mode="json")
    legacy.pop("requested_executor")
    legacy.pop("input_fingerprint_version")
    reloaded = DocumentWorkOrder.model_validate(legacy)
    assert reloaded.input_fingerprint == _work_order().input_fingerprint


def test_v2_fingerprint_includes_requested_executor():
    v2_order = _work_order(requested_executor="internal_harness")
    v2 = compute_input_fingerprint_v2(v2_order)
    assert v2 == compute_input_fingerprint_v2(
        v2_order.model_copy(update={"input_fingerprint": ""}))
    other = _work_order(requested_executor="internal_harness", work_order_id="wo-other")
    assert compute_input_fingerprint_v2(other) != v2
    with pytest.raises(ValueError):
        compute_input_fingerprint_v2(_work_order())
