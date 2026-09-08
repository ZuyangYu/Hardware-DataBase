"""Strict, serializable facts produced by plan-backed coverage/review stages."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.document_authoring.models import content_hash


class ReviewIssue(BaseModel):
    """A bounded review finding without evidence text or storage paths."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    issue_id: str = ""
    stage: Literal["coverage", "unit_review", "pre_render", "post_render"] = "unit_review"
    code: str
    severity: Literal["info", "warning", "error", "critical"] = "error"
    blocking: bool | None = None
    unit_id: str | None = None
    row_key: str | None = None
    column_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    suggested_action: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "ReviewIssue":
        self.code = self.code.strip()
        if not self.code:
            raise ValueError("review issues require a code")
        for field_name in ("unit_id", "row_key", "column_id"):
            value = getattr(self, field_name)
            setattr(self, field_name, value.strip() if value is not None and value.strip() else None)
        self.evidence_ids = _unique_strings(self.evidence_ids, "evidence_ids")
        if self.blocking is None:
            self.blocking = self.severity in {"error", "critical"}
        preimage = self.model_dump(mode="json", exclude={"issue_id"})
        expected_id = content_hash(preimage)
        # Callers may supply a domain-stable issue id.  Otherwise derive one
        # from the immutable facts so repeated evaluation is deterministic.
        self.issue_id = self.issue_id or expected_id
        return self


class CoverageRequirementResult(BaseModel):
    """Deterministic coverage outcome for one immutable requirement."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requirement_id: str
    unit_id: str
    kind: str
    required: bool = True
    status: Literal[
        "complete", "partial", "missing", "unsupported", "conflicting", "not_applicable",
    ]
    expected_count: int = 0
    covered_count: int = 0
    missing_count: int = 0
    unsupported_count: int = 0
    duplicate_count: int = 0
    missing_row_keys: list[str] = Field(default_factory=list)
    unexpected_row_keys: list[str] = Field(default_factory=list)
    missing_columns: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)
    result_hash: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "CoverageRequirementResult":
        self.evidence_ids = _unique_strings(self.evidence_ids, "evidence_ids")
        self.missing_row_keys = _unique_strings(self.missing_row_keys, "missing_row_keys")
        self.unexpected_row_keys = _unique_strings(self.unexpected_row_keys, "unexpected_row_keys")
        self.missing_columns = _unique_strings(self.missing_columns, "missing_columns")
        expected = content_hash(self.model_dump(mode="json", exclude={"result_hash"}))
        if self.result_hash and self.result_hash != expected:
            raise ValueError("coverage result hash does not match its contents")
        self.result_hash = expected
        return self


# The shorter name is convenient for callers that model this as a report row.
RequirementCoverageResult = CoverageRequirementResult


class CoverageReport(BaseModel):
    """Immutable coverage aggregate used by unit/document review gates."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plan_id: str = ""
    plan_version: int | None = None
    plan_hash: str = ""
    requirement_results: dict[str, CoverageRequirementResult] = Field(default_factory=dict)
    expected_count: int = 0
    covered_count: int = 0
    missing_count: int = 0
    unsupported_count: int = 0
    duplicate_count: int = 0
    report_hash: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "CoverageReport":
        expected = content_hash(self.model_dump(mode="json", exclude={"report_hash"}))
        if self.report_hash and self.report_hash != expected:
            raise ValueError("coverage report hash does not match its contents")
        self.report_hash = expected
        return self


class UnitReviewResult(BaseModel):
    """One immutable unit review attempt and its bounded routing decision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["pass", "rework", "needs_human", "blocked"]
    unit_id: str
    attempt: int = Field(ge=1)
    issues: list[ReviewIssue] = Field(default_factory=list)
    accepted_draft_hash: str | None = None
    report_hash: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "UnitReviewResult":
        self.unit_id = self.unit_id.strip()
        if not self.unit_id:
            raise ValueError("unit review requires a unit id")
        if self.status == "pass" and self.issues:
            raise ValueError("passed unit reviews may not contain issues")
        expected = content_hash(self.model_dump(mode="json", exclude={"report_hash"}))
        if self.report_hash and self.report_hash != expected:
            raise ValueError("unit review report hash does not match its contents")
        self.report_hash = expected
        return self


def safe_review_projection(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """Return a UI-safe projection containing only IDs, codes, hashes/statuses."""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    forbidden = {
        "content", "raw_content", "evidence_content", "path", "storage_ref",
        "template_bytes", "prompt", "credential", "password", "api_key",
    }

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): scrub(child)
                for key, child in item.items()
                if str(key).casefold() not in forbidden
            }
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    return scrub(payload)


def _unique_strings(values: list[str], label: str) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} entries must be unique")
    if any(not value for value in normalized):
        raise ValueError(f"{label} entries must be non-empty")
    return normalized
