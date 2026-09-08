"""Provider-neutral Managed Writer contract.

Providers receive already validated evidence only.  They do not receive a
database handle, arbitrary paths, tool definitions, or raw source documents.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from src.document_authoring.models import DocumentUnitDraft


class WriterRequest(BaseModel):
    work_order_id: str
    run_id: str
    unit_id: str
    unit_label: str
    unit_description: str = ""
    field_value_type: str = "text"
    table_columns: dict[str, str] | None = None
    # ``typed_rows`` is selected by the server for table fields.  A model is
    # never allowed to turn a table into a display scalar or a prose list.
    table_mode: Literal["scalar", "typed_rows"] = "scalar"
    expected_row_keys: list[str] = Field(default_factory=list)
    expected_columns: list[str] = Field(default_factory=list)
    row_key_schema: dict[str, Any] = Field(default_factory=dict)
    row_order: Literal["declared", "stable_key", "input"] = "input"
    duplicate_policy: Literal["reject", "allow"] = "reject"
    table_output_requirement: Literal["scalar", "typed_rows"] = "scalar"
    retrieval_query_terms: list[str] = Field(default_factory=list)
    style: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    allowed_derivations: list[dict[str, Any]] = Field(default_factory=list)
    missing_or_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    prompt_version: str

    @model_validator(mode="after")
    def normalize_table_contract(self) -> "WriterRequest":
        is_table = self.field_value_type.strip().casefold() in {"table", "repeating_table"}
        if is_table:
            if self.table_mode != "typed_rows" and self.table_mode != "scalar":
                raise ValueError("table requests require typed_rows mode")
            # Direct callers from the legacy boundary may omit the new mode;
            # table value types still fail closed against scalar output.
            if self.table_mode == "scalar":
                self.table_mode = "typed_rows"
            self.table_output_requirement = "typed_rows"
            if not self.expected_columns and self.table_columns:
                self.expected_columns = list(self.table_columns)
        elif self.table_mode == "typed_rows" or self.table_output_requirement == "typed_rows":
            raise ValueError("typed_rows mode is only valid for table requests")
        self.expected_row_keys = _unique_nonempty_or_empty(self.expected_row_keys, "expected_row_keys")
        self.expected_columns = _unique_nonempty_or_empty(self.expected_columns, "expected_columns")
        return self


class WriterProvider(Protocol):
    provider_id: str

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        """Return a structured Draft, never a binary document or FillPlan."""


def _unique_nonempty_or_empty(values: list[str], label: str) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError(f"{label} entries must be non-empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} entries must be unique")
    return normalized
