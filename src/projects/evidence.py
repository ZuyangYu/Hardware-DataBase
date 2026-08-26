"""Adapters into the single project/document evidence boundary contract."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from src.pipelines.document_rag.schemas import Evidence, EvidenceEnvelope


def evidence_occurrence_id(
    source_version_id: str | None,
    processing_artifact_id: str | None,
    locator: dict,
    content: str,
) -> str:
    payload = "|".join([
        source_version_id or "", processing_artifact_id or "", repr(sorted(locator.items())), content,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_evidence_envelope(
    evidence: Evidence,
    *,
    project_id: str,
    baseline_id: str | None,
    source_version_id: str,
    processing_artifact_id: str | None,
    locator: dict | None = None,
    document_role: str | None = None,
    module_scope: list[str] | None = None,
    revision: str | None = None,
    approval_status: str | None = None,
) -> EvidenceEnvelope:
    """Convert a pipeline-local Evidence without changing that pipeline type."""
    locator = locator or dict(evidence.metadata.get("locator") or {})
    content_hash = hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()
    occurrence_id = evidence_occurrence_id(source_version_id, processing_artifact_id, locator, evidence.content)
    return EvidenceEnvelope(
        id=occurrence_id,
        content=evidence.content,
        source_name=evidence.source_name,
        source_type=evidence.source_type,
        score=evidence.score,
        metadata=dict(evidence.metadata),
        backend=evidence.backend,
        retriever=evidence.retriever,
        project_id=project_id,
        baseline_id=baseline_id,
        source_version_id=source_version_id,
        processing_artifact_id=processing_artifact_id,
        document_role=document_role,
        module_scope=module_scope or [],
        revision=revision,
        approval_status=approval_status,
        locator=locator,
        content_hash=content_hash,
        retrieved_at=datetime.now(timezone.utc),
    )
