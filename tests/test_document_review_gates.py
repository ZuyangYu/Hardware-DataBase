"""Task 6 semantic review and bounded release-gate tests."""

from __future__ import annotations

from types import SimpleNamespace

from src.document_authoring.document_model import Citation, DocumentModel, ParagraphBlock
from src.document_authoring.models import AuthoringExecutionEvent, AuthoringRunManifest
from src.document_authoring.planning.models import CoverageContract, CoverageRequirement
from src.document_authoring.planning.review_contracts import (
    CoverageReport,
    CoverageRequirementResult,
    ReviewIssue,
    UnitReviewResult,
)
from src.document_authoring.planning.document_review import (
    DocumentReleaseGate,
    DocumentReviewer,
    DocumentReworkRouter,
)
from src.document_authoring.service import DocumentGenerationService


def _plan(*, required_status: str = "complete"):
    requirements = CoverageContract(requirements=[
        CoverageRequirement(
            requirement_id="summary", unit_id="summary", kind="paragraph",
            required=True, min_evidence_items=1,
        ),
    ])
    return SimpleNamespace(
        document_plan_id="plan-1", version=1, plan_hash="plan-hash", status="accepted",
        semantic_units=[SimpleNamespace(unit_id="summary", kind="paragraph", required=True)],
        coverage_contract=requirements,
        document_review_policy={"require_citations": True},
        render_spec={"format": "xlsx"},
        _required_status=required_status,
    )


def _model(*, citations: bool = True) -> DocumentModel:
    return DocumentModel(
        document_id="document:plan-1:v1", plan_id="plan-1", plan_version=1,
        plan_hash="plan-hash",
        blocks=[ParagraphBlock(
            block_id="block:summary", unit_id="summary", content="Confirmed summary",
            citations=[Citation(evidence_ids=["e1"])] if citations else [],
        )],
    )


def _coverage(status: str = "complete") -> CoverageReport:
    result = CoverageRequirementResult(
        requirement_id="summary", unit_id="summary", kind="paragraph", status=status,
        expected_count=1, covered_count=1 if status == "complete" else 0,
        missing_count=0 if status == "complete" else 1,
        evidence_ids=["e1"] if status == "complete" else [],
    )
    return CoverageReport(
        plan_id="plan-1", plan_version=1, plan_hash="plan-hash",
        requirement_results={"summary": result}, expected_count=1,
        covered_count=1 if status == "complete" else 0,
        missing_count=0 if status == "complete" else 1,
    )


def _unit_review(status: str = "pass", issues=None) -> UnitReviewResult:
    return UnitReviewResult(
        status=status, unit_id="summary", attempt=1,
        issues=list(issues or []), accepted_draft_hash="draft-hash" if status == "pass" else None,
    )


def test_pre_render_review_passes_only_when_required_coverage_and_citations_are_bound():
    report = DocumentReviewer().pre_render(
        _plan(), _model(), _coverage(), {"summary": _unit_review()}
    )

    assert report.stage == "pre_render"
    assert report.status == "pass"
    assert report.model_hash == _model().model_hash
    assert report.report_hash


def test_pre_render_review_blocks_missing_coverage_and_unsupported_evidence():
    unsupported = ReviewIssue(
        stage="unit_review", code="unsupported_evidence", unit_id="summary",
        evidence_ids=["e-bad"], suggested_action="replace_evidence",
    )
    report = DocumentReviewer().pre_render(
        _plan(), _model(), _coverage("missing"),
        {"summary": _unit_review("needs_human", [unsupported])},
    )

    assert report.status == "blocked"
    assert {issue.code for issue in report.issues} >= {
        "required_coverage_missing", "unsupported_evidence",
    }
    assert "summary" in report.affected_unit_ids


def test_pre_render_review_carries_cross_unit_conflicts_and_missing_citations():
    model = _model(citations=False).model_copy(update={
        "issues": [ReviewIssue(
            stage="unit_review", code="cross_unit_identity_conflict",
            unit_id="summary", suggested_action="resolve_identity",
        )],
    })
    report = DocumentReviewer().pre_render(
        _plan(), model, _coverage(), {"summary": _unit_review()}
    )

    assert report.status == "blocked"
    assert {issue.code for issue in report.issues} >= {
        "cross_unit_identity_conflict", "missing_citation",
    }


def test_release_gate_rejects_any_required_pre_or_post_render_issue():
    reviewer = DocumentReviewer()
    pre = reviewer.pre_render(
        _plan(), _model(), _coverage(), {"summary": _unit_review()}
    )
    blocked_pre = pre.model_copy(update={
        "status": "blocked",
        "issues": [ReviewIssue(stage="pre_render", code="required_row_missing")],
    })
    post = pre.model_copy(update={"stage": "post_render"})

    decision = DocumentReleaseGate().evaluate(blocked_pre, post)

    assert decision.status == "blocked"
    assert decision.release_allowed is False
    assert "required_row_missing" in decision.issue_codes


def test_rework_router_bounds_semantic_and_layout_rework():
    semantic = DocumentReviewer().pre_render(
        _plan(), _model(), _coverage("missing"), {"summary": _unit_review("needs_human")}
    )
    semantic = semantic.model_copy(update={
        "status": "rework",
        "issues": [ReviewIssue(stage="pre_render", code="missing_required_value", unit_id="summary")],
        "affected_unit_ids": ["summary"],
    })
    layout = semantic.model_copy(update={
        "stage": "post_render", "issues": [ReviewIssue(
            stage="post_render", code="wrong_physical_row", unit_id="summary",
        )],
    })

    semantic_decision = DocumentReworkRouter(max_attempts=2).route(semantic, attempt=1)
    layout_decision = DocumentReworkRouter(max_attempts=2).route(layout, attempt=1)
    exhausted = DocumentReworkRouter(max_attempts=2).route(semantic, attempt=2)

    assert semantic_decision.route == "semantic_subgraph"
    assert semantic_decision.unit_ids == ["summary"]
    assert layout_decision.route == "layout_binding_renderer"
    assert exhausted.route == "human_review"


def test_run_manifest_binds_review_and_artifact_hashes_to_the_execution():
    manifest = AuthoringRunManifest(
        run_manifest_id="manifest-1", work_order_id="wo-1",
        harness_policy_id="policy-1", harness_policy_version="1",
        writer_provider_id="managed", prompt_version="1",
        source_set_snapshot_id="source-1", input_fingerprint="fp-1",
        document_model_hash="model-hash", pre_render_review_hash="pre-hash",
        post_render_review_hash="post-hash", artifact_hash="artifact-hash",
        release_decision_hash="release-hash", release_status="blocked",
    )

    assert manifest.document_model_hash == "model-hash"
    assert manifest.pre_render_review_hash == "pre-hash"
    assert manifest.post_render_review_hash == "post-hash"
    assert manifest.artifact_hash == "artifact-hash"
    assert manifest.release_decision_hash == "release-hash"
    assert manifest.release_status == "blocked"


def test_execution_events_have_explicit_document_review_and_release_types():
    for event_type in ("pre_render_reviewed", "post_render_reviewed", "release_gated"):
        event = AuthoringExecutionEvent(
            event_id=f"event-{event_type}", event_type=event_type,
            work_order_id="wo-1", harness_run_id="run-1",
            idempotency_key=f"key-{event_type}",
        )
        assert event.event_type == event_type


def test_generation_service_exposes_the_two_review_stages_and_manifest_binding():
    service = object.__new__(DocumentGenerationService)
    service.document_reviewer = DocumentReviewer()
    service.document_release_gate = DocumentReleaseGate()
    service.document_rework_router = DocumentReworkRouter(max_attempts=2)
    pre = service.review_document_pre_render(
        _plan(), _model(), _coverage(), {"summary": _unit_review()}
    )
    # A post-render report can be supplied by the renderer integration; the
    # service method must preserve the independent stage/hash contract.
    post = service.review_document_post_render(
        _plan(), _model(), SimpleNamespace(
            content=b"not-used", integrity_manifest={"manifest_hash": "m"},
        ), b"not-used",
    )

    assert pre.stage == "pre_render"
    assert post.stage == "post_render"
    assert service.evaluate_document_release(pre, post).release_allowed is False
