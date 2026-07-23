"""Safe built-in Managed Writer implementations.

The deterministic provider is deliberately modest: it is useful in tests and
offline deployments, but cannot fabricate content.  A model-backed provider
can implement the same WriterProvider protocol later without widening its
input privilege.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable

from src.core.llm_client import LLMClient
from src.document_authoring.models import DocumentUnitDraft, DraftAssertion
from src.document_authoring.validator import DocumentValidator
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


class LLMManagedWriter:
    """Constrained document draft provider backed by the shared chat client."""

    provider_id = "llm_managed_writer"

    def __init__(self, client: LLMClient | None = None):
        # Constructing without a config deliberately reuses the same AGENT_*
        # runtime configuration as intelligent chat.
        self._client = client or LLMClient()
        self._validator = DocumentValidator()

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        response = self._client.chat(
            [
                {"role": "system", "content": _WRITER_SYSTEM_PROMPT},
                {"role": "user", "content": request.model_dump_json()},
            ],
            usage_stage="document_authoring",
        )
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ValueError("managed writer returned malformed JSON") from exc
        if not isinstance(payload, dict) or set(payload) != _DRAFT_FIELDS:
            raise ValueError("managed writer returned unsupported draft fields")
        try:
            draft = DocumentUnitDraft.model_validate(payload)
        except Exception as exc:
            raise ValueError("managed writer returned malformed draft") from exc
        if draft.unit_id != request.unit_id or draft.run_id != request.run_id:
            raise ValueError("managed writer returned a draft for a different unit or run")
        if (
            draft.generated_by != "managed_writer"
            or not draft.content
            or not draft.assertions
            or draft.validation_status != "pending"
            or draft.validation_notes
        ):
            raise ValueError("managed writer returned an unsupported draft")
        evidence = {str(item.get("id") or ""): item for item in request.evidence}
        if not set(draft.evidence_ids) or not set(draft.evidence_ids) <= set(evidence):
            raise ValueError("managed writer draft is not grounded in supplied evidence")
        validated = self._validator.validate_unit_draft(draft, evidence)
        if validated.validation_status != "supported":
            raise ValueError("managed writer returned an ungrounded draft")
        return draft


_DRAFT_FIELDS = {
    "unit_id", "run_id", "generated_by", "content", "proposed_value",
    "assertions", "evidence_ids", "proposed_status", "validation_status", "validation_notes",
}
_WRITER_SYSTEM_PROMPT = """Create one evidence-grounded document draft.
Return only JSON with exactly these keys: unit_id, run_id, generated_by, content,
proposed_value, assertions, evidence_ids, proposed_status, validation_status,
validation_notes. Use generated_by=managed_writer. Every assertion must cite only
provided evidence_ids and include wording lexically anchored in its cited evidence.
Do not invent facts, tools, locations, sources, or file content."""


class CallableWriter:
    """Test/enterprise adapter for a controlled callable, without tool access."""

    provider_id = "callable_writer"

    def __init__(self, generate: Callable[[WriterRequest], DocumentUnitDraft]):
        self._generate = generate

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        return self._generate(request)
