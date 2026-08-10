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
import re
from typing import Any, Callable

from src.core.llm_client import LLMClient
from src.document_authoring.models import DocumentUnitDraft, DraftAssertion, TypedFieldValue
from src.document_authoring.pin_function_inference import (
    infer_pin_function_from_net,
    resolve_pin_function,
)
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
    typed_value = _extract_typed_value(request, items)
    return DocumentUnitDraft(
        unit_id=request.unit_id,
        run_id=request.run_id,
        generated_by="managed_writer",
        content=body,
        proposed_value=body,
        typed_value=typed_value,
        evidence_ids=evidence_ids,
        assertions=[DraftAssertion(
            assertion_id=f"assertion-{assertion_id}", text=body,
            claim_id=f"claim-{request.unit_id}", evidence_ids=evidence_ids,
            value=body, consistency_key=request.unit_id,
        )],
        proposed_status="draft",
    )


def _extract_typed_value(
    request: WriterRequest,
    items: list[tuple[str, str]],
) -> TypedFieldValue | None:
    """Extract only explicit assignments; arbitrary evidence prose is not a value."""
    value_kind = request.field_value_type.strip().casefold()
    if value_kind in {"text", "string", "scalar", "number", "integer", "float", "date"}:
        kind = "scalar"
    elif value_kind in {"enum", "enumeration", "list", "set"}:
        kind = "enumeration"
    else:
        return None

    extracted: list[tuple[str, str]] = []
    for evidence_id, text in items:
        match = re.search(r"(?:[:：=]|为|\bis\b)\s*([^\n。；;.!]+)", text, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            extracted.append((evidence_id, value))
    if not extracted:
        return None

    if kind == "enumeration":
        values = [
            candidate.strip()
            for _, value in extracted
            for candidate in re.split(r"[,，、/;；]", value)
            if candidate.strip()
        ]
    else:
        values = [value for _, value in extracted]
    normalized_values = _unique_values(values)
    evidence_ids = _unique_values([evidence_id for evidence_id, _ in extracted])
    display_value = ", ".join(normalized_values) if kind == "enumeration" else " / ".join(normalized_values)
    return TypedFieldValue(
        kind=kind,
        normalized_values=normalized_values,
        display_value=display_value,
        evidence_ids=evidence_ids,
    )


def _unique_values(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized.casefold() not in seen:
            result.append(normalized)
            seen.add(normalized.casefold())
    return result


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
        connector_draft = _connector_function_draft(request)
        if connector_draft is not None:
            logger.info("Using deterministic connector function resolver for unit %s", request.unit_id)
            return connector_draft
        last_error: str | None = None
        for attempt in range(1, self._MAX_LLM_ATTEMPTS + 1):
            user_content = _build_user_prompt(request, last_error)
            try:
                response = self._client.chat(
                    [
                        {"role": "system", "content": _WRITER_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    usage_stage="document_authoring",
                    # A field writer is recoverable: after this bounded wait
                    # the evidence-grounded deterministic writer can finish
                    # the field instead of holding the whole Harness lease.
                    timeout=60,
                    # One template cell needs a concise, structured value;
                    # retaining the global multi-thousand-token ceiling lets
                    # a congested model hold an entire parallel worker open.
                    max_tokens=512,
                    # Under concurrent document generation, retrying a 429
                    # inside one field can monopolize a worker for minutes.
                    # A validated-evidence fallback is safer and faster.
                    rate_limit_max_retries=0,
                )
            except Exception as exc:
                last_error = f"managed writer provider failed: {exc}"
                logger.warning(
                    "LLMManagedWriter attempt %d/%d failed for unit %s: %s",
                    attempt, self._MAX_LLM_ATTEMPTS, request.unit_id, last_error,
                )
                continue
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
            or draft.typed_value is None
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
        validated = self._validator.validate_typed_field_draft(
            validated,
            evidence,
            expected_value_type=request.field_value_type,
        )
        if validated.validation_status != "supported":
            raise ValueError(
                f"managed writer returned an ungrounded draft "
                f"(validator_status={validated.validation_status!r}, "
                f"notes={validated.validation_notes})"
            )
        return draft


_CONNECTOR_PIN_TERM_RE = re.compile(
    r"^(?P<ref>[XJ]\d+[A-Z0-9_]*)[-_./:](?P<pin>[A-Za-z0-9_.]+)$",
    re.IGNORECASE,
)


def _connector_function_draft(request: WriterRequest) -> DocumentUnitDraft | None:
    """Build a grounded function draft without asking the LLM to cross-wire rows."""

    field_text = f"{request.unit_label} {request.unit_description}".casefold()
    if not any(term in field_text for term in ("功能描述", "功能说明", "function", "pin function")):
        return None
    target = next(
        (match for term in request.retrieval_query_terms
         if (match := _CONNECTOR_PIN_TERM_RE.fullmatch(str(term).strip()))),
        None,
    )
    if target is None:
        for item in request.evidence:
            match = re.search(r"(?<![A-Za-z0-9_])([XJ]\d+[A-Z0-9_]*)\s*[-_./:]\s*&?([A-Za-z0-9_.]+)", str(item.get("content") or ""), re.IGNORECASE)
            if match:
                target = _CONNECTOR_PIN_TERM_RE.fullmatch(f"{match.group(1)}-{match.group(2)}")
                if target:
                    break
    if target is None:
        return None
    refdes, pin_name = target.group("ref").upper(), target.group("pin")
    net_name = next(
        (str(term).strip() for term in request.retrieval_query_terms
         if str(term).strip() and not _CONNECTOR_PIN_TERM_RE.fullmatch(str(term).strip())
         and infer_pin_function_from_net(str(term).strip()) is not None),
        "",
    )
    if not net_name:
        target_text = f"{refdes}-{pin_name}".casefold()
        for item in request.evidence:
            text = str(item.get("content") or "")
            if target_text not in re.sub(r"[.&]", "-", text).casefold() and target_text not in text.casefold():
                continue
            candidates = re.findall(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{2,}(?![A-Za-z0-9_])", text.upper())
            net_name = next((candidate for candidate in candidates if infer_pin_function_from_net(candidate)), "")
            if net_name:
                break
    resolution = resolve_pin_function(
        refdes=refdes,
        pin_name=pin_name,
        net_name=net_name or "connected",
        evidence=request.evidence,
    )
    if not resolution.function:
        return None
    evidence_id = next(iter(resolution.evidence_ids), "")
    if not evidence_id and request.evidence:
        evidence_id = str(request.evidence[0].get("id") or "")
    if not evidence_id:
        return None
    prefix = f"{refdes}-{pin_name}"
    net_clause = f" 网络 {net_name}" if net_name else ""
    content = f"{prefix}{net_clause}：{resolution.function}"
    synthetic_evidence: list[dict[str, Any]] = [{
        "id": evidence_id,
        "content": content,
        "source_name": "connector-function-resolver",
        "metadata": {"derived_from": resolution.source},
        "locator": {},
        "fact_type": "connector_pin_function",
    }]
    return _deterministic_draft(request.model_copy(update={"evidence": synthetic_evidence}))


_DRAFT_FIELDS = {
    "unit_id", "run_id", "generated_by", "content", "proposed_value",
    "typed_value", "assertions", "evidence_ids", "proposed_status", "validation_status", "validation_notes",
}


_WRITER_SYSTEM_PROMPT = """You are the Managed Writer for a governed document authoring pipeline.

Return ONE JSON object, and ONLY that object — no prose, no code fences.

Top-level keys (exactly these eleven, no others):
  unit_id             string   — copy verbatim from the request
  run_id              string   — copy verbatim from the request
  generated_by        string   — must be exactly "managed_writer"
  content             string   — the drafted body text, non-empty
  proposed_value      string   — usually the same as `content`
  typed_value         object   — required for automatic filling, with:
      kind               string — "scalar" or "enumeration", matching field_value_type
      normalized_values  array of strings — one value for scalar; deduplicated values for enumeration
      display_value      string — the exact value to fill, never a whole evidence chunk
      evidence_ids       array of strings — ids that directly support the typed value
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
- Treat `retrieval_query_terms` as the field's focus. Prefer the smallest
  evidence-grounded value that answers those terms; do not copy a whole table,
  page dump, or unrelated long evidence chunk into a cell.
- Every `evidence_ids` entry (top-level and inside assertions) must exist in
  the request `evidence[].id`. Do NOT fabricate ids.
- `typed_value.evidence_ids` must exist in the request evidence and directly
  support its normalized values. Do not create `typed_value` from an entire
  evidence paragraph or from low-confidence/reused evidence.
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
        "typed_value": {
            "kind": "scalar",
            "normalized_values": [ev_snippet],
            "display_value": ev_snippet,
            "evidence_ids": [ev_id] if ev_id else [],
        },
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
