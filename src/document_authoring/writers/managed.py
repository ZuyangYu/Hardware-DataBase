"""Safe built-in Managed Writer implementations.

The deterministic provider is deliberately modest: it is useful in tests and
offline deployments, but cannot fabricate content.  A model-backed provider
can implement the same WriterProvider protocol later without widening its
input privilege.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable

from src.core.llm_client import LLMClient
from src.document_authoring.models import DocumentUnitDraft, DraftAssertion
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.writers.provider import WriterRequest, WriterProvider


logger = logging.getLogger(__name__)


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
        return _deterministic_draft(request)


def _deterministic_draft(request: WriterRequest) -> DocumentUnitDraft:
    if not request.evidence:
        raise ValueError("managed writer requires validated evidence")
    items: list[tuple[str, str]] = []
    for evidence in request.evidence:
        text = str(evidence.get("content") or "").strip()
        evidence_id = str(evidence.get("id") or "")
        if not text or not evidence_id:
            raise ValueError("validated evidence requires id and content")
        items.append((evidence_id, text))
    if len(items) == 1:
        # Single evidence: present it verbatim (there is nothing to summarize).
        evidence_id, text = items[0]
        body = text
        evidence_ids = [evidence_id]
    else:
        # Multiple evidence: structured summary of ALL evidence (no fabrication)
        # by enumerating each chunk. One summary assertion references every
        # evidence id so the deterministic validator's lexical-anchor check
        # holds (the body contains each chunk's content) and no intra-unit
        # cross-unit conflict is triggered (a single consistency_key/value).
        body = "\n".join(f"[{i + 1}] {text}" for i, (_, text) in enumerate(items))
        evidence_ids = [evidence_id for evidence_id, _ in items]
    assertion_id = hashlib.sha256(
        f"{request.run_id}|{request.unit_id}|{'|'.join(evidence_ids)}".encode("utf-8")
    ).hexdigest()[:20]
    return DocumentUnitDraft(
        unit_id=request.unit_id,
        run_id=request.run_id,
        generated_by="managed_writer",
        content=body,
        proposed_value=body,
        evidence_ids=evidence_ids,
        assertions=[DraftAssertion(
            assertion_id=f"assertion-{assertion_id}", text=body,
            claim_id=f"claim-{request.unit_id}", evidence_ids=evidence_ids,
            value=body, consistency_key=request.unit_id,
        )],
        proposed_status="draft",
    )


class LLMManagedWriter:
    """Constrained document draft provider backed by the shared chat client.

    Robustness strategy:

    1. The LLM is asked with a strict schema + concrete example filled with
       the exact request values, so the model can copy the boilerplate rather
       than re-derive it.
    2. If the LLM response fails validation, retry once with a targeted
       error-feedback message before giving up.
    3. On persistent LLM failure, fall back to the deterministic evidence
       writer so an offline-safe evidence-grounded draft is still produced.
       The user sees a completed run instead of "generation failed"; every
       fallback is logged for audit.
    """

    provider_id = "llm_managed_writer"

    # Ceiling on how many times the LLM may be re-prompted with feedback.
    _MAX_LLM_ATTEMPTS = 2

    def __init__(self, client: LLMClient | None = None):
        # Constructing without a config deliberately reuses the same AGENT_*
        # runtime configuration as intelligent chat.
        self._client = client or LLMClient()
        self._validator = DocumentValidator()

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        last_error: str | None = None
        for attempt in range(1, self._MAX_LLM_ATTEMPTS + 1):
            user_content = _build_user_prompt(request, last_error)
            response = self._client.chat(
                [
                    {"role": "system", "content": _WRITER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                usage_stage="document_authoring",
            )
            try:
                draft = self._parse_and_validate(response, request)
                return draft
            except ValueError as exc:
                last_error = str(exc)
                logger.warning(
                    "LLMManagedWriter attempt %d/%d failed for unit %s: %s",
                    attempt, self._MAX_LLM_ATTEMPTS, request.unit_id, exc,
                )

        # Persistent LLM failure → fall back to the deterministic writer.
        # This is safer than failing the whole generation run because the
        # evidence has already been validated and the deterministic writer
        # only copies that evidence verbatim.
        logger.warning(
            "LLMManagedWriter falling back to deterministic writer for unit %s: %s",
            request.unit_id, last_error,
        )
        return _deterministic_draft(request)

    def _parse_and_validate(self, response: str, request: WriterRequest) -> DocumentUnitDraft:
        cleaned = _strip_code_fences(response)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"managed writer returned malformed JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("managed writer response must be a JSON object")
        missing = _DRAFT_FIELDS - set(payload)
        extra = set(payload) - _DRAFT_FIELDS
        if missing or extra:
            raise ValueError(
                f"managed writer returned unsupported draft fields "
                f"(missing={sorted(missing)}, extra={sorted(extra)})"
            )
        try:
            draft = DocumentUnitDraft.model_validate(payload)
        except Exception as exc:
            raise ValueError(f"managed writer returned malformed draft: {exc}") from exc
        if draft.unit_id != request.unit_id or draft.run_id != request.run_id:
            raise ValueError(
                f"managed writer returned a draft for a different unit or run "
                f"(got unit={draft.unit_id!r} run={draft.run_id!r}, "
                f"expected unit={request.unit_id!r} run={request.run_id!r})"
            )
        if (
            draft.generated_by != "managed_writer"
            or not draft.content
            or not draft.assertions
            or draft.validation_status != "pending"
            or draft.validation_notes
        ):
            raise ValueError(
                f"managed writer returned an unsupported draft "
                f"(generated_by={draft.generated_by!r}, "
                f"content={'set' if draft.content else 'empty'}, "
                f"assertions={len(draft.assertions)}, "
                f"validation_status={draft.validation_status!r}, "
                f"validation_notes={draft.validation_notes})"
            )
        evidence = {str(item.get("id") or ""): item for item in request.evidence}
        if not set(draft.evidence_ids) or not set(draft.evidence_ids) <= set(evidence):
            raise ValueError(
                f"managed writer draft is not grounded in supplied evidence "
                f"(draft_evidence_ids={draft.evidence_ids}, "
                f"available_evidence_ids={sorted(evidence)})"
            )
        validated = self._validator.validate_unit_draft(draft, evidence)
        if validated.validation_status != "supported":
            raise ValueError(
                f"managed writer returned an ungrounded draft "
                f"(validator_status={validated.validation_status!r}, "
                f"notes={validated.validation_notes})"
            )
        return draft


_DRAFT_FIELDS = {
    "unit_id", "run_id", "generated_by", "content", "proposed_value",
    "assertions", "evidence_ids", "proposed_status", "validation_status", "validation_notes",
}


_WRITER_SYSTEM_PROMPT = """You are the Managed Writer for a governed document authoring pipeline.

Return ONE JSON object, and ONLY that object — no prose, no code fences.

Top-level keys (exactly these ten, no others):
  unit_id             string   — copy verbatim from the request
  run_id              string   — copy verbatim from the request
  generated_by        string   — must be exactly "managed_writer"
  content             string   — the drafted body text, non-empty
  proposed_value      string   — usually the same as `content`
  assertions          array    — non-empty; each item is an object with:
      assertion_id       string   — a stable id you generate (e.g. "assertion-<unit_id>-1")
      text               string   — a sentence lexically anchored in the cited evidence
      claim_id           string   — e.g. "claim-<unit_id>"
      evidence_ids       array of strings — MUST reference ids present in the request evidence
      value              any      — optional; usually a repeat of the drafted value
      consistency_key    string   — usually the unit_id
      assertion_kind     string   — one of: "confirmed_fact", "document_statement",
                                    "derived_observation", "inference",
                                    "missing_information", "conflict"; default "document_statement"
  evidence_ids        array of strings — MUST be a subset of the request evidence ids
  proposed_status     string   — use "draft"
  validation_status   string   — MUST be exactly "pending"
  validation_notes    array    — MUST be an empty list []

Rules:
- Copy `unit_id` and `run_id` verbatim from the request; do NOT invent them.
- Every `evidence_ids` entry (top-level and inside assertions) must exist in
  the request `evidence[].id`. Do NOT fabricate ids.
- Each `assertions[].text` must reuse concrete wording from the cited
  evidence (a subphrase or key noun/number from the evidence content).
- Do NOT invent facts, tools, locations, sources, or file content.
- Output ONLY the JSON object. No markdown fences, no explanation."""


def _build_user_prompt(request: WriterRequest, last_error: str | None) -> str:
    """Build the user prompt with a concrete example based on the request."""
    example = _example_from_request(request)
    parts = [
        "Request:",
        request.model_dump_json(),
        "",
        "Fill in the JSON below with content grounded in the evidence above. "
        "Copy unit_id/run_id verbatim. Use only listed evidence ids.",
        "",
        "Example structure (copy the shape, fill with real content):",
        example,
    ]
    if last_error:
        parts.extend([
            "",
            "IMPORTANT — your previous response was rejected with this error:",
            f"  {last_error}",
            "Fix it. Return only the corrected JSON object.",
        ])
    return "\n".join(parts)


def _example_from_request(request: WriterRequest) -> str:
    """Build a minimal, valid example draft using request values verbatim."""
    ev_id = ""
    ev_snippet = "(insert wording from the evidence)"
    if request.evidence:
        primary = request.evidence[0]
        ev_id = str(primary.get("id") or "")
        content = str(primary.get("content") or "").strip()
        if content:
            # Take a short snippet the LLM can lexically anchor on
            ev_snippet = content[:80]
    example = {
        "unit_id": request.unit_id,
        "run_id": request.run_id,
        "generated_by": "managed_writer",
        "content": ev_snippet,
        "proposed_value": ev_snippet,
        "assertions": [{
            "assertion_id": f"assertion-{request.unit_id}-1",
            "text": ev_snippet,
            "claim_id": f"claim-{request.unit_id}",
            "evidence_ids": [ev_id] if ev_id else [],
            "value": ev_snippet,
            "consistency_key": request.unit_id,
            "assertion_kind": "document_statement",
        }],
        "evidence_ids": [ev_id] if ev_id else [],
        "proposed_status": "draft",
        "validation_status": "pending",
        "validation_notes": [],
    }
    return json.dumps(example, ensure_ascii=False, indent=2)


def _strip_code_fences(response: str) -> str:
    """Strip common LLM decorations so plain JSON parsing succeeds.

    Many models wrap JSON in ```json ... ``` fences despite instructions.
    We accept and strip that; anything else that still fails to parse falls
    through to the retry / fallback path.
    """
    text = response.strip()
    if text.startswith("```"):
        # Drop the opening fence line
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        # Drop trailing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


class CallableWriter:
    """Test/enterprise adapter for a controlled callable, without tool access."""

    provider_id = "callable_writer"

    def __init__(self, generate: Callable[[WriterRequest], DocumentUnitDraft]):
        self._generate = generate

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        return self._generate(request)
