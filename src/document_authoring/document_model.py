"""Strict, format-neutral semantic document model.

The model contains renderable semantic values and references to evidence IDs,
never evidence text, source paths, template bytes, or renderer coordinates.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.document_authoring.models import TypedTableRow, content_hash
from src.document_authoring.planning.review_contracts import ReviewIssue


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_ids: list[str] = Field(default_factory=list)
    claim_id: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "Citation":
        self.evidence_ids = _unique_strings(self.evidence_ids, "citation evidence_ids")
        if self.claim_id is not None:
            self.claim_id = self.claim_id.strip() or None
        return self


class MissingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    unit_id: str
    reason: str
    row_key: str | None = None
    column_id: str | None = None
    required: bool = True
    issue_code: str = "missing_required_value"

    @model_validator(mode="after")
    def normalize(self) -> "MissingItem":
        self.unit_id = self.unit_id.strip()
        self.reason = self.reason.strip()
        self.issue_code = self.issue_code.strip()
        if not self.unit_id or not self.reason or not self.issue_code:
            raise ValueError("missing items require unit_id, reason and issue_code")
        for field_name in ("row_key", "column_id"):
            value = getattr(self, field_name)
            setattr(self, field_name, value.strip() if value is not None and value.strip() else None)
        return self


class _BlockBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    block_id: str
    unit_id: str
    citations: list[Citation] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    layout_hints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_identity(self):
        self.block_id = self.block_id.strip()
        self.unit_id = self.unit_id.strip()
        if not self.block_id or not self.unit_id:
            raise ValueError("document blocks require block_id and unit_id")
        _reject_raw_fields(self.layout_hints, path="layout_hints")
        return self


class SectionBlock(_BlockBase):
    kind: Literal["section"] = "section"
    title: str = ""
    content: str | None = None


class ParagraphBlock(_BlockBase):
    kind: Literal["paragraph"] = "paragraph"
    content: str


class ListBlock(_BlockBase):
    kind: Literal["list"] = "list"
    items: list[str] = Field(default_factory=list)


class TypedTableBlock(_BlockBase):
    kind: Literal["table"] = "table"
    columns: list[str] = Field(default_factory=list)
    rows: list[TypedTableRow] = Field(default_factory=list)
    expected_row_keys: list[str] = Field(default_factory=list)
    missing_cells: list[MissingItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_table_identity(self) -> "TypedTableBlock":
        self.columns = _unique_strings(self.columns, "table columns")
        self.expected_row_keys = _unique_strings(self.expected_row_keys, "expected row keys")
        if self.columns:
            allowed = set(self.columns)
            for row in self.rows:
                unknown = set(row.cells) - allowed
                if unknown:
                    raise ValueError(f"table row contains unknown columns: {sorted(unknown)}")
        row_keys = [row.row_key.strip() for row in self.rows]
        explicit_keys = [key for key in row_keys if key]
        if len(explicit_keys) != len(set(explicit_keys)):
            raise ValueError("table row keys must be unique")
        if self.expected_row_keys and any(not key for key in row_keys):
            raise ValueError("table rows require non-empty row keys")
        if self.expected_row_keys and set(row_keys) - set(self.expected_row_keys):
            raise ValueError("table contains row keys outside expected scope")
        return self


class CrossReferenceBlock(_BlockBase):
    kind: Literal["cross_reference"] = "cross_reference"
    references: list[str] = Field(default_factory=list)


Block: TypeAlias = Annotated[
    SectionBlock | ParagraphBlock | ListBlock | TypedTableBlock | CrossReferenceBlock,
    Field(discriminator="kind"),
]


class DocumentModel(BaseModel):
    """Canonical semantic document before format-specific rendering."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: str
    plan_id: str = ""
    plan_version: int | None = None
    plan_hash: str = ""
    blocks: list[Block] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    layout_hints: dict[str, Any] = Field(default_factory=dict)
    draft_hashes: dict[str, str] = Field(default_factory=dict)
    review_hashes: dict[str, str] = Field(default_factory=dict)
    issues: list[ReviewIssue] = Field(default_factory=list)
    model_hash: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "DocumentModel":
        self.document_id = self.document_id.strip()
        self.plan_id = self.plan_id.strip()
        self.plan_hash = self.plan_hash.strip()
        if not self.document_id:
            raise ValueError("document model requires document_id")
        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("document block ids must be unique")
        _reject_raw_fields(self.layout_hints, path="layout_hints")
        _reject_raw_fields(self.draft_hashes, path="draft_hashes")
        _reject_raw_fields(self.review_hashes, path="review_hashes")
        expected = content_hash(self.model_dump(mode="json", exclude={"model_hash"}))
        if self.model_hash and self.model_hash != expected:
            raise ValueError("document model hash does not match its contents")
        self.model_hash = expected
        return self


def _unique_strings(values: list[str], label: str) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError(f"{label} entries must be non-empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} entries must be unique")
    return normalized


def _reject_raw_fields(value: Any, *, path: str) -> None:
    forbidden = {
        "evidence_content", "raw_content", "source_path", "storage_ref",
        "template_bytes", "prompt", "credential", "password", "api_key",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in forbidden:
                raise ValueError(f"{path} contains forbidden field: {key}")
            _reject_raw_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_raw_fields(child, path=f"{path}[{index}]")

