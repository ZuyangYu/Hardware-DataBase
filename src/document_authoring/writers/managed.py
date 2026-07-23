"""Safe built-in Managed Writer implementations.

The deterministic provider is deliberately modest: it is useful in tests and
offline deployments, but cannot fabricate content.  A model-backed provider
can implement the same WriterProvider protocol later without widening its
input privilege.
"""

from __future__ import annotations

import hashlib
from typing import Callable

from src.document_authoring.models import DocumentUnitDraft, DraftAssertion
from src.document_authoring.writers.provider import WriterRequest, WriterProvider


class ManagedWriter:
    """Small adapter wrapper that validates provider-level invariants."""

    def __init__(self, provider: WriterProvider):
        self.provider = provider

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        draft = self.provider.generate(request)
        if draft.unit_id != request.unit_id or draft.run_id != request.run_id:
            raise ValueError("writer returned a draft for a different unit or run")
        if draft.generated_by not in {"managed_writer", "external_agent"}:
            raise ValueError("managed writer returned an invalid generated_by value")
        return draft


class DeterministicEvidenceWriter:
    """Offline provider that summarizes one validated evidence occurrence."""

    provider_id = "deterministic_evidence_writer"

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        if not request.evidence:
            raise ValueError("managed writer requires validated evidence")
        primary = request.evidence[0]
        text = str(primary.get("content") or "").strip()
        evidence_id = str(primary.get("id") or "")
        if not text or not evidence_id:
            raise ValueError("validated evidence requires id and content")
        assertion_id = hashlib.sha256(f"{request.run_id}|{request.unit_id}|{evidence_id}".encode("utf-8")).hexdigest()[:20]
        return DocumentUnitDraft(
            unit_id=request.unit_id,
            run_id=request.run_id,
            generated_by="managed_writer",
            content=text,
            proposed_value=text,
            evidence_ids=[evidence_id],
            assertions=[DraftAssertion(
                assertion_id=f"assertion-{assertion_id}", text=text,
                claim_id=f"claim-{request.unit_id}", evidence_ids=[evidence_id],
                value=text, consistency_key=request.unit_id,
            )],
            proposed_status="draft",
        )


class CallableWriter:
    """Test/enterprise adapter for a controlled callable, without tool access."""

    provider_id = "callable_writer"

    def __init__(self, generate: Callable[[WriterRequest], DocumentUnitDraft]):
        self._generate = generate

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        return self._generate(request)
