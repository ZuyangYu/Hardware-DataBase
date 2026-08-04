from __future__ import annotations

from unittest.mock import Mock

import pytest

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


def _policy(*, allowed_tools=None) -> HarnessPolicy:
    return HarnessPolicy(
        harness_policy_id="hp", version="1", status="approved",
        max_steps=40, max_retrieval_rounds=4, max_retrieval_attempts_per_unit=2,
        allowed_tools=allowed_tools if allowed_tools is not None else [
            "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
            "detect_template_contamination", "validate_cross_unit",
            "requirement_fit_check",
        ],
    )


def _evidence(eid: str, content: str) -> Evidence:
    return Evidence(
        id=eid, content=content, source_name=SOURCE,
        content_kind="document_text", processor_kind="ragflow",
        score=0.5, metadata={"knowledge_base_name": KB},
    )


def _outcome(evidences) -> RetrievalOutcome:
    return RetrievalOutcome(
        requirement_id="req-1", status="success_with_hits", evidences=evidences,
        source_outcomes=[], query_fingerprint="fp",
        applied_source_set_snapshot_id=SNAP_ID, applied_region_policy_versions={},
    )


def _setup():
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


def _graph(*, fit_checker=None, validator_status="supported", policy=None) -> AuthoringGraph:
    def draft_provider(request):
        return DocumentUnitDraft(
            unit_id=request.unit_id, run_id=request.run_id,
            generated_by="managed_writer", validation_status="pending", content="draft body",
        )

    validator = Mock()
    validator.validate_unit_draft.side_effect = lambda draft, ev_by_id: draft.model_copy(
        update={"validation_status": validator_status}
    )
    validator.detect_template_contamination.return_value = []
    validator.validate_cross_unit_consistency.return_value = []
    return AuthoringGraph(
        HarnessToolPolicy(policy or _policy()), Mock(),
        validator=validator, draft_provider=draft_provider, reranker=None,
        fit_checker=fit_checker,
    )


def _run(graph):
    work_order, snapshot, schema, harness_run, manifest = _setup()
    retrieve = lambda req, attempt, query_override=None: _outcome([_evidence("a", "alpha")])
    return graph.run(
        work_order=work_order, harness_run=harness_run, run_manifest=manifest,
        schema=schema, snapshot=snapshot, legacy_claims=[], retrieve=retrieve,
    )


def test_fit_check_marks_requires_human_when_unfit():
    fit_checker = Mock()
    fit_checker.check.return_value = {"fit": False, "reason": "missing spec value"}
    graph = _graph(fit_checker=fit_checker)

    result = _run(graph)

    assert result.unit_statuses["field:f1"] == "requires_human"
    assert result.drafts[0].validation_status == "requires_human"
    assert any(issue["kind"] == "requirement_fit_failed" for issue in result.issues)
    fit_checker.check.assert_called_once()


def test_fit_check_passes_when_fit():
    fit_checker = Mock()
    fit_checker.check.return_value = {"fit": True, "reason": "ok"}
    graph = _graph(fit_checker=fit_checker)

    result = _run(graph)

    assert result.unit_statuses["field:f1"] == "ready_to_render"
    fit_checker.check.assert_called_once()


def test_fit_check_skipped_when_fit_checker_none():
    graph = _graph(fit_checker=None)

    result = _run(graph)

    assert result.unit_statuses["field:f1"] == "ready_to_render"


def test_fit_check_skipped_for_unsupported_unit():
    fit_checker = Mock()
    fit_checker.check.return_value = {"fit": False, "reason": "would block"}
    graph = _graph(fit_checker=fit_checker, validator_status="unsupported")

    result = _run(graph)

    assert result.unit_statuses["field:f1"] == "requires_human"
    fit_checker.check.assert_not_called()  # unsupported short-circuits before fit check


def test_fit_check_gated_by_require_tool():
    policy = _policy(allowed_tools=[
        "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
        "detect_template_contamination", "validate_cross_unit",
    ])  # no requirement_fit_check
    fit_checker = Mock()
    fit_checker.check.return_value = {"fit": True, "reason": "ok"}
    graph = _graph(fit_checker=fit_checker, policy=policy)

    with pytest.raises(PermissionError):
        _run(graph)
