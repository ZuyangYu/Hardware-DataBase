from __future__ import annotations

from unittest.mock import Mock

from src.agents.claim_evidence import RetrievalOutcome
from src.agents.state import Evidence
from src.document_authoring.harness.graph import AuthoringGraph
from src.document_authoring.harness.policy import HarnessToolPolicy
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentFieldSchema,
    DocumentSchema,
    DocumentUnitDraft,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
)


KB = "kb1"
SOURCE = "doc.pdf"
SNAP_ID = "snap-1"


def _policy(*, allowed_tools=None, max_steps: int = 40, max_retrieval_rounds: int = 4,
            max_adaptive_recovery_rounds: int = 0) -> HarnessPolicy:
    return HarnessPolicy(
        harness_policy_id="hp", version="1", status="approved",
        max_steps=max_steps, max_retrieval_rounds=max_retrieval_rounds,
        max_retrieval_attempts_per_unit=2,
        max_adaptive_recovery_rounds=max_adaptive_recovery_rounds,
        allowed_tools=allowed_tools if allowed_tools is not None else [
            "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
            "detect_template_contamination", "validate_cross_unit",
            "rewrite_query", "rerank_evidence",
        ],
    )


def _recovery_policy(*, max_adaptive_recovery_rounds: int = 1,
                     max_retrieval_rounds: int = 4) -> HarnessPolicy:
    return _policy(
        allowed_tools=[
            "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
            "detect_template_contamination", "validate_cross_unit",
            "rewrite_query", "rerank_evidence", "adaptive_recovery",
        ],
        max_adaptive_recovery_rounds=max_adaptive_recovery_rounds,
        max_retrieval_rounds=max_retrieval_rounds,
    )


def _evidence(eid: str, content: str) -> Evidence:
    return Evidence(
        id=eid, content=content, source_name=SOURCE,
        content_kind="document_text", processor_kind="ragflow",
        score=0.5, metadata={"knowledge_base_name": KB},
    )


def _outcome(evidences, *, status="success_with_hits") -> RetrievalOutcome:
    return RetrievalOutcome(
        requirement_id="req-1", status=status, evidences=evidences,
        source_outcomes=[], query_fingerprint="fp",
        applied_source_set_snapshot_id=SNAP_ID, applied_region_policy_versions={},
    )


def _setup(*, missing_policy: str = "mark_tbd"):
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id=SNAP_ID, tenant_id="t", knowledge_base_name=KB,
        source_names=[SOURCE], created_by="alice",
    )
    work_order = DocumentWorkOrder(
        work_order_id="wo-1", tenant_id="t", scope_type="knowledge_base",
        knowledge_base_name=KB, project_id=None, baseline_id=None,
        baseline_content_hash="", source_set_snapshot_id=SNAP_ID,
        template_version_id="tv", document_schema_id="ds", document_schema_version="1",
        template_schema_id="ts", template_schema_version="1",
        retrieval_policy_version="1", renderer_policy_version="1",
        target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id="hp", harness_policy_version="1", created_by="alice",
    )
    schema = DocumentSchema(
        document_schema_id="ds", version="1", document_type="spec",
        execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id="f1", label="额定电流", description="电源拓扑的额定电流",
            retrieval_policy_id="rp", verification_policy_id="vp",
            required_capabilities=["entity_lookup"], authoring_policy="managed_writer",
            missing_policy=missing_policy,
        )],
    )
    harness_run = HarnessRun(
        harness_run_id="hr-1", work_order_id="wo-1", run_manifest_id="rm-1", status="running",
    )
    manifest = AuthoringRunManifest(
        run_manifest_id="rm-1", work_order_id="wo-1", harness_policy_id="hp",
        harness_policy_version="1", writer_provider_id="managed", prompt_version="1",
        source_set_snapshot_id=SNAP_ID, input_fingerprint=work_order.input_fingerprint,
    )
    return work_order, snapshot, schema, harness_run, manifest


def _graph(policy) -> AuthoringGraph:
    def draft_provider(request):
        return DocumentUnitDraft(
            unit_id=request.unit_id, run_id=request.run_id,
            generated_by="managed_writer", validation_status="supported",
        )
    validator = Mock()
    validator.validate_unit_draft.side_effect = lambda draft, ev_by_id: draft
    validator.validate_typed_field_draft.side_effect = lambda draft, ev_by_id, **kwargs: draft
    validator.detect_template_contamination.return_value = []
    validator.validate_cross_unit_consistency.return_value = []
    return AuthoringGraph(
        HarnessToolPolicy(policy), Mock(),
        validator=validator, draft_provider=draft_provider,
    )


def _run(graph, retrieve, *, missing_policy: str = "mark_tbd"):
    work_order, snapshot, schema, harness_run, manifest = _setup(missing_policy=missing_policy)
    return graph.run(
        work_order=work_order, harness_run=harness_run, run_manifest=manifest,
        schema=schema, snapshot=snapshot, legacy_claims=[], retrieve=retrieve,
    )


def test_recovery_fires_on_success_empty_when_enabled():
    relaxed_calls = []

    def retrieve(requirement, attempt, query_override=None, relaxed=False):
        if relaxed:
            relaxed_calls.append(attempt)
            return _outcome([_evidence("a", "alpha")])
        return _outcome([], status="success_empty")

    result = _run(_graph(_recovery_policy()), retrieve)

    assert relaxed_calls == [3]                       # one relaxed retrieve, attempt 3
    assert result.unit_statuses["field:f1"] == "requires_human"
    assert any(i["kind"] == "low_confidence_recovery" for i in result.issues)
    assert result.retrieval_ledger[0]["recovery_triggered"] is True
    assert result.retrieval_ledger[0]["recovery_reason"] == "balanced_route_retry"


def test_recovery_not_fired_when_tool_not_allowed():
    # Default policy (no adaptive_recovery, budget 0) -> zero regression.
    relaxed_calls = []

    def retrieve(requirement, attempt, query_override=None, relaxed=False):
        if relaxed:
            relaxed_calls.append(attempt)
            return _outcome([_evidence("a", "alpha")])
        return _outcome([], status="success_empty")

    result = _run(_graph(_policy()), retrieve)

    assert relaxed_calls == []
    assert not any(i["kind"] == "low_confidence_recovery" for i in result.issues)
    assert result.unit_statuses["field:f1"] != "requires_human"
    assert result.retrieval_ledger[0]["recovery_triggered"] is False


def test_recovery_not_fired_when_budget_zero():
    # Tool allowlisted but budget 0 -> recovery disabled (double switch).
    relaxed_calls = []

    def retrieve(requirement, attempt, query_override=None, relaxed=False):
        if relaxed:
            relaxed_calls.append(attempt)
            return _outcome([_evidence("a", "alpha")])
        return _outcome([], status="success_empty")

    result = _run(_graph(_recovery_policy(max_adaptive_recovery_rounds=0)), retrieve)

    assert relaxed_calls == []
    assert not any(i["kind"] == "low_confidence_recovery" for i in result.issues)
    assert result.unit_statuses["field:f1"] != "requires_human"


def test_recovery_no_hits_keeps_original_status():
    relaxed_calls = []

    def retrieve(requirement, attempt, query_override=None, relaxed=False):
        if relaxed:
            relaxed_calls.append(attempt)
            return _outcome([], status="success_empty")   # recovery found nothing
        return _outcome([], status="success_empty")

    result = _run(_graph(_recovery_policy()), retrieve, missing_policy="block_section")

    assert relaxed_calls == [3]
    assert not any(i["kind"] == "low_confidence_recovery" for i in result.issues)
    # block_section field with no evidence stays blocked, not requires_human.
    assert result.unit_statuses["field:f1"] == "blocked"
    assert result.retrieval_ledger[0]["recovery_triggered"] is False


def test_recovery_not_fired_on_hard_failure():
    relaxed_calls = []

    def retrieve(requirement, attempt, query_override=None, relaxed=False):
        if relaxed:
            relaxed_calls.append(attempt)
            return _outcome([_evidence("a", "alpha")])
        return _outcome([], status="retrieval_failed")

    result = _run(_graph(_recovery_policy()), retrieve)

    assert relaxed_calls == []
    assert result.unit_statuses["field:f1"] == "retrieval_failed"


def test_recovery_does_not_exceed_retrieval_round_budget():
    # max_retrieval_rounds == attempts (2); recovery is a 3rd retrieve that must
    # NOT count against the retrieval-round budget.
    relaxed_calls = []

    def retrieve(requirement, attempt, query_override=None, relaxed=False):
        if relaxed:
            relaxed_calls.append(attempt)
            return _outcome([_evidence("a", "alpha")])
        return _outcome([], status="success_empty")

    result = _run(
        _graph(_recovery_policy(max_retrieval_rounds=2)),
        retrieve,
    )

    assert relaxed_calls == [3]
    assert result.unit_statuses["field:f1"] == "requires_human"
