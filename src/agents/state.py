from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from src.agents.claim_evidence import Claim


class SubQuestion(BaseModel):
    id: str
    question: str
    expected_evidence: list[str] = Field(default_factory=list)


class QuestionAnalysis(BaseModel):
    intent: str = "general_question"
    summary: str
    reasoning_summary: str = ""
    entities: list[str] = Field(default_factory=list)
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    # 对标 Google Agentic RAG Orchestrator：识别该问题是否需要多步/跨源推理
    # （如先查项目文档拿到 server ID，再去另一源查规格）。多跳查询允许跑到
    # AGENT_MAX_RETRIEVAL_ROUNDS 上限迭代检索。
    multi_hop: bool = False


class CatalogSource(BaseModel):
    record_id: int | None = None
    document_name: str
    original_file_name: str = ""
    processor_kind: str = ""
    content_kind: str = ""
    dataset_kind: str = ""
    source_group: str = ""
    status: str = ""
    local_path: str = ""
    file_size: int = 0
    profile: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallPlan(BaseModel):
    tool_name: str
    query: str
    reason: str = ""
    top_k: int = 5
    filters: dict[str, Any] = Field(default_factory=dict)


class SourcePlanItem(BaseModel):
    source_name: str
    processor_kind: str
    reason: str
    tool_calls: list[ToolCallPlan] = Field(default_factory=list)


class SourcePlan(BaseModel):
    source_plan: list[SourcePlanItem] = Field(default_factory=list)
    skipped_sources: list[dict[str, str]] = Field(default_factory=list)


class Evidence(BaseModel):
    id: str
    content: str
    source_name: str
    content_kind: str
    processor_kind: str
    score: float = 0.0
    locator: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceQuality(BaseModel):
    evidence_id: str
    score: float = 0.0
    source_scope_match: bool = False
    evidence_type_match: bool = False
    token_overlap: int = 0
    matched_sub_questions: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class CoverageItem(BaseModel):
    sub_question_id: str
    sub_question: str
    coverage_score: float = 0.0
    status: Literal["covered", "partial", "weak", "missing"] = "missing"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class CoverageMatrix(BaseModel):
    coverage: list[CoverageItem] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    recommended_followups: list[ToolCallPlan] = Field(default_factory=list)


class RetrievalLedgerItem(BaseModel):
    sub_question_id: str
    sub_question: str
    expected_evidence: list[str] = Field(default_factory=list)
    status: Literal["covered", "partial", "weak", "missing"] = "missing"
    searched_tools: list[str] = Field(default_factory=list)
    searched_sources: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    supporting_sources: list[str] = Field(default_factory=list)
    missing_evidence_types: list[str] = Field(default_factory=list)
    unsearched_relevant_sources: list[str] = Field(default_factory=list)
    gap_feedback: str = ""


class SufficiencyDecision(BaseModel):
    status: Literal[
        "sufficient",
        "partial_but_answerable",
        "insufficient_need_more",
    ]
    reason: str
    # LLM 判断出的缺失信息（用于 compose_answer 诚实降级与 footer 展示）。
    missing: list[str] = Field(default_factory=list)
    # 多跳关键：LLM 基于第一轮证据里发现的实体/缺口，产出下一轮新检索查询。
    # 每项 {query, tool_name, source_name?, reason}。
    suggested_queries: list[dict[str, Any]] = Field(default_factory=list)


class GroundingVerification(BaseModel):
    grounded: bool = True
    unsupported_claims: list[dict[str, str]] = Field(default_factory=list)
    weak_claims: list[dict[str, str]] = Field(default_factory=list)
    citation_coverage: float = 0.0


class AgentState(TypedDict, total=False):
    thread_id: str
    kb_name: str
    user_query: str
    query_mode: Literal["fast", "deep"]
    history: list[tuple[str, str]]
    ctx: dict[str, Any]
    _ctx_obj: Any

    route_decision: dict[str, Any]
    question_analysis: dict[str, Any]
    claim_coverage: list[dict[str, Any]]

    catalog: dict[str, Any]
    source_plan: dict[str, Any]

    retrieval_round: int
    evidence: list[dict[str, Any]]
    merged_evidence: list[dict[str, Any]]
    evidence_quality: list[dict[str, Any]]
    retrieval_diagnostics: list[dict[str, Any]]
    retrieval_ledger: list[dict[str, Any]]
    coverage_matrix: dict[str, Any]
    sufficiency: dict[str, Any]
    intermediate_answer: str
    # plan_next_retrieval 产出的下一轮 tool calls（多跳动态重规划）。
    next_retrieval_calls: list[dict[str, Any]]

    answer: str
    verification: dict[str, Any]

    trace: list[dict[str, Any]]
    final_response: str
