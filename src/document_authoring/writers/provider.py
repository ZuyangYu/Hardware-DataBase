"""Provider-neutral Managed Writer contract.

Providers receive already validated evidence only.  They do not receive a
database handle, arbitrary paths, tool definitions, or raw source documents.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from src.document_authoring.models import DocumentUnitDraft


class WriterRequest(BaseModel):
    work_order_id: str
    run_id: str
    unit_id: str
    unit_label: str
    unit_description: str = ""
    field_value_type: str = "text"
    style: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    allowed_derivations: list[dict[str, Any]] = Field(default_factory=list)
    missing_or_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    prompt_version: str


class WriterProvider(Protocol):
    provider_id: str

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        """Return a structured Draft, never a binary document or FillPlan."""
