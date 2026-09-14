"""Document-level semantic review and bounded release routing."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.document_authoring.models import content_hash
from src.document_authoring.planning.review_contracts import (
    CoverageReport,
    ReviewIssue,
    UnitReviewResult,
)

from .artifact_review import ArtifactReviewer


class DocumentReviewReport(BaseModel):
    """Hash-bound result for one semantic or physical document review."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    stage: Literal["pre_render", "post_render"]
    plan_id: str = ""
    plan_version: int | None = None
    plan_hash: str = ""
    document_model_hash: str
    model_hash: str = ""
    artifact_hash: str | None = None
    render_manifest_hash: str | None = None
    status: Literal["pass", "rework", "needs_review", "blocked"] = "pass"
    issues: list[ReviewIssue] = Field(default_factory=list)
    affected_unit_ids: list[str] = Field(default_factory=list)
    report_hash: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "DocumentReviewReport":
        self.plan_id = self.plan_id.strip()
        self.plan_hash = self.plan_hash.strip()
        self.document_model_hash = self.document_model_hash.strip()
        self.model_hash = self.model_hash.strip() or self.document_model_hash
        if not self.document_model_hash:
            raise ValueError("document review requires a document model hash")
        self.affected_unit_ids = _unique_strings(self.affected_unit_ids)
        expected = content_hash(self.model_dump(mode="json", exclude={"report_hash"}))
        if self.report_hash and self.report_hash != expected:
            raise ValueError("document review report hash does not match its contents")
        self.report_hash = expected
        return self

    @property
    def blocking(self) -> bool:
        return any(bool(issue.blocking) for issue in self.issues)

    @property
    def issue_codes(self) -> list[str]:
        return list(dict.fromkeys(issue.code for issue in self.issues))


class ReleaseDecision(BaseModel):
    """The only decision object accepted by a plan-backed release caller."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["release", "needs_review", "blocked"]
    release_allowed: bool = False
    plan_id: str = ""
    plan_hash: str = ""
    model_hash: str
    artifact_hash: str | None = None
    pre_render_report_hash: str
    post_render_report_hash: str
    issue_codes: list[str] = Field(default_factory=list)
    subject_hash: str = ""

    @model_validator(mode="after")
    def bind_subject(self) -> "ReleaseDecision":
        self.issue_codes = _unique_strings(self.issue_codes)
        if not self.subject_hash:
            self.subject_hash = content_hash({
                "plan_id": self.plan_id,
                "plan_hash": self.plan_hash,
                "model_hash": self.model_hash,
                "artifact_hash": self.artifact_hash,
                "pre_render_report_hash": self.pre_render_report_hash,
                "post_render_report_hash": self.post_render_report_hash,
                "issue_codes": self.issue_codes,
            })
        return self


class ReworkDecision(BaseModel):
    """Bounded routing result for a document review issue."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    route: Literal["semantic_subgraph", "layout_binding_renderer", "human_review"]
    stage: Literal["pre_render", "post_render"]
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    unit_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    bounded: bool = True

    @model_validator(mode="after")
    def normalize(self) -> "ReworkDecision":
        self.unit_ids = _unique_strings(self.unit_ids)
        self.reason_codes = _unique_strings(self.reason_codes)
        return self


class DocumentReviewer:
    """Run deterministic semantic and artifact checks for one model hash."""

    def __init__(self, artifact_reviewer: ArtifactReviewer | None = None):
        self.artifact_reviewer = artifact_reviewer or ArtifactReviewer()

    def pre_render(
        self,
        plan: Any,
        document_model: Any,
        coverage_report: CoverageReport | Mapping[str, Any] | None,
        unit_reports: Mapping[str, UnitReviewResult | Mapping[str, Any]] | Sequence[UnitReviewResult | Mapping[str, Any]] | None,
    ) -> DocumentReviewReport:
        plan_id, plan_version, plan_hash = _plan_identity(plan)
        model_hash = str(_value(document_model, "model_hash", "") or "")
        issues: list[ReviewIssue] = []
        affected: set[str] = set()

        if str(_value(plan, "status", "accepted")) != "accepted":
            issues.append(_issue(
                "plan_not_accepted", severity="critical",
                suggested_action="accept_the_immutable_document_plan_before_render",
            ))
        if _value(document_model, "plan_id", "") and str(_value(document_model, "plan_id", "")) != plan_id:
            issues.append(_issue(
                "document_plan_identity_mismatch", severity="critical",
                suggested_action="aggregate_against_the_accepted_plan",
            ))
        if _value(document_model, "plan_hash", "") and str(_value(document_model, "plan_hash", "")) != plan_hash:
            issues.append(_issue(
                "document_plan_hash_mismatch", severity="critical",
                suggested_action="discard_the_stale_document_model",
            ))

        coverage = _coerce_coverage(coverage_report)
        requirements = _requirements(plan)
        if coverage is None:
            issues.append(_issue(
                "required_coverage_missing", severity="critical",
                suggested_action="evaluate_the_frozen_coverage_contract",
            ))
        else:
            if coverage.plan_id and coverage.plan_id != plan_id:
                issues.append(_issue(
                    "coverage_plan_identity_mismatch", severity="critical",
                    suggested_action="recompute_coverage_for_the_accepted_plan",
                ))
            if coverage.plan_hash and plan_hash and coverage.plan_hash != plan_hash:
                issues.append(_issue(
                    "coverage_plan_hash_mismatch", severity="critical",
                    suggested_action="recompute_coverage_for_the_accepted_plan",
                ))
            for requirement_id, requirement in requirements.items():
                result = coverage.requirement_results.get(requirement_id)
                if result is None:
                    if bool(_value(requirement, "required", True)):
                        issues.append(_issue(
                            "required_coverage_missing", severity="critical",
                            unit_id=_value(requirement, "unit_id", None),
                            suggested_action="evaluate_the_missing_requirement",
                        ))
                        affected.add(str(_value(requirement, "unit_id", "")))
                    continue
                result_status = str(result.status)
                if bool(_value(requirement, "required", True)) and result_status != "complete":
                    code = {
                        "missing": "required_coverage_missing",
                        "partial": "incomplete_coverage",
                        "unsupported": "unsupported_evidence",
                        "conflicting": "cross_unit_identity_conflict",
                    }.get(result_status, "required_coverage_missing")
                    missing_keys = list(getattr(result, "missing_row_keys", []) or [])
                    if missing_keys:
                        for row_key in missing_keys:
                            issues.append(_issue(
                                "required_row_missing", severity="critical",
                                unit_id=result.unit_id, row_key=row_key,
                                suggested_action="retrieve_and_draft_missing_row",
                            ))
                    else:
                        issues.append(_issue(
                            code, severity="critical", unit_id=result.unit_id,
                            suggested_action="rework_the_affected_unit",
                        ))
                    affected.add(result.unit_id)
                for result_issue in result.issues:
                    normalized = _as_stage_issue(result_issue, "pre_render")
                    issues.append(normalized)
                    if normalized.unit_id:
                        affected.add(normalized.unit_id)

        reports = _unit_report_map(unit_reports)
        for unit_id, report in reports.items():
            status = str(_value(report, "status", "blocked"))
            if status != "pass":
                unit_issues = _review_issue_values(report)
                if unit_issues:
                    issues.extend(_as_stage_issue(item, "pre_render") for item in unit_issues)
                else:
                    issues.append(_issue(
                        "unit_review_blocked", severity="critical", unit_id=unit_id,
                        suggested_action="rework_the_affected_unit",
                    ))
                affected.add(unit_id)

        issues.extend(self._model_semantic_checks(plan, document_model, requirements, affected))
        issues = _dedupe_issues(issues)
        status = _review_status(issues)
        return DocumentReviewReport(
            stage="pre_render", plan_id=plan_id, plan_version=plan_version,
            plan_hash=plan_hash, document_model_hash=model_hash, model_hash=model_hash,
            status=status, issues=issues, affected_unit_ids=sorted(affected),
        )

    def post_render(
        self,
        plan: Any,
        document_model: Any,
        render_result: Any,
        artifact_bytes: bytes,
    ) -> DocumentReviewReport:
        plan_id, plan_version, plan_hash = _plan_identity(plan)
        model_hash = str(_value(document_model, "model_hash", "") or "")
        artifact_report = self.artifact_reviewer.review(
            plan, document_model, render_result, artifact_bytes,
        )
        issues = _dedupe_issues(list(getattr(artifact_report, "issues", []) or []))
        artifact_hash = hashlib.sha256(bytes(artifact_bytes)).hexdigest()
        manifest = _render_manifest(render_result)
        manifest_hash = str(manifest.get("manifest_hash") or "") or None
        status = _review_status(issues)
        return DocumentReviewReport(
            stage="post_render", plan_id=plan_id, plan_version=plan_version,
            plan_hash=plan_hash, document_model_hash=model_hash, model_hash=model_hash,
            artifact_hash=artifact_hash, render_manifest_hash=manifest_hash,
            status=status, issues=issues,
            affected_unit_ids=sorted({issue.unit_id for issue in issues if issue.unit_id}),
        )

    @staticmethod
    def _model_semantic_checks(
        plan: Any,
        model: Any,
        requirements: Mapping[str, Any],
        affected: set[str],
    ) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        blocks = list(_value(model, "blocks", []) or [])
        block_units = [str(_value(block, "unit_id", "")) for block in blocks]
        if len(block_units) != len(set(block_units)):
            issues.append(_issue(
                "duplicate_semantic_unit", severity="critical",
                suggested_action="retain_one_block_for_each_frozen_semantic_unit",
            ))
        claim_locations: dict[str, list[str]] = {}
        for block in blocks:
            unit_id = str(_value(block, "unit_id", ""))
            citations = list(_value(block, "citations", []) or [])
            policy = _value(plan, "document_review_policy", {}) or {}
            if bool(policy.get("require_citations", False)) and not citations:
                issues.append(_issue(
                    "missing_citation", severity="critical", unit_id=unit_id,
                    suggested_action="attach_evidence_ids_to_the_accepted_draft",
                ))
                affected.add(unit_id)
            for citation in citations:
                claim_id = str(_value(citation, "claim_id", "") or "")
                if claim_id:
                    claim_locations.setdefault(claim_id, []).append(unit_id)
            for missing in list(_value(block, "missing_items", []) or []):
                if bool(_value(missing, "required", True)):
                    issues.append(_issue(
                        str(_value(missing, "issue_code", "missing_required_value")),
                        severity="critical", unit_id=unit_id,
                        row_key=_value(missing, "row_key", None),
                        column_id=_value(missing, "column_id", None),
                        suggested_action="resolve_the_required_missing_item",
                    ))
                    affected.add(unit_id)
            if str(_value(block, "kind", "")) == "table":
                issues.extend(_table_checks(block, requirements.get(unit_id), affected))
        for claim_id, units in claim_locations.items():
            if len(units) != len(set(units)):
                issues.append(_issue(
                    "duplicate_semantic_claim", severity="critical",
                    suggested_action="deduplicate_the_semantic_claim",
                    detail=claim_id,
                ))
        for model_issue in list(_value(model, "issues", []) or []):
            issue = _as_stage_issue(model_issue, "pre_render")
            issues.append(issue)
            if issue.unit_id:
                affected.add(issue.unit_id)
        return issues


class DocumentReleaseGate:
    """Fail-closed release decision for two independent review reports."""

    def evaluate(
        self,
        pre_render: DocumentReviewReport,
        post_render: DocumentReviewReport,
    ) -> ReleaseDecision:
        issue_codes: list[str] = []
        for report in (pre_render, post_render):
            issue_codes.extend(report.issue_codes)
        issue_codes = list(dict.fromkeys(issue_codes))
        mismatch = (
            pre_render.stage != "pre_render"
            or post_render.stage != "post_render"
            or pre_render.model_hash != post_render.model_hash
            or pre_render.plan_hash != post_render.plan_hash
            or (
                post_render.artifact_hash is None
                or not post_render.render_manifest_hash
            )
        )
        if mismatch:
            issue_codes.append("review_subject_mismatch")
        if mismatch or pre_render.blocking or post_render.blocking:
            status = "blocked"
            allowed = False
        elif issue_codes or pre_render.status != "pass" or post_render.status != "pass":
            status = "needs_review"
            allowed = False
        else:
            status = "release"
            allowed = True
        return ReleaseDecision(
            status=status, release_allowed=allowed,
            plan_id=post_render.plan_id or pre_render.plan_id,
            plan_hash=post_render.plan_hash or pre_render.plan_hash,
            model_hash=post_render.model_hash,
            artifact_hash=post_render.artifact_hash,
            pre_render_report_hash=pre_render.report_hash,
            post_render_report_hash=post_render.report_hash,
            issue_codes=issue_codes,
        )

    def can_release(self, pre_render: DocumentReviewReport, post_render: DocumentReviewReport) -> bool:
        return self.evaluate(pre_render, post_render).release_allowed


class DocumentReworkRouter:
    """Select one bounded rework route; unsafe issues go to a human."""

    def __init__(self, *, max_attempts: int = 3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts

    def route(self, report: DocumentReviewReport, *, attempt: int) -> ReworkDecision:
        if attempt < 1:
            raise ValueError("attempt must be positive")
        codes = report.issue_codes
        unit_ids = list(report.affected_unit_ids)
        if attempt >= self.max_attempts:
            return ReworkDecision(
                route="human_review", stage=report.stage, attempt=attempt,
                max_attempts=self.max_attempts, unit_ids=unit_ids,
                reason_codes=codes or ["rework_attempts_exhausted"],
            )
        if report.stage == "pre_render" and unit_ids and all(
            _is_semantic_rework_code(code) for code in codes
        ):
            return ReworkDecision(
                route="semantic_subgraph", stage=report.stage, attempt=attempt,
                max_attempts=self.max_attempts, unit_ids=unit_ids,
                reason_codes=codes,
            )
        if report.stage == "post_render" and codes and all(
            _is_layout_rework_code(code) for code in codes
        ):
            return ReworkDecision(
                route="layout_binding_renderer", stage=report.stage, attempt=attempt,
                max_attempts=self.max_attempts, unit_ids=unit_ids,
                reason_codes=codes,
            )
        return ReworkDecision(
            route="human_review", stage=report.stage, attempt=attempt,
            max_attempts=self.max_attempts, unit_ids=unit_ids,
            reason_codes=codes or ["unsafe_or_unlocalized_review_issue"],
        )


def _table_checks(block: Any, requirement: Any, affected: set[str]) -> list[ReviewIssue]:
    unit_id = str(_value(block, "unit_id", ""))
    rows = list(_value(block, "rows", []) or [])
    actual_keys = [str(_value(row, "row_key", "")).strip() for row in rows]
    issues: list[ReviewIssue] = []
    expected_keys = list(_value(requirement, "row_keys", []) or []) if requirement is not None else list(
        _value(block, "expected_row_keys", []) or []
    )
    required_columns = list(_value(requirement, "required_columns", []) or []) if requirement is not None else []
    if expected_keys:
        if any(not key for key in actual_keys):
            issues.append(_issue(
                "row_identity_missing", severity="critical", unit_id=unit_id,
                suggested_action="return_server_owned_row_keys",
            ))
        for key in expected_keys:
            if key not in actual_keys:
                issues.append(_issue(
                    "required_row_missing", severity="critical", unit_id=unit_id,
                    row_key=key, suggested_action="retrieve_and_draft_missing_row",
                ))
        if [key for key in actual_keys if key] != expected_keys:
            issues.append(_issue(
                "row_order_mismatch", severity="critical", unit_id=unit_id,
                suggested_action="sort_by_the_frozen_row_key_order",
            ))
    duplicates = {key for key in actual_keys if key and actual_keys.count(key) > 1}
    if duplicates:
        for key in sorted(duplicates):
            issues.append(_issue(
                "duplicate_row_key", severity="critical", unit_id=unit_id,
                row_key=key, suggested_action="deduplicate_row_identity",
            ))
    for row in rows:
        row_key = str(_value(row, "row_key", "")).strip() or None
        cells = dict(_value(row, "cells", {}) or {})
        missing = set(required_columns) - set(cells)
        for column in sorted(missing):
            issues.append(_issue(
                "missing_required_column", severity="critical", unit_id=unit_id,
                row_key=row_key, column_id=column,
                suggested_action="return_the_required_table_cell",
            ))
        if missing:
            affected.add(unit_id)
    if issues:
        affected.add(unit_id)
    return issues


def _coerce_coverage(value: Any) -> CoverageReport | None:
    if value is None:
        return None
    return value if isinstance(value, CoverageReport) else CoverageReport.model_validate(value)


def _requirements(plan: Any) -> dict[str, Any]:
    contract = _value(plan, "coverage_contract", None)
    values = _value(contract, "requirements", []) if contract is not None else []
    result: dict[str, Any] = {}
    for requirement in values or []:
        result[str(_value(requirement, "requirement_id", ""))] = requirement
    return result


def _unit_report_map(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    values = value.items() if isinstance(value, Mapping) else ((
        str(_value(item, "unit_id", "")), item
    ) for item in value)
    return {str(key).removeprefix("field:").removeprefix("review:"): report for key, report in values}


def _review_issue_values(report: Any) -> list[Any]:
    return list(_value(report, "issues", []) or [])


def _as_stage_issue(value: Any, stage: Literal["pre_render", "post_render"]) -> ReviewIssue:
    issue = value if isinstance(value, ReviewIssue) else ReviewIssue.model_validate(value)
    return issue.model_copy(update={"stage": stage})


def _issue(
    code: str,
    *,
    severity: str = "error",
    unit_id: Any = None,
    row_key: Any = None,
    column_id: Any = None,
    suggested_action: str = "",
    detail: str | None = None,
) -> ReviewIssue:
    # ``detail`` is intentionally not persisted: reports carry sanitized issue
    # facts only, never evidence text or template paths.
    del detail
    return ReviewIssue(
        stage="pre_render", code=code, severity=severity,
        unit_id=str(unit_id).strip() if unit_id else None,
        row_key=str(row_key).strip() if row_key else None,
        column_id=str(column_id).strip() if column_id else None,
        suggested_action=suggested_action,
    )


def _plan_identity(plan: Any) -> tuple[str, int | None, str]:
    return (
        str(_value(plan, "document_plan_id", _value(plan, "plan_id", "")) or ""),
        _value(plan, "version", _value(plan, "plan_version", None)),
        str(_value(plan, "plan_hash", "") or ""),
    )


def _render_manifest(render_result: Any) -> dict[str, Any]:
    value = _value(render_result, "integrity_manifest", {}) or {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _review_status(issues: Sequence[ReviewIssue]) -> Literal["pass", "rework", "needs_review", "blocked"]:
    if any(bool(issue.blocking) for issue in issues):
        return "blocked"
    return "needs_review" if issues else "pass"


def _is_semantic_rework_code(code: str) -> bool:
    return code in {
        "missing_required_value", "required_coverage_missing", "incomplete_coverage",
        "unsupported_evidence", "missing_citation", "cross_unit_identity_conflict",
        "duplicate_semantic_claim", "required_row_missing", "missing_required_column",
        "row_identity_missing", "duplicate_row_key", "row_order_mismatch",
        "unit_review_blocked", "draft_not_supported", "missing_evidence",
    }


def _is_layout_rework_code(code: str) -> bool:
    return code in {
        "wrong_physical_sheet", "wrong_physical_row", "wrong_physical_mapping",
        "non_allowlisted_cell", "overflow_or_truncation", "required_value_mismatch",
    }


def _unique_strings(values: Sequence[str]) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("review identifiers must be unique")
    if any(not value for value in normalized):
        raise ValueError("review identifiers must be non-empty")
    return normalized


def _dedupe_issues(issues: Sequence[ReviewIssue]) -> list[ReviewIssue]:
    result: list[ReviewIssue] = []
    seen: set[tuple[Any, ...]] = set()
    for issue in issues:
        key = (issue.code, issue.unit_id, issue.row_key, issue.column_id, issue.stage)
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


__all__ = [
    "DocumentReleaseGate",
    "DocumentReviewReport",
    "DocumentReviewer",
    "DocumentReworkRouter",
    "ReleaseDecision",
    "ReworkDecision",
]
