"""Evidence capability model describing what a structured source can answer.

Used by ``src/ingestion/parser_registry.py`` (domain manifests) and
``src/test_data/manifest.py``; consumed by circuit datasheet follow-up logic
and future planning code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


CapabilityName = Literal[
    "entity_lookup",
    "relationship_lookup",
    "tabular_lookup",
    "document_claim_lookup",
    "revision_lookup",
]


class EvidenceCapability(BaseModel):
    name: CapabilityName
    content_kinds: list[str] = Field(default_factory=list)
    direct_fact: bool
    supports_filters: set[str] = Field(default_factory=set)
