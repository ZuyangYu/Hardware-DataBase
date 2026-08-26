"""Safe, hash-bound structural contracts for template analysis."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


_WORKBOOK_CELL_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
_MAX_WORKBOOK_COLUMN = 16_384  # XFD
_MAX_WORKBOOK_ROW = 1_048_576


def workbook_cell_coordinates(reference: str) -> tuple[int, int]:
    """Parse one canonical, in-bounds Excel A1 cell reference."""
    match = _WORKBOOK_CELL_RE.fullmatch(reference)
    if match is None:
        raise ValueError(f"not a valid Excel A1 reference: {reference}")
    column = 0
    for char in match.group(1):
        column = column * 26 + ord(char) - ord("A") + 1
    row = int(match.group(2))
    if column > _MAX_WORKBOOK_COLUMN or row > _MAX_WORKBOOK_ROW:
        raise ValueError(f"not a valid Excel A1 reference: {reference}")
    return column, row


def workbook_value_hash(value: str | None) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TemplateNeighbor(BaseModel):
    relative_row: int
    relative_column: int
    value_preview: str


class TemplateAnalysisUnit(BaseModel):
    unit_id: str
    locator: dict[str, Any]
    label: str = ""
    writable: bool = False
    blocked_reason: str | None = None
    value_preview: str | None = None
    value_hash: str | None = None
    value_kind: Literal["blank", "text", "number", "boolean", "formula", "error"] = "blank"
    style_fingerprint: str = ""
    neighborhood: list[TemplateNeighbor] = Field(default_factory=list)
    structural_role_hint: Literal[
        "unknown",
        "section_header",
        "fixed_label",
        "placeholder",
        "sample_value",
        "scalar_input",
        "value",
        "table_header",
        "table_body",
        "layout_blank",
    ] = "unknown"
    candidate_for_auto_fill: bool = False


class TemplateAnalysisSuggestion(BaseModel):
    semantic_unit_id: str
    label: str
    target_unit_ids: list[str]
    retrieval_terms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    value_shape: Literal["scalar", "repeating_table"] = "scalar"
    overwrite_basis: Literal["placeholder", "sample_value"] | None = None


class TemplateRiskMetrics(BaseModel):
    total_unit_count: int = 0
    target_count: int = 0
    nonempty_target_count: int = 0
    target_ratio: float = 0.0
    nonempty_overwrite_ratio: float = 0.0
    min_confidence: float | None = None


class TemplateActivationDecision(BaseModel):
    status: Literal["auto_accepted", "requires_human"]
    reason_codes: list[str] = Field(default_factory=list)
    suggestion_ids: list[str] = Field(default_factory=list)
    metrics: TemplateRiskMetrics = Field(default_factory=TemplateRiskMetrics)


class TemplateMappingCorrection(BaseModel):
    analysis_id: str
    expected_content_hash: str
    suggestions: list[TemplateAnalysisSuggestion]
    locked_unit_ids: list[str] = Field(default_factory=list)
    approved_overwrite_unit_ids: list[str] = Field(default_factory=list)
    actor_id: str
    comment: str = Field(min_length=1)


class TemplateAnalysis(BaseModel):
    analysis_id: str
    template_version_id: str
    content_hash: str
    format: Literal["xlsx", "xlsm", "docx"]
    status: Literal["ready_for_confirmation", "requires_human", "failed"]
    units: list[TemplateAnalysisUnit]
    suggestions: list[TemplateAnalysisSuggestion] = Field(default_factory=list)
    activation_decision: TemplateActivationDecision | None = None
    human_confirmed_target_unit_ids: list[str] = Field(default_factory=list)
    approved_overwrite_unit_ids: list[str] = Field(default_factory=list)
    locked_unit_ids: list[str] = Field(default_factory=list)
    correction_actor_id: str | None = None
    correction_comment: str | None = None
    mapping_conflict_unit_ids: list[str] = Field(default_factory=list)

    def validate_suggestion_targets(self) -> None:
        units = {unit.unit_id: unit for unit in self.units}
        for suggestion in self.suggestions:
            for unit_id in suggestion.target_unit_ids:
                if unit_id not in units:
                    raise ValueError(f"suggestion references unknown analysis unit: {unit_id}")
                if not units[unit_id].writable:
                    raise PermissionError(f"suggestion targets non-writable analysis unit: {unit_id}")

    def validate_suggestions(self) -> None:
        self.validate_suggestion_targets()
        seen_targets: set[str] = set()
        seen_semantic_unit_ids: set[str] = set()
        for suggestion in self.suggestions:
            if suggestion.semantic_unit_id in seen_semantic_unit_ids:
                raise ValueError(
                    "semantic unit may only be suggested once: "
                    f"{suggestion.semantic_unit_id}"
                )
            seen_semantic_unit_ids.add(suggestion.semantic_unit_id)
            for unit_id in suggestion.target_unit_ids:
                if unit_id in seen_targets:
                    raise ValueError(f"suggestion target may only be used once: {unit_id}")
                seen_targets.add(unit_id)


class DocxRegionSchema(BaseModel):
    """An allowlisted DOCX location; protected roles cannot be machine writable."""

    region_id: str
    locator: dict[str, Any]
    role: Literal[
        "locked_template", "project_metadata", "evidence_derived", "semantic_draft",
        "formula", "human_input", "human_approval", "legacy_example",
    ]
    write_policy: Literal["never", "deterministic_only", "validated_draft", "human_only"]
    value_type: str | None = None

    @model_validator(mode="after")
    def reject_writable_protected_roles(self):
        protected_roles = {
            "locked_template", "formula", "human_input", "human_approval", "legacy_example",
        }
        if self.role in protected_roles and self.write_policy != "never":
            raise ValueError("protected DOCX regions may not be writable")
        return self
