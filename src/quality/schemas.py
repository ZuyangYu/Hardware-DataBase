from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReasonCode = Literal[
    "factual_error", "unsupported_evidence", "not_found", "version_conflict",
    "incomplete_task", "format_mismatch",
]


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assistant_message_id: int = Field(gt=0)
    answer_revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    client_request_id: str = Field(min_length=1, max_length=128)
    rating: Literal["helpful", "unhelpful", "withdrawn"]
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=6)
    comment: str = Field(default="", max_length=4000)
    share_with_support: bool = False
    support_contact: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def valid_reasons(self):
        if self.rating != "unhelpful" and (self.share_with_support or self.support_contact):
            raise ValueError("support material requires unhelpful feedback")
        if self.support_contact and not self.share_with_support:
            raise ValueError("support contact requires ticket consent")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        if self.rating != "unhelpful" and self.reason_codes:
            raise ValueError("reason_codes require unhelpful feedback")
        if self.rating == "withdrawn" and self.comment:
            raise ValueError("withdrawn feedback cannot contain a comment")
        return self


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int = Field(ge=1)
    reviewer_user_ids: list[int] = Field(min_length=1, max_length=20)
    fields: list[Literal["query", "answer", "comment"]] = Field(min_length=1, max_length=3)
    evidence_indices: list[int] = Field(default_factory=list, max_length=100)
    retention_days: int = Field(default=7, ge=1, le=30)


class ReviewRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=128)
    message: str = Field(default="", max_length=1000)


class CaseVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int = Field(ge=1)


class ReviewRequestReject(CaseVersionRequest):
    decision_note: str = Field(default="", max_length=1000)


class ReviewRequest(CaseVersionRequest):
    client_request_id: str = Field(min_length=1, max_length=128)
    judgment: Literal["confirmed", "not_an_error", "needs_information"]
    correctness: Literal["correct", "incorrect", "not_evaluated"] = "not_evaluated"
    root_cause: Literal[
        "retrieval", "parsing", "missing_source", "prompt", "model", "tool",
        "permission", "presentation", "unknown",
    ] = "unknown"
    reference_answer: str = Field(default="", max_length=20000)
    notes: str = Field(default="", max_length=4000)
    rubric: str = Field(default="", max_length=4000)
    supersedes: str | None = Field(default=None, max_length=64)
