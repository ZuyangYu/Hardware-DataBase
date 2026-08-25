"""Cross-layer agent data models shared by tools, circuit and services.

These are the evidence/catalog shapes that flow from retrieval adapters into
the runner's summaries, query traces, and the API layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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


class Evidence(BaseModel):
    id: str
    content: str
    source_name: str
    content_kind: str
    processor_kind: str
    score: float = 0.0
    locator: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
