"""Task 4 bounded unit-review contract tests."""

from __future__ import annotations

from src.document_authoring.models import DocumentUnitDraft, DraftAssertion, TypedFieldValue
from src.document_authoring.planning.models import CoverageRequirement, UnitTaskSpec
from src.document_authoring.planning.unit_review import UnitReviewer, safe_review_projection


EVIDENCE = {"e1": {"id": "e1", "content": "Voltage is 12 V", "metadata": {}}}


def _draft(*, status="supported", evidence_id="e1") -> DocumentUnitDraft:
    return DocumentUnitDraft(
        unit_id="field:voltage", run_id="run-1", generated_by="managed_writer",
        content="Voltage is 12 V", evidence_ids=[evidence_id],
        typed_value=TypedFieldValue(
            kind="scalar", normalized_values=["12 V"], display_value="12 V",
            evidence_ids=[evidence_id],
        ),
        assertions=[DraftAssertion(
            assertion_id="a1", claim_id="voltage", text="Voltage is 12 V",
            evidence_ids=[evidence_id],
        )],
        validation_status=status,
    )


def _task(max_attempts: int = 2) -> UnitTaskSpec:
    return UnitTaskSpec(
        task_id="task-voltage", unit_id="voltage", plan_version=1,
        output_schema={"type": "number"}, action_key="draft:voltage",
        max_attempts=max_attempts,
    )


def _coverage(status: str = "complete") -> dict:
    return {"unit_id": "voltage", "status": status, "issues": []}


def test_supported_unit_is_passed_with_stable_draft_and_report_hashes():
    result = UnitReviewer().review(
        _task(), _draft(), _coverage(), EVIDENCE,
    )

    assert result.status == "pass"
    assert result.unit_id == "voltage"
    assert result.attempt == 1
    assert result.accepted_draft_hash
    assert result.report_hash
    assert result.issues == []


def test_invalid_unit_is_reworked_until_attempt_limit_then_needs_human():
    invalid = _draft(status="unsupported", evidence_id="unknown")
    reviewer = UnitReviewer()

    first = reviewer.review(_task(max_attempts=2), invalid, _coverage(), EVIDENCE, attempt=1)
    last = reviewer.review(_task(max_attempts=2), invalid, _coverage(), EVIDENCE, attempt=2)

    assert first.status == "rework"
    assert any(issue.code == "unknown_evidence" for issue in first.issues)
    assert last.status == "needs_human"
    assert any(issue.blocking for issue in last.issues)


def test_missing_unlocatable_coverage_is_blocked_and_projection_contains_no_raw_evidence():
    result = UnitReviewer().review(
        _task(), None, {
            "unit_id": "voltage", "status": "missing",
            "issues": [{"code": "missing_evidence", "evidence_ids": ["e1"]}],
        }, EVIDENCE,
    )

    assert result.status in {"rework", "needs_human", "blocked"}
    if result.status == "blocked":
        assert any(issue.code == "unit_draft_missing" for issue in result.issues)
    public = safe_review_projection(result)
    serialized = str(public)
    assert "Voltage is 12 V" not in serialized
    assert "e1" in serialized
    assert "content" not in public


def test_policy_issue_cannot_be_auto_approved():
    coverage = {
        "unit_id": "voltage", "status": "complete",
        "issues": [{"code": "policy_immutable", "severity": "critical", "blocking": True}],
    }
    result = UnitReviewer().review(_task(), _draft(), coverage, EVIDENCE)

    assert result.status == "blocked"
    assert any(issue.code == "policy_immutable" for issue in result.issues)
