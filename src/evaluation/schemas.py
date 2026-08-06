from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MetricStatus = Literal["success", "failed", "not_applicable"]
SnapshotStatus = Literal["success", "failed"]
RunStatus = Literal[
    "queued",
    "running",
    "pause_requested",
    "paused",
    "cancel_requested",
    "cancelled",
    "completed",
    "failed",
]
RunStage = Literal["idle", "collecting", "scoring", "reporting"]


class SampleRubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_facts: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    must_disclose_missing: bool = False
    must_disclose_conflicts: bool = False


class EvaluationSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    reference_contexts: list[str] = Field(default_factory=list)
    kb_name: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    required_evidence_types: list[str] = Field(default_factory=list)
    rubric: SampleRubric = Field(default_factory=SampleRubric)
    request_context: dict[str, Any] = Field(default_factory=dict)
    critical: bool = False

    @field_validator("id", "question", "reference_answer", "kb_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("request_context")
    @classmethod
    def context_is_allowlisted(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "user_id",
            "session_id",
            "department_id",
            "roles",
            "allowed_kbs",
            "kb_permissions",
        }
        unsupported = sorted(set(value) - allowed)
        if unsupported:
            raise ValueError(f"request_context contains unsupported fields: {', '.join(unsupported)}")
        return value

    @model_validator(mode="after")
    def validate_metric_inputs(self):
        if "context_recall" in self.metrics and not self.reference_contexts:
            raise ValueError("reference_contexts are required for context_recall")
        return self


class DocumentGenerationEvalRecord(BaseModel):
    """One expected field result for a controlled document template."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    template_fixture: str = Field(min_length=1)
    field_id: str = Field(min_length=1)
    expected_value: str = Field(min_length=1)
    allowed_sources: list[str] = Field(min_length=1)
    required: bool = True
    critical: bool = True

    @field_validator("id", "template_fixture", "field_id", "expected_value")
    @classmethod
    def strip_document_generation_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("allowed_sources")
    @classmethod
    def validate_allowed_sources(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_sources must be unique")
        if not normalized:
            raise ValueError("allowed_sources must not be empty")
        return normalized


class AnswerSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    question: str
    kb_name: str
    response: str = ""
    scored_response: str = ""
    retrieved_contexts: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_summary: dict[str, Any] = Field(default_factory=dict)
    status: SnapshotStatus = "success"
    error_stage: str = ""
    error_message: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentGenerationSnapshot(BaseModel):
    """Observed output needed to score a document-generation eval record."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str
    template_fixture: str
    mapped_field_id: str | None = None
    filled_value: str | None = None
    evidence_sources: list[str] = Field(default_factory=list)
    retrieved_evidence_sources: list[str] = Field(default_factory=list)
    attempted_fill_count: int = Field(default=0, ge=0)
    fixed_content_overwrite_count: int = Field(default=0, ge=0)
    source_scope_violation_count: int = Field(default=0, ge=0)
    unsupported_required_field_fill_count: int = Field(default=0, ge=0)
    auto_approved: bool = False


class MetricResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    metric_name: str
    score: float | None = None
    status: MetricStatus = "success"
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class SampleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    question: str = ""
    reference_answer: str = ""
    response: str = ""
    scored_response: str = ""
    retrieved_contexts: list[str] = Field(default_factory=list)
    critical: bool = False
    snapshot_status: SnapshotStatus = "success"
    metrics: list[MetricResult] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    exit_code: int = 0
    metric_scores: dict[str, float] = Field(default_factory=dict)
    metric_counts: dict[str, int] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sample_count: int = 0
    successful_samples: int = 0
    failed_samples: int = 0
    metric_scores: dict[str, float] = Field(default_factory=dict)
    metric_counts: dict[str, int] = Field(default_factory=dict)
    metric_failures: dict[str, int] = Field(default_factory=dict)
    gate: GateResult | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationRunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    dataset_path: str
    snapshot_path: str
    mode: Literal["online", "offline"]
    score_enabled: bool
    sample_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: RunStatus = "queued"
    stage: RunStage = "idle"
    total_samples: int = 0
    completed_samples: int = 0
    successful_samples: int = 0
    failed_samples: int = 0
    scoring_completed_groups: int = 0
    scoring_total_groups: int = 0
    current_sample_id: str = ""
    current_question: str = ""
    started_at: str = ""
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""
    error_message: str = ""
    report_path: str = ""

    @classmethod
    def new_online(
        cls,
        *,
        run_id: str,
        dataset_path: str,
        snapshot_path: str,
        total_samples: int,
        score_enabled: bool,
        sample_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> "EvaluationRunState":
        return cls(
            run_id=run_id,
            dataset_path=dataset_path,
            snapshot_path=snapshot_path,
            mode="online",
            score_enabled=score_enabled,
            total_samples=total_samples,
            sample_ids=sample_ids or [],
            tags=tags or [],
        )
