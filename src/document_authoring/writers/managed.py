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
import time
from typing import Any, Callable

from src.core.chat_model_runtime import (
    ChatModelLike,
    ModelInvocationError,
    StructuredOutputCapabilityError,
    StructuredOutputValidationError,
    invoke_structured,
    invoke_text,
    model_name,
    model_provider,
)
from src.document_authoring.models import (
    DocumentUnitDraft,
    DraftAssertion,
    ManagedDraftPayload,
    TypedFieldValue,
    TypedTableRow,
)
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
    if value_kind in {"table", "repeating_table"} or request.table_mode == "typed_rows":
        return _extract_typed_table(request)
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


def _extract_typed_table(request: WriterRequest) -> TypedFieldValue | None:
    """Copy only server-shaped row metadata into a typed table value.

    The deterministic writer deliberately does not derive identity from a
    display sentence.  Retrieval adapters may expose a bounded ``row_key``
    and ``cells`` mapping in evidence metadata; absent that structure the
    table remains unfilled and the normal validator reports a missing typed
    table rather than guessing.
    """

    candidates: list[TypedTableRow] = []
    for evidence in request.evidence:
        evidence_id = str(evidence.get("id") or "").strip()
        metadata = evidence.get("metadata") or {}
        row_values: list[dict[str, Any]] = []
        if isinstance(metadata, dict):
            table_rows = metadata.get("table_rows")
            if isinstance(table_rows, list):
                row_values.extend(item for item in table_rows if isinstance(item, dict))
            elif isinstance(metadata.get("table_row"), dict):
                row_values.append(metadata["table_row"])
            elif metadata.get("row_key") is not None and isinstance(metadata.get("cells"), dict):
                row_values.append(metadata)
        if evidence.get("row_key") is not None and isinstance(evidence.get("cells"), dict):
            row_values.append(evidence)
        for value in row_values:
            row_key = str(value.get("row_key") or "").strip()
            cells = value.get("cells")
            if not row_key or not isinstance(cells, dict) or not evidence_id:
                # Empty identity is not safe to render.  It is left out so a
                # later validation result can classify the incomplete table.
                continue
            normalized_cells = {
                str(column): str(cell_value).strip()
                for column, cell_value in cells.items()
            }
            cell_ids = value.get("cell_evidence_ids")
            if not isinstance(cell_ids, dict):
                cell_ids = {
                    column: [evidence_id]
                    for column in normalized_cells
                }
            else:
                cell_ids = {
                    str(column): [str(item) for item in ids]
                    if isinstance(ids, (list, tuple, set)) else [str(ids)]
                    for column, ids in cell_ids.items()
                }
            candidates.append(TypedTableRow(
                row_key=row_key,
                cells=normalized_cells,
                evidence_ids=[evidence_id],
                cell_evidence_ids=cell_ids,
            ))
    if not candidates:
        return _frozen_pin_table_value(request)

    by_key: dict[str, TypedTableRow] = {}
    for row in candidates:
        if row.row_key in by_key:
            # Keep the first occurrence deterministic; the validator will
            # still reject duplicate identity when the contract requires it.
            continue
        by_key[row.row_key] = row
    if request.expected_row_keys:
        ordered = [by_key[key] for key in request.expected_row_keys if key in by_key]
        ordered.extend(
            row for key, row in by_key.items() if key not in set(request.expected_row_keys)
        )
    elif request.row_order == "stable_key":
        ordered = [by_key[key] for key in sorted(by_key)]
    else:
        ordered = list(by_key.values())
    evidence_ids = _unique_values([
        evidence_id
        for row in ordered
        for evidence_id in row.evidence_ids
    ])
    return TypedFieldValue(
        kind="table",
        normalized_values=[row.row_key for row in ordered],
        display_value=f"{len(ordered)} rows",
        evidence_ids=evidence_ids,
        rows=ordered,
    )


_MISSING_MARKER = "TBD"


def _frozen_pin_mappings(
    request: WriterRequest,
) -> list[tuple[str, str, str, str]]:
    """Return ``(evidence_id, refdes, pin_name, net_name)`` frozen facts."""

    result: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for evidence in request.evidence:
        evidence_id = str(evidence.get("id") or "").strip()
        metadata = evidence.get("metadata") or {}
        if not evidence_id or not isinstance(metadata, dict):
            continue
        raw_mappings = metadata.get("pin_mappings")
        if not isinstance(raw_mappings, list):
            continue
        for raw in raw_mappings:
            if not isinstance(raw, dict):
                continue
            refdes = str(raw.get("refdes") or "").strip()
            pin_name = str(
                raw.get("pin_name") or raw.get("raw_pin_name") or ""
            ).strip()
            if not (refdes and pin_name):
                continue
            key = (refdes.casefold(), pin_name.casefold())
            if key in seen:
                continue
            seen.add(key)
            net_name = str(raw.get("net_name") or "").strip() or "NC"
            result.append((evidence_id, refdes, pin_name, net_name))
    return result


def _column_semantics(columns: dict[str, str]) -> dict[str, str]:
    """Classify template column labels into pin-table cell semantics."""

    semantics: dict[str, str] = {}
    for column_id, label in columns.items():
        normalized = str(label or "").casefold().replace("\n", " ")
        compact = re.sub(r"\s+", "", normalized)
        if any(
            token in compact
            for token in ("管脚号", "引脚号", "pin number", "pin no", "pinnumber", "pinno")
        ):
            semantics[column_id] = "pin"
        elif any(
            token in compact
            for token in ("管脚定义", "引脚定义", "pin definition", "signal definition", "定义")
        ):
            semantics[column_id] = "definition"
        elif any(token in compact for token in ("功能描述", "功能", "function")):
            semantics[column_id] = "function"
        elif any(token in compact for token in ("备注", "notice", "说明")):
            semantics[column_id] = "notice"
        else:
            semantics[column_id] = "tbd"
    return semantics


def _token_in_text(value: str, text: str) -> bool:
    token = value.strip()
    if not token or not text:
        return False
    pattern = (
        r"(?<![A-Za-z0-9_.])"
        + re.escape(token)
        + r"(?![A-Za-z0-9_.])"
    )
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def _frozen_pin_table_value(request: WriterRequest) -> TypedFieldValue | None:
    """Assemble a typed pin table directly from frozen pin-mapping metadata.

    The frozen circuit scope is the authoritative source for connector pin
    identity and net definition.  Cells that cannot be anchored in the frozen
    evidence (for example ERP numbers or a function description absent from
    the knowledge base) carry the governed ``TBD`` marker instead of a model
    invention, so a 100+ row table never depends on one oversized LLM reply.
    """

    if request.table_mode != "typed_rows":
        return None
    columns = dict(request.table_columns or {})
    if not columns and request.expected_columns:
        columns = {column: column for column in request.expected_columns}
    if not columns:
        return None
    mappings = _frozen_pin_mappings(request)
    if not mappings:
        return None
    semantics = _column_semantics(columns)
    evidence_text = {
        str(evidence.get("id") or ""): str(evidence.get("content") or "")
        for evidence in request.evidence
    }
    function_hits: dict[str, tuple[str, str]] = {}
    for evidence in request.evidence:
        evidence_id = str(evidence.get("id") or "").strip()
        metadata = evidence.get("metadata") or {}
        hits = metadata.get("pin_function_hits") if isinstance(metadata, dict) else None
        if not evidence_id or not isinstance(hits, dict):
            continue
        for row_key, text in hits.items():
            key = str(row_key or "").strip()
            value = str(text or "").strip()
            if key and value and key not in function_hits:
                function_hits[key] = (value, evidence_id)
    rows: list[TypedTableRow] = []
    for evidence_id, refdes, pin_name, net_name in mappings:
        text = evidence_text.get(evidence_id, "")
        row_key = f"{refdes}-{pin_name}"
        row_evidence = [evidence_id]
        cells: dict[str, str] = {}
        cell_evidence: dict[str, list[str]] = {}
        for column_id, kind in semantics.items():
            support_id = evidence_id
            if kind == "pin":
                candidate = f"{refdes}-{pin_name}"
            elif kind == "function" and row_key in function_hits:
                candidate, support_id = function_hits[row_key]
            elif kind == "definition":
                candidate = net_name
            elif kind == "function":
                resolution = resolve_pin_function(
                    refdes=refdes, pin_name=pin_name, net_name=net_name,
                )
                candidate = str(getattr(resolution, "function", "") or "").strip()
            else:
                candidate = ""
            anchor_text = evidence_text.get(support_id, "") or text
            cells[column_id] = (
                candidate
                if candidate and _token_in_text(candidate, anchor_text)
                else _MISSING_MARKER
            )
            if support_id not in row_evidence:
                row_evidence.append(support_id)
            cell_evidence[column_id] = [support_id]
        rows.append(TypedTableRow(
            row_key=row_key,
            cells=cells,
            evidence_ids=row_evidence,
            cell_evidence_ids=cell_evidence,
        ))
    if not rows:
        return None
    return TypedFieldValue(
        kind="table",
        normalized_values=[row.row_key for row in rows],
        display_value=f"{len(rows)} rows",
        evidence_ids=_unique_values([
            evidence_id
            for row in rows
            for evidence_id in row.evidence_ids
        ]),
        rows=rows,
    )


def _frozen_pin_table_draft(request: WriterRequest) -> DocumentUnitDraft | None:
    """Return a validated deterministic table draft, or ``None`` to fall back."""

    if request.table_mode != "typed_rows" or not _frozen_pin_mappings(request):
        return None
    try:
        draft = _deterministic_draft(request)
    except ValueError:
        return None
    evidence = {str(item.get("id") or ""): item for item in request.evidence}
    validator = DocumentValidator()
    validated = validator.validate_unit_draft(draft, evidence)
    validated = validator.validate_typed_field_draft(
        validated,
        evidence,
        expected_value_type=request.field_value_type,
        expected_row_keys=request.expected_row_keys,
        required_columns=request.expected_columns,
        row_order=request.row_order,
        duplicate_policy=request.duplicate_policy,
    )
    if validated.validation_status != "supported":
        logger.info(
            "Deterministic frozen pin table failed validation for unit %s: %s",
            request.unit_id,
            validated.validation_notes,
        )
        return None
    return draft


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
    """Constrained document draft provider backed by a shared chat model.

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

    def __init__(
        self,
        model: ChatModelLike | None = None,
        *,
        observation_callback: Callable[[dict[str, Any]], None] | None = None,
        model_factory: Callable[[], ChatModelLike] | None = None,
    ):
        """Create a managed writer.

        A LangChain ``BaseChatModel`` is preferred. A factory is accepted for
        lazy construction so importing the service never starts a provider
        connection. Text JSON remains an explicit provider-compatibility path.
        """
        self._model = model
        self._model_factory = model_factory
        self._observation_callback = observation_callback
        self._validator = DocumentValidator()

    def generate(self, request: WriterRequest) -> DocumentUnitDraft:
        connector_draft = _connector_function_draft(request)
        if connector_draft is not None:
            logger.info("Using deterministic connector function resolver for unit %s", request.unit_id)
            return _with_writer_metadata(
                connector_draft,
                writer_mode="deterministic_connector",
                observations=[],
            )

        if request.table_mode == "typed_rows":
            table_draft = _frozen_pin_table_draft(request)
            if table_draft is not None:
                logger.info("Using deterministic frozen pin table for unit %s", request.unit_id)
                return _with_writer_metadata(
                    table_draft,
                    writer_mode="deterministic_pin_table",
                    observations=[],
                )

        draft, observations, capability_error = self._generate_structured(request)
        if draft is not None:
            return _with_writer_metadata(
                draft, writer_mode="structured", observations=observations,
            )
        if capability_error is None:
            return self._fallback_draft(
                request,
                observations,
                fallback_reason="structured_output_validation_failed",
                writer_mode="structured",
            )
        if capability_error is not None:
            logger.info(
                "Managed writer provider does not support structured output for %s; "
                "using JSON compatibility path: %s",
                request.unit_id, capability_error,
            )
            json_draft, json_observations, _ = self._generate_json(
                request,
                prior_observations=observations,
                initial_error=f"structured output unsupported: {capability_error}",
            )
            if json_draft is not None:
                return _with_writer_metadata(
                    json_draft, writer_mode="json_fallback", observations=json_observations,
                )
            return self._fallback_draft(
                request, json_observations,
                fallback_reason="structured_output_unsupported_and_json_failed",
                writer_mode="json_fallback",
            )

        draft, observations, _ = self._generate_json(
            request, prior_observations=observations,
        )
        if draft is not None:
            return _with_writer_metadata(
                draft,
                writer_mode="json_fallback",
                observations=observations,
            )
        return self._fallback_draft(
            request,
            observations,
            fallback_reason="json_compatibility_failed",
            writer_mode="json_fallback",
        )

    def _get_model(self) -> Any | None:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory()
            return self._model
        # The service's default production construction goes through the
        # central factory.  Import lazily to keep this module usable by the
        # deterministic/offline test path.
        from src.core.model_factory import create_chat_model

        self._model = create_chat_model()
        return self._model

    def _generate_structured(
        self,
        request: WriterRequest,
    ) -> tuple[DocumentUnitDraft | None, list[dict[str, Any]], str | None]:
        observations: list[dict[str, Any]] = []
        last_error: str | None = None
        try:
            model = self._get_model()
        except Exception as exc:
            observations.append(_runtime_observation(
                attempt=1,
                operation="structured",
                started=time.monotonic(),
                model=None,
                error=exc,
            ))
            return None, observations, None
        for attempt in range(1, self._MAX_LLM_ATTEMPTS + 1):
            user_content = _build_user_prompt(request, last_error)
            started = time.monotonic()
            try:
                result = invoke_structured(
                    model,
                    ManagedDraftPayload,
                    [
                        {"role": "system", "content": _WRITER_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    operation="document_authoring",
                    profile="default",
                )
                draft = self._draft_from_payload(result.value, request)
                observations.append(_runtime_observation(
                    attempt=attempt,
                    operation="structured",
                    started=started,
                    model=model,
                    result=result,
                ))
                return draft, observations, None
            except StructuredOutputCapabilityError as exc:
                observations.append(_runtime_observation(
                    attempt=attempt,
                    operation="structured",
                    started=started,
                    model=model,
                    error=exc,
                ))
                return None, observations, str(exc)
            except (ModelInvocationError, StructuredOutputValidationError, ValueError) as exc:
                last_error = str(exc)
                observations.append(_runtime_observation(
                    attempt=attempt,
                    operation="structured",
                    started=started,
                    model=model,
                    error=exc,
                ))
                logger.warning(
                    "LLMManagedWriter structured attempt %d/%d failed for unit %s: %s",
                    attempt, self._MAX_LLM_ATTEMPTS, request.unit_id, last_error,
                )
        return None, observations, None

    def _generate_json(
        self,
        request: WriterRequest,
        *,
        prior_observations: list[dict[str, Any]] | None = None,
        initial_error: str | None = None,
    ) -> tuple[DocumentUnitDraft | None, list[dict[str, Any]], str | None]:
        observations = list(prior_observations or [])
        last_error: str | None = initial_error
        for attempt in range(1, self._MAX_LLM_ATTEMPTS + 1):
            user_content = _build_user_prompt(request, last_error)
            messages = [
                {"role": "system", "content": _WRITER_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]
            started = time.monotonic()
            try:
                model = self._get_model()
                result = invoke_text(
                    model,
                    messages,
                    operation="document_authoring",
                    profile="default",
                )
                observations.append(_runtime_observation(
                    attempt=attempt,
                    operation="json_fallback",
                    started=started,
                    model=model,
                    result=result,
                ))
            except Exception as exc:
                last_error = f"managed writer provider failed: {exc}"
                observations.append(_runtime_observation(
                    attempt=attempt,
                    operation="json_fallback",
                    started=started,
                    model=self._model,
                    error=exc,
                ))
                logger.warning(
                    "LLMManagedWriter JSON attempt %d/%d failed for unit %s: %s",
                    attempt, self._MAX_LLM_ATTEMPTS, request.unit_id, last_error,
                )
                continue
            try:
                draft = self._parse_and_validate(result.text, request)
                return draft, observations, None
            except ValueError as exc:
                last_error = str(exc)
                logger.warning(
                    "LLMManagedWriter JSON attempt %d/%d failed for unit %s: %s",
                    attempt, self._MAX_LLM_ATTEMPTS, request.unit_id, exc,
                )
        return None, observations, last_error

    def _draft_from_payload(
        self,
        payload: ManagedDraftPayload,
        request: WriterRequest,
    ) -> DocumentUnitDraft:
        """Attach coordinator-owned identity/lifecycle fields to a payload."""
        draft = DocumentUnitDraft(
            unit_id=request.unit_id,
            run_id=request.run_id,
            generated_by="managed_writer",
            content=payload.content,
            proposed_value=payload.proposed_value,
            typed_value=payload.typed_value,
            assertions=payload.assertions,
            evidence_ids=payload.evidence_ids,
            proposed_status="draft",
            validation_status="pending",
            validation_notes=[],
        )
        self._validate_draft(draft, request)
        return draft

    def _validate_draft(self, draft: DocumentUnitDraft, request: WriterRequest) -> None:
        if not draft.content or not draft.assertions or draft.typed_value is None:
            raise ValueError(
                "managed writer returned an unsupported draft "
                f"(content={'set' if draft.content else 'empty'}, "
                f"assertions={len(draft.assertions)}, typed_value={'set' if draft.typed_value else 'empty'})"
            )
        evidence = {str(item.get("id") or ""): item for item in request.evidence}
        if not set(draft.evidence_ids) or not set(draft.evidence_ids) <= set(evidence):
            raise ValueError(
                "managed writer draft is not grounded in supplied evidence "
                f"(draft_evidence_ids={draft.evidence_ids}, "
                f"available_evidence_ids={sorted(evidence)})"
            )
        validated = self._validator.validate_unit_draft(draft, evidence)
        validated = self._validator.validate_typed_field_draft(
            validated,
            evidence,
            expected_value_type=request.field_value_type,
            expected_row_keys=request.expected_row_keys,
            required_columns=request.expected_columns,
            row_order=request.row_order,
            duplicate_policy=request.duplicate_policy,
        )
        if validated.validation_status != "supported":
            raise ValueError(
                "managed writer returned an ungrounded draft "
                f"(validator_status={validated.validation_status!r}, "
                f"notes={validated.validation_notes})"
            )

    def _fallback_draft(
        self,
        request: WriterRequest,
        observations: list[dict[str, Any]],
        *,
        fallback_reason: str,
        writer_mode: str,
    ) -> DocumentUnitDraft:
        logger.warning(
            "LLMManagedWriter falling back to deterministic writer for unit %s: %s",
            request.unit_id,
            fallback_reason,
        )
        return _with_writer_metadata(
            _deterministic_draft(request),
            writer_mode=writer_mode,
            observations=observations,
            writer_fallback=True,
            fallback_reason=fallback_reason,
        )

    def _parse_and_validate(self, response: str, request: WriterRequest) -> DocumentUnitDraft:
        cleaned = _strip_code_fences(response)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"managed writer returned malformed JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("managed writer response must be a JSON object")
        owned_fields = set(ManagedDraftPayload.model_fields)
        compatibility_fields = _DRAFT_COORDINATOR_FIELDS
        unknown = set(payload) - owned_fields - compatibility_fields
        missing = {"content", "typed_value", "assertions", "evidence_ids"} - set(payload)
        if unknown or missing:
            raise ValueError(
                f"managed writer returned unsupported draft fields "
                f"(missing={sorted(missing)}, extra={sorted(unknown)})"
            )
        # Legacy JSON clients sent coordinator-owned fields.  They are parsed
        # only on this explicitly named compatibility path, then overwritten
        # by the request-owned identity/status below.
        try:
            managed_payload = ManagedDraftPayload.model_validate({
                key: value for key, value in payload.items() if key in owned_fields
            })
        except Exception as exc:
            raise ValueError(f"managed writer returned malformed JSON payload: {exc}") from exc
        return self._draft_from_payload(managed_payload, request)


_DRAFT_COORDINATOR_FIELDS = {
    "unit_id", "run_id", "generated_by", "proposed_status",
    "validation_status", "validation_notes",
}


def _runtime_observation(
    *,
    attempt: int,
    operation: str,
    started: float,
    model: Any | None,
    result: Any | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    usage = getattr(result, "usage", None)
    usage_returned = bool(getattr(usage, "usage_returned", False))
    prompt_tokens = getattr(usage, "input_tokens", None) if usage_returned else None
    completion_tokens = getattr(usage, "output_tokens", None) if usage_returned else None
    total_tokens = getattr(usage, "total_tokens", None) if usage_returned else None
    return {
        "call": attempt,
        "operation": operation,
        "duration_seconds": time.monotonic() - started,
        "provider": model_provider(model),
        "model": model_name(model),
        "status": "error" if error is not None else "success",
        "usage_returned": usage_returned,
        "prompt_tokens": prompt_tokens if prompt_tokens is not None else "unknown",
        "completion_tokens": completion_tokens if completion_tokens is not None else "unknown",
        "total_tokens": total_tokens if total_tokens is not None else "unknown",
        "error_type": type(error).__name__ if error is not None else None,
    }


def _with_writer_metadata(
    draft: DocumentUnitDraft,
    *,
    writer_mode: str,
    observations: list[dict[str, Any]],
    writer_fallback: bool = False,
    fallback_reason: str | None = None,
) -> DocumentUnitDraft:
    metadata = dict(draft.metadata or {})
    metadata.update({
        "writer_mode": writer_mode,
        "writer_fallback": bool(writer_fallback),
        "fallback_reason": fallback_reason,
    })
    if observations:
        metadata["llm_observations"] = observations
    return draft.model_copy(update={"metadata": metadata})


_CONNECTOR_PIN_TERM_RE = re.compile(
    r"^(?P<ref>[XJ]\d+[A-Z0-9_]*)[-_./:](?P<pin>[A-Za-z0-9_.]+)$",
    re.IGNORECASE,
)


def _connector_function_draft(request: WriterRequest) -> DocumentUnitDraft | None:
    """Build a grounded function draft without asking the LLM to cross-wire rows.

    This resolver answers a single-pin function question.  A typed table unit
    must never take this shortcut: it would replace the whole table contract
    with one scalar row and fail the deterministic release gate.
    """

    if (
        request.table_mode == "typed_rows"
        or str(request.field_value_type).strip().casefold()
        in {"table", "repeating_table"}
    ):
        return None
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

Top-level keys (exactly these five, no others):
  content             string   — the drafted body text, non-empty
  proposed_value      string   — usually the same as `content`
  typed_value         object   — required for automatic filling, with:
      kind               string — "scalar", "enumeration" or "table", matching field_value_type
      normalized_values  array of strings — one value for scalar; deduplicated values for enumeration
      display_value      string — the exact value to fill, never a whole evidence chunk
      rows               array    — required when kind is "table"; each row has:
          row_key            string — the server-owned row identity from expected_row_keys
          cells              object — one string per expected column
          cell_evidence_ids  object — evidence ids per column, from the request evidence
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

Rules:
- The coordinator owns unit_id, run_id, generated_by and the lifecycle status;
  do NOT include those keys.
- Treat `retrieval_query_terms` as the field's focus. Prefer the smallest
  evidence-grounded value that answers those terms; do not copy a whole table,
  page dump, or unrelated long evidence chunk into a cell.
- Every `evidence_ids` entry (top-level and inside assertions) must exist in
  the request `evidence[].id`. Do NOT fabricate ids.
- `typed_value.evidence_ids` must exist in the request evidence and directly
  support its normalized values. Do not create `typed_value` from an entire
  evidence paragraph or from low-confidence/reused evidence.
- When `table_mode` is `typed_rows`, `typed_value.kind` MUST be `table` and
  every row MUST contain a non-empty server-owned `row_key`, a `cells` object
  using only the expected columns, and `cell_evidence_ids` for its cell
  evidence. Return typed rows, never a scalar, comma-separated list, or
  display string in place of a table. Do not derive a row key from display
  text and do not choose workbook sheets, cells, or ranges.
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
    if request.table_mode == "typed_rows":
        parts[4:4] = [
            "TABLE OUTPUT REQUIREMENT: return typed rows with row_key, cells, "
            "and cell_evidence_ids; never as a scalar or prose list.",
            "Every row key must come from the server-owned row contract. "
            "The writer must not choose workbook coordinates.",
            "",
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
    if request.table_mode == "typed_rows":
        row_key = request.expected_row_keys[0] if request.expected_row_keys else "<server-row-key>"
        columns = request.expected_columns or list(request.table_columns or {})
        row_cells = {column: f"<{column}-value>" for column in columns}
        row_evidence = [ev_id] if ev_id else []
        return json.dumps({
            "unit_id": request.unit_id,
            "run_id": request.run_id,
            "generated_by": "managed_writer",
            "content": ev_snippet,
            "proposed_value": ev_snippet,
            "typed_value": {
                "kind": "table",
                "normalized_values": [row_key],
                "display_value": "1 row",
                "evidence_ids": row_evidence,
                "rows": [{
                    "row_key": row_key,
                    "cells": row_cells,
                    "evidence_ids": row_evidence,
                    "cell_evidence_ids": {
                        column: row_evidence for column in columns
                    },
                }],
            },
            "assertions": [{
                "assertion_id": f"assertion-{request.unit_id}-1",
                "text": ev_snippet,
                "claim_id": f"claim-{request.unit_id}",
                "evidence_ids": row_evidence,
                "value": row_key,
                "consistency_key": request.unit_id,
                "assertion_kind": "document_statement",
            }],
            "evidence_ids": row_evidence,
        }, ensure_ascii=False, indent=2)

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
