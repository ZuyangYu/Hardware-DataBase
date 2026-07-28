"""Safe, hash-bound structural contracts for template analysis."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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
        "fixed_label",
        "placeholder",
        "value",
        "table_header",
        "table_body",
        "layout_blank",
    ] = "unknown"


class TemplateAnalysisSuggestion(BaseModel):
    semantic_unit_id: str
    label: str
    target_unit_ids: list[str]
    retrieval_terms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    value_shape: Literal["scalar", "repeating_table"] = "scalar"


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
        for suggestion in self.suggestions:
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
