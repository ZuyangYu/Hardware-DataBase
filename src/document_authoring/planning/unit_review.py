"""Bounded deterministic review for one plan-backed semantic unit."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.document_authoring.models import DocumentUnitDraft, content_hash
from src.document_authoring.validator import DocumentValidator

from .review_contracts import CoverageRequirementResult, ReviewIssue, UnitReviewResult, safe_review_projection


class UnitReviewer:
    """Review a draft without widening its frozen source or policy scope."""

    def __init__(self, validator: DocumentValidator | None = None):
        self.validator = validator or DocumentValidator()

    def review(
        self,
        task_spec: Any,
        draft: DocumentUnitDraft | Mapping[str, Any] | None,
        coverage_context: Any,
        evidence_registry: Mapping[str, Any] | None,
        *,
        attempt: int = 1,
    ) -> UnitReviewResult:
        task = _as_mapping(task_spec)
        unit_id = str(task.get("unit_id") or "").strip()
        if not unit_id:
            raise ValueError("unit review requires a task unit_id")
        if attempt < 1:
            raise ValueError("unit review attempts start at one")
        max_attempts = int(task.get("max_attempts") or 1)
        evidence = _evidence_map(evidence_registry)
        issues = _coverage_issues(coverage_context, unit_id)
        if _coverage_status(coverage_context, unit_id) in {"missing", "unsupported", "partial", "conflicting"} and not issues:
            issues.append(ReviewIssue(
                stage="unit_review", code="coverage_incomplete", unit_id=unit_id,
                suggested_action="rework_unit_coverage",
            ))

        normalized_draft: DocumentUnitDraft | None = None
        if draft is None:
            issues.append(ReviewIssue(
                stage="unit_review", code="unit_draft_missing", severity="critical",
                blocking=True, unit_id=unit_id, suggested_action="dispatch_unit_draft",
            ))
        else:
            normalized_draft = (
                draft if isinstance(draft, DocumentUnitDraft)
                else DocumentUnitDraft.model_validate(draft)
            )
            if normalized_draft.unit_id.removeprefix("field:").removeprefix("review:") != unit_id.removeprefix("field:").removeprefix("review:"):
                issues.append(ReviewIssue(
                    stage="unit_review", code="draft_unit_mismatch", severity="critical",
                    blocking=True, unit_id=unit_id,
                    suggested_action="discard_mismatched_draft",
                ))
            if evidence:
                expected_type = _expected_type(task)
                checked = self.validator.validate_unit_draft(normalized_draft, evidence)
                if expected_type:
                    checked = self.validator.validate_typed_field_draft(
                        checked, evidence, expected_value_type=expected_type,
                        expected_row_keys=_task_list(task, "expected_row_keys"),
                        required_columns=_task_list(task, "required_columns"),
                        row_order=str(task.get("row_order") or "input"),
                        duplicate_policy=str(task.get("duplicate_policy") or "reject"),
                    )
                if checked.validation_status != "supported":
                    issues.extend(_validation_issues(checked.validation_notes, unit_id))
                    if not checked.validation_notes:
                        issues.append(ReviewIssue(
                            stage="unit_review", code="draft_not_supported", unit_id=unit_id,
                            suggested_action="rework_unit_draft",
                        ))

        hard_block = any(issue.code in {
            "policy_immutable", "source_policy_changed", "plan_mismatch", "draft_unit_mismatch",
        } for issue in issues)
        if hard_block:
            status = "blocked"
        elif not issues and _coverage_status(coverage_context, unit_id) in {"complete", "passed", "supported", ""}:
            status = "pass"
        elif attempt >= max_attempts:
            status = "needs_human"
            issues = [
                issue.model_copy(update={"blocking": True})
                for issue in issues
            ]
        else:
            status = "rework"

        draft_hash = content_hash(normalized_draft.model_dump(mode="json")) if normalized_draft is not None else None
        return UnitReviewResult(
            status=status,
            unit_id=unit_id,
            attempt=attempt,
            issues=issues,
            accepted_draft_hash=draft_hash if status == "pass" else None,
        )


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    raise TypeError("task specification must be a mapping or model")


def _expected_type(task: Mapping[str, Any]) -> str:
    output = task.get("output_schema") or {}
    if isinstance(output, Mapping):
        return str(output.get("type") or output.get("value_type") or "").strip()
    return ""


def _task_list(task: Mapping[str, Any], key: str) -> list[str]:
    value = task.get(key) or (task.get("output_schema") or {}).get(key, [])
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, (list, tuple)) else []


def _coverage_result(value: Any, unit_id: str) -> CoverageRequirementResult | dict[str, Any] | None:
    if isinstance(value, CoverageRequirementResult):
        return value if value.unit_id == unit_id or value.requirement_id == unit_id else None
    # ``CoverageEvaluator`` returns a typed CoverageReport in the service
    # path.  Keep accepting the historical mapping projection, but do not
    # silently treat the model object as an empty context (which would mark a
    # missing required row as a passing unit).
    requirement_results = getattr(value, "requirement_results", None)
    if requirement_results is not None:
        values = requirement_results.values() if isinstance(requirement_results, Mapping) else requirement_results
        for result in values:
            candidate = _coverage_result(result, unit_id)
            if candidate is not None:
                return candidate
        return None
    if isinstance(value, Mapping):
        if "requirement_results" in value:
            results = value.get("requirement_results") or {}
            for result in results.values() if isinstance(results, Mapping) else results:
                candidate = _coverage_result(result, unit_id)
                if candidate is not None:
                    return candidate
            return None
        candidate_unit = str(value.get("unit_id") or value.get("semantic_unit_id") or "")
        return value if candidate_unit.removeprefix("field:") == unit_id.removeprefix("field:") else None
    return None


def _coverage_status(value: Any, unit_id: str) -> str:
    result = _coverage_result(value, unit_id)
    if isinstance(result, CoverageRequirementResult):
        return result.status
    if isinstance(result, Mapping):
        return str(result.get("status") or "").strip()
    return ""


def _coverage_issues(value: Any, unit_id: str) -> list[ReviewIssue]:
    result = _coverage_result(value, unit_id)
    if result is None:
        return []
    raw = result.issues if isinstance(result, CoverageRequirementResult) else result.get("issues") or []
    issues: list[ReviewIssue] = []
    for issue in raw:
        if isinstance(issue, ReviewIssue):
            issues.append(issue.model_copy(update={"stage": "unit_review"}))
        elif isinstance(issue, Mapping):
            payload = dict(issue)
            payload.setdefault("unit_id", unit_id)
            payload.setdefault("stage", "unit_review")
            issues.append(ReviewIssue.model_validate(payload))
    return issues


def _validation_issues(notes: list[str], unit_id: str) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    for note in notes:
        lowered = note.casefold()
        if "unknown evidence" in lowered:
            code = "unknown_evidence"
        elif "outside" in lowered or "ownership" in lowered:
            code = "evidence_ownership"
        elif "no evidence" in lowered:
            code = "missing_evidence"
        elif "table" in lowered and "kind" in lowered:
            code = "table_scalarized"
        else:
            code = "draft_not_supported"
        issues.append(ReviewIssue(
            stage="unit_review", code=code, unit_id=unit_id,
            suggested_action="rework_unit_draft",
        ))
    return issues


def _evidence_map(value: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not value:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        evidence_id = str(key or "").strip()
        if not evidence_id:
            continue
        result[evidence_id] = dict(item) if isinstance(item, Mapping) else {"id": evidence_id}
    return result
