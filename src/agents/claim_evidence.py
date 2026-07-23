from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


CapabilityName = Literal[
    "entity_lookup",
    "relationship_lookup",
    "tabular_lookup",
    "document_claim_lookup",
    "revision_lookup",
]
ClaimOperation = Literal[
    "identity",
    "attribute",
    "relationship",
    "aggregation",
    "capability",
    "requirement",
    "revision",
    "explanation",
]


class EvidenceCapability(BaseModel):
    name: CapabilityName
    content_kinds: list[str] = Field(default_factory=list)
    direct_fact: bool
    supports_filters: set[str] = Field(default_factory=set)


class SourceCapability(BaseModel):
    processor_kind: str
    capabilities: list[EvidenceCapability] = Field(default_factory=list)


class Claim(BaseModel):
    id: str
    text: str
    operation: ClaimOperation
    subject_terms: list[str] = Field(default_factory=list)
    required_capabilities: list[CapabilityName] = Field(default_factory=list)
    support_mode: Literal["direct", "composite", "inference_allowed"] = "direct"
    required: bool = True
    project_id: str | None = None
    baseline_id: str | None = None
    source_version_scope: list[str] = Field(default_factory=list)
    expected_value_type: str | None = None
    expected_unit: str | None = None
    verification_policy_id: str | None = None


class InformationRequirement(BaseModel):
    """A bounded request for evidence, separate from any resolved claim."""

    requirement_id: str
    semantic_unit_id: str
    claim_type: ClaimOperation
    subject: str
    predicate: str | None = None
    object_hint: str | None = None
    required_capabilities: list[CapabilityName] = Field(default_factory=list)
    preferred_source_roles: list[str] = Field(default_factory=list)
    project_id: str | None = None
    baseline_id: str | None = None
    source_version_scope: list[str] = Field(default_factory=list)
    expected_value_type: str | None = None
    expected_unit: str | None = None
    verification_policy_id: str | None = None
    missing_policy: Literal["mark_tbd", "block_section", "optional"] = "mark_tbd"


class ClaimCoverage(BaseModel):
    claim_id: str
    status: Literal[
        "unsearched", "supported", "partial", "conflicting", "missing",
        "retrieval_failed", "access_denied", "source_unavailable", "requires_human",
    ]
    evidence_ids: list[str] = Field(default_factory=list)
    missing_capabilities: list[CapabilityName] = Field(default_factory=list)
    conflict_evidence_ids: list[str] = Field(default_factory=list)
    semantic_support: Literal["not_checked", "supported", "partial", "unsupported"] = "not_checked"
    scope_status: Literal["matched", "mismatched", "unknown"] = "unknown"
    revision_status: Literal["current", "outdated", "unknown"] = "unknown"
    authority_status: Literal["sufficient", "insufficient", "unknown"] = "unknown"
    independent_source_count: int = 0
    validation_notes: list[str] = Field(default_factory=list)


class AnswerAssertion(BaseModel):
    text: str
    claim_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    assertion_kind: Literal[
        "confirmed_fact",
        "document_statement",
        "derived_observation",
        "inference",
        "missing_information",
        "conflict",
    ]


class DraftAssertion(BaseModel):
    """A document field/review result before it becomes a rendered artifact."""

    assertion_id: str
    semantic_unit_id: str
    text: str
    value: Any | None = None
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["draft", "validated", "requires_human", "tbd"] = "draft"


class RetrievalSourceOutcome(BaseModel):
    source_version_id: str
    processing_artifact_id: str | None = None
    status: Literal[
        "success_with_hits", "success_empty", "source_unavailable", "retrieval_failed",
        "access_denied", "filter_unsupported",
    ]
    evidence_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class RetrievalOutcome(BaseModel):
    """Fail-closed retrieval result for a frozen document source set."""

    requirement_id: str
    status: Literal[
        "success_with_hits", "success_empty", "partial_failure", "retrieval_failed",
        "source_unavailable", "access_denied",
    ]
    evidences: list[Any] = Field(default_factory=list)
    source_outcomes: list[RetrievalSourceOutcome] = Field(default_factory=list)
    query_fingerprint: str
    applied_source_set_snapshot_id: str
    applied_region_policy_versions: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


_RELATIONSHIP_TERMS = (
    "连接",
    "接到",
    "连到",
    "网络",
    "网表",
    "引脚",
    "拓扑",
    "路径",
    "上游",
    "下游",
    "connect",
    "connection",
    "netlist",
    "topology",
    "path",
)
_TABULAR_TERMS = ("价格", "采购", "替代", "供应商", "数量", "用量", "bom", "price", "supplier", "quantity")
_REVISION_TERMS = ("版本", "生效", "当前", "日期", "时间", "revision", "effective", "latest", "current")
_CAPABILITY_TERMS = ("能力", "支持", "保护", "特性", "capability", "support", "protection", "feature")
_REQUIREMENT_TERMS = ("要求", "规范", "合规", "标准", "requirement", "compliance", "standard")
_EXPLANATION_TERMS = ("为什么", "原因", "影响", "如何工作", "why", "reason", "impact", "how does")
_AGGREGATION_TERMS = ("所有", "哪些", "列出", "总数", "合计", "all", "list", "count", "total")


def plan_claims(question: str, expected_evidence: Iterable[str] = ()) -> list[Claim]:
    """Create one generic, evidence-bearing claim for a question fragment."""

    text = str(question or "").strip()
    lowered = text.casefold()
    operation, capabilities, support_mode = _claim_requirements(lowered)
    expected = {str(item) for item in expected_evidence}
    if "circuit_design" in expected and "relationship_lookup" not in capabilities:
        capabilities.append("relationship_lookup")
    if "spreadsheet_table" in expected and "tabular_lookup" not in capabilities:
        capabilities.append("tabular_lookup")
    if "document_text" in expected and "document_claim_lookup" not in capabilities:
        capabilities.append("document_claim_lookup")
    if len(capabilities) > 1 and support_mode == "direct":
        support_mode = "composite"
    return [
        Claim(
            id="claim_1",
            text=text,
            operation=operation,
            subject_terms=_subject_terms(text),
            required_capabilities=capabilities,
            support_mode=support_mode,
        )
    ]


def _claim_requirements(text: str) -> tuple[ClaimOperation, list[CapabilityName], str]:
    if _contains_any(text, _REVISION_TERMS) and _contains_any(text, _TABULAR_TERMS):
        return "revision", ["tabular_lookup", "revision_lookup"], "composite"
    if _contains_any(text, _RELATIONSHIP_TERMS):
        return "relationship", ["relationship_lookup"], "direct"
    if _contains_any(text, _REQUIREMENT_TERMS):
        return "requirement", ["document_claim_lookup"], "direct"
    if _contains_any(text, _CAPABILITY_TERMS):
        return "capability", ["document_claim_lookup"], "direct"
    if _contains_any(text, _TABULAR_TERMS):
        return "aggregation", ["tabular_lookup"], "direct"
    if _contains_any(text, _REVISION_TERMS):
        return "revision", ["revision_lookup"], "direct"
    if _contains_any(text, _EXPLANATION_TERMS):
        return "explanation", ["document_claim_lookup"], "inference_allowed"
    if _contains_any(text, _AGGREGATION_TERMS):
        return "aggregation", ["entity_lookup"], "direct"
    return "identity", ["entity_lookup"], "direct"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _subject_terms(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]*|[\u4e00-\u9fff]{2,}", text)
        if len(token) > 1
    ][:12]
