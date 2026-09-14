"""Deterministic resolution of document-field requirements.

The resolver is deliberately a small boundary object between template
contracts and the clarification flow.  It accepts the concrete Pydantic
models used by document authoring, but also accepts their JSON/dictionary
forms because contracts and evidence often cross a process boundary as
serialized payloads.

No model, retriever, or writer is called here.  A field is considered
resolved only when a value is present in project context, is supported by a
single unambiguous evidence value, or is explicitly supplied as an allowed
default.  Missing, partial, and conflicting inputs remain structured so a
caller can decide whether to ask a user or apply the approved policy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


_MISSING = object()

_CANDIDATE_KEYS = (
    "candidate_values",
    "candidates",
    "candidate_value",
    "normalized_value",
    "proposed_value",
    "value",
    "display_value",
)
_FIELD_KEYS = (
    "field_id",
    "target_id",
    "field",
    "field_name",
    "semantic_unit_id",
    "unit_id",
)
_EVIDENCE_ID_KEYS = ("id", "evidence_id", "evidence_ref", "claim_id")
_SOURCE_NAME_KEYS = (
    "source_name",
    "source_id",
    "source_version_id",
    "document_name",
    "source",
)
_SOURCE_ROLE_KEYS = (
    "source_role",
    "preferred_source_role",
    "document_role",
    "source_group",
    "source_type",
    "role",
)
_COVERAGE_STATUS_ALIASES = {
    "supported": "supported",
    "covered": "supported",
    "success_with_hits": "supported",
    "partial": "partial",
    "partial_failure": "partial",
    "missing": "missing",
    "unsearched": "missing",
    "success_empty": "missing",
    "conflicting": "conflicting",
    "retrieval_failed": "retrieval_failed",
    "source_unavailable": "source_unavailable",
    "access_denied": "access_denied",
}

_POLICY_ALIASES = {
    "ask": "ask",
    "clarify": "ask",
    "clarification": "ask",
    "user_input": "ask",
    "required_input": "ask",
    "user_confirm": "ask",
    "mark_tbd": "mark_tbd",
    "tbd": "mark_tbd",
    "mark-as-tbd": "mark_tbd",
    "block_section": "block_section",
    "block_generation": "block_generation",
    "block": "block_section",
    "keep_blank": "keep_blank",
    "optional": "optional",
    "none": "none",
    "use_default": "use_default",
    "allow_derivation": "allow_derivation",
}
_POLICY_STRENGTH = {
    "optional": 1,
    "keep_blank": 1,
    "mark_tbd": 2,
    "block_section": 3,
    "block_generation": 3,
}
_DEFAULT_STRATEGY = {
    "optional": "keep_blank",
    "keep_blank": "keep_blank",
    "mark_tbd": "mark_tbd",
    "block_section": "block_section",
    "block_generation": "block_section",
    "ask": "ask",
    "none": "ask",
    "use_default": "use_default",
    "allow_derivation": "allow_derivation",
}
_NO_VALUE_STRINGS = frozenset({
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "未提供",
    "未知",
    "暂无",
    "无",
})
_ASK_POLICIES = frozenset({"ask", "none"})


@dataclass(frozen=True)
class EvidenceCoverage:
    """Evidence coverage observed for one field requirement."""

    status: str
    coverage_ratio: float
    evidence_ids: tuple[str, ...] = ()
    source_count: int = 0
    candidate_count: int = 0

    def __post_init__(self) -> None:
        ratio = max(0.0, min(1.0, float(self.coverage_ratio)))
        object.__setattr__(self, "coverage_ratio", ratio)
        object.__setattr__(self, "evidence_ids", tuple(str(item) for item in self.evidence_ids if str(item)))
        object.__setattr__(self, "source_count", max(0, int(self.source_count)))
        object.__setattr__(self, "candidate_count", max(0, int(self.candidate_count)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "coverage_ratio": self.coverage_ratio,
            "coverage": self.coverage_ratio,
            "evidence_ids": list(self.evidence_ids),
            "source_count": self.source_count,
            "candidate_count": self.candidate_count,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class UnresolvedRequirement:
    """One field that cannot yet be safely resolved."""

    field: str
    field_label: str
    reason: str
    reason_codes: tuple[str, ...]
    evidence_coverage: EvidenceCoverage
    candidate_values: tuple[Any, ...]
    risk_level: str
    required: bool
    default_policy: str
    allowed_default_strategy: str
    requires_clarification: bool

    @property
    def options(self) -> tuple[Any, ...]:
        """Compatibility alias for clarification UIs."""

        return self.candidate_values

    def to_dict(self) -> dict[str, Any]:
        candidates = [_json_safe(value) for value in self.candidate_values]
        return {
            "field": self.field,
            "field_id": self.field,
            "field_label": self.field_label,
            "label": self.field_label,
            "reason": self.reason,
            "reason_codes": list(self.reason_codes),
            "evidence_coverage": self.evidence_coverage.to_dict(),
            "candidate_values": candidates,
            "options": candidates,
            "risk_level": self.risk_level,
            "required": self.required,
            "default_policy": self.default_policy,
            "allowed_default_strategy": self.allowed_default_strategy,
            "requires_clarification": self.requires_clarification,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass
class RequirementResolutionResult:
    """Result of resolving all fields in a document contract."""

    unresolved_requirements: list[UnresolvedRequirement]
    resolved_fields: dict[str, Any]
    coverage_by_field: dict[str, EvidenceCoverage]

    @property
    def clarification_requirements(self) -> list[UnresolvedRequirement]:
        return [item for item in self.unresolved_requirements if item.requires_clarification]

    def to_dict(self) -> dict[str, Any]:
        return {
            "unresolved_requirements": [item.to_dict() for item in self.unresolved_requirements],
            "clarification_requirements": [item.to_dict() for item in self.clarification_requirements],
            "resolved_fields": {
                str(field_id): _json_safe(value)
                for field_id, value in self.resolved_fields.items()
            },
            "coverage_by_field": {
                str(field_id): coverage.to_dict()
                for field_id, coverage in self.coverage_by_field.items()
            },
        }


@dataclass(frozen=True)
class _FieldContract:
    field_id: str
    label: str
    description: str
    required: bool
    value_type: str
    required_capabilities: tuple[str, ...]
    preferred_source_roles: tuple[str, ...]
    required_source_roles: tuple[str, ...]
    query_terms: tuple[str, ...]
    subject_aliases: tuple[str, ...]
    allow_derivation: bool
    missing_policy: str | None
    default_policy: str | None
    allowed_default_strategy: str | None
    default_value: Any
    context_paths: tuple[str, ...]


@dataclass(frozen=True)
class _EvidenceView:
    evidence_id: str
    evidence_ids: tuple[str, ...]
    field_keys: tuple[str, ...]
    content: str
    source_name: str
    source_role: str
    metadata: Mapping[str, Any]
    candidate_values: tuple[Any, ...]
    coverage_status: str | None
    explicitly_bound: bool


class RequirementResolver:
    """Resolve field requirements using only deterministic evidence rules."""

    def resolve(
        self,
        document_schema: Any = None,
        field_contracts: Any = None,
        evidence: Any = None,
        project_context: Any = None,
        available_sources: Any = None,
        generation_policy: Any = None,
        *,
        schema: Any = None,
        available_evidence: Any = None,
    ) -> RequirementResolutionResult:
        """Resolve a document schema or explicit field contracts.

        ``document_schema`` and ``field_contracts`` may be Pydantic models,
        dataclasses, dictionaries, or iterables of those shapes.  When both
        are supplied, explicit field contracts overlay matching schema fields
        while preserving schema order.
        """

        if document_schema is None:
            document_schema = schema
        if evidence is None and available_evidence is not None:
            evidence = available_evidence

        contracts = _build_field_contracts(document_schema, field_contracts)
        known_field_keys = {
            key
            for contract in contracts
            for key in _field_match_keys(contract)
            if key
        }
        evidence_views = _normalise_evidence(evidence, known_field_keys=known_field_keys)
        context = _as_mapping(project_context)
        policy = _as_mapping(generation_policy)
        source_roles, source_capabilities, sources_supplied = _available_source_capabilities(available_sources)

        unresolved: list[UnresolvedRequirement] = []
        resolved: dict[str, Any] = {}
        coverage_by_field: dict[str, EvidenceCoverage] = {}

        for contract in contracts:
            context_values = _context_candidates(context, contract)
            matching_evidence = [
                item for item in evidence_views if _evidence_matches_field(item, contract)
            ]
            evidence_values: list[Any] = []
            evidence_ids: list[str] = []
            source_names: set[str] = set()
            evidence_statuses: list[str] = []
            for item in matching_evidence:
                evidence_values.extend(item.candidate_values)
                if not item.candidate_values and item.content:
                    evidence_values.extend(_content_candidates(item.content, contract))
                evidence_ids.extend(item.evidence_ids)
                if item.source_name:
                    source_names.add(item.source_name)
                if item.coverage_status:
                    evidence_statuses.append(item.coverage_status)

            context_values = _dedupe_candidates(context_values)
            evidence_values = _dedupe_candidates(evidence_values)
            all_values = _dedupe_candidates([*context_values, *evidence_values])
            same_context_and_evidence = bool(context_values and evidence_values and _candidate_sets_equal(
                context_values,
                evidence_values,
            ))
            context_evidence_conflict = bool(
                context_values
                and evidence_values
                and not same_context_and_evidence
            )

            availability_codes: list[str] = []
            if sources_supplied:
                if contract.required_source_roles and not set(contract.required_source_roles).intersection(source_roles):
                    availability_codes.append("required_source_unavailable")
                if contract.required_capabilities and not set(contract.required_capabilities).intersection(source_capabilities):
                    availability_codes.append("required_capability_unavailable")

            coverage_status, coverage_ratio = _coverage_for_field(
                matching_evidence=matching_evidence,
                evidence_statuses=evidence_statuses,
                context_values=context_values,
                all_values=all_values,
                conflict=context_evidence_conflict,
            )
            coverage = EvidenceCoverage(
                status=coverage_status,
                coverage_ratio=coverage_ratio,
                evidence_ids=tuple(_dedupe_strings(evidence_ids)),
                source_count=len(source_names),
                candidate_count=len(all_values),
            )
            coverage_by_field[contract.field_id] = coverage

            effective_policy = _effective_default_policy(contract, policy)
            allowed_strategy = _effective_default_strategy(contract, effective_policy, policy)
            explicit_default = _present_default(contract.default_value)

            primary_reason: str | None = None
            if availability_codes:
                primary_reason = availability_codes[0]
            elif context_evidence_conflict:
                primary_reason = "context_evidence_conflict"
            elif len(all_values) > 1:
                primary_reason = "multiple_candidates"
            elif not all_values:
                primary_reason = "evidence_without_candidate" if matching_evidence else "no_evidence"
            elif coverage_status in {"partial", "retrieval_failed", "source_unavailable", "access_denied"}:
                primary_reason = "partial_evidence"

            if primary_reason is None:
                resolved[contract.field_id] = _json_safe(all_values[0])
                continue

            if not all_values and explicit_default and allowed_strategy not in _ASK_POLICIES:
                resolved[contract.field_id] = _json_safe(contract.default_value)
                coverage_by_field[contract.field_id] = EvidenceCoverage(
                    status="defaulted",
                    coverage_ratio=1.0,
                    evidence_ids=coverage.evidence_ids,
                    source_count=coverage.source_count,
                    candidate_count=1,
                )
                continue

            reason_codes = [primary_reason]
            if primary_reason == "no_evidence":
                reason_codes.append("missing_evidence")
            if contract.required:
                reason_codes.append("required_field")
            reason_codes.extend(code for code in availability_codes if code not in reason_codes)
            requires_clarification = _requires_clarification(
                reason=primary_reason,
                allowed_default_strategy=allowed_strategy,
            )
            unresolved.append(
                UnresolvedRequirement(
                    field=contract.field_id,
                    field_label=contract.label,
                    reason=primary_reason,
                    reason_codes=tuple(reason_codes),
                    evidence_coverage=coverage,
                    candidate_values=tuple(_json_safe(value) for value in all_values),
                    risk_level=_risk_level(contract.required, primary_reason),
                    required=contract.required,
                    default_policy=effective_policy,
                    allowed_default_strategy=allowed_strategy,
                    requires_clarification=requires_clarification,
                )
            )

        return RequirementResolutionResult(
            unresolved_requirements=unresolved,
            resolved_fields=resolved,
            coverage_by_field=coverage_by_field,
        )

    def resolve_requirements(self, *args: Any, **kwargs: Any) -> RequirementResolutionResult:
        """Named alias for callers that use the domain operation as a method."""

        return self.resolve(*args, **kwargs)


def resolve_requirements(*args: Any, **kwargs: Any) -> RequirementResolutionResult:
    """Convenience function using a fresh deterministic resolver."""

    return RequirementResolver().resolve(*args, **kwargs)


def _build_field_contracts(document_schema: Any, explicit_contracts: Any) -> list[_FieldContract]:
    schema_items = _contract_items(_read(document_schema, "fields", default=[])) if document_schema is not None else []
    explicit_items = _contract_items(explicit_contracts) if explicit_contracts is not None else []

    merged: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    labels: dict[str, int] = {}
    for item in schema_items:
        mapping = _as_mapping(item)
        if not mapping:
            continue
        normalized = dict(mapping)
        identity = _contract_identity(normalized)
        index = len(merged)
        merged.append(normalized)
        if identity:
            indexes[identity] = index
        label = _normalise_key(_text(_read(normalized, "label", "field_name", default="")))
        if label:
            labels[label] = index

    for item in explicit_items:
        mapping = _as_mapping(item)
        if not mapping:
            continue
        normalized = dict(mapping)
        identity = _contract_identity(normalized)
        label = _normalise_key(_text(_read(normalized, "label", "field_name", default="")))
        index = indexes.get(identity) if identity else labels.get(label)
        if index is None:
            index = len(merged)
            merged.append(normalized)
        else:
            merged[index] = {**merged[index], **normalized}
        if identity:
            indexes[identity] = index
        if label:
            labels[label] = index

    result: list[_FieldContract] = []
    for index, raw in enumerate(merged, start=1):
        contract = _normalise_field_contract(raw, index=index)
        if contract is not None:
            result.append(contract)
    return result


def _contract_items(value: Any) -> list[Any]:
    if value is None:
        return []
    mapping = _as_mapping(value)
    if isinstance(value, Mapping):
        if "fields" in value:
            return _contract_items(value["fields"])
        if _looks_like_contract(mapping):
            return [mapping]
        items: list[Any] = []
        for key, item in value.items():
            item_mapping = _as_mapping(item)
            if item_mapping:
                if not _contract_identity(item_mapping):
                    item_mapping = {"field_id": key, **item_mapping}
                items.append(item_mapping)
            else:
                items.append({"field_id": key, "default_value": item})
        return items
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _normalise_field_contract(raw: Any, *, index: int) -> _FieldContract | None:
    mapping = _as_mapping(raw)
    if not mapping:
        return None
    field_id = _contract_identity(mapping) or f"field-{index}"
    label = _text(_read(mapping, "label", "field_name", "name", default=field_id)) or field_id
    description = _text(_read(mapping, "description", "details", default=""))
    required = _as_bool(_read(mapping, "required", "is_required", default=True), default=True)
    required_capabilities = _string_tuple(_read(mapping, "required_capabilities", "capabilities", default=[]))
    preferred_roles = _string_tuple(_read(mapping, "preferred_source_roles", "source_roles", default=[]))
    required_roles = _string_tuple(_read(mapping, "required_source_roles", default=[]))
    query_terms = _string_tuple(_read(mapping, "query_terms", "retrieval_terms", default=[]))
    aliases = _string_tuple(_read(mapping, "subject_aliases", "aliases", default=[]))
    raw_missing_policy = _read(mapping, "missing_policy", default=_MISSING)
    raw_default_policy = _read(mapping, "default_policy", default=_MISSING)
    raw_allowed_strategy = _read(
        mapping,
        "allowed_default_strategy",
        "default_strategy",
        "allowed_default_policy",
        default=_MISSING,
    )
    missing_policy = _canonical_policy(raw_missing_policy)
    default_policy = _canonical_policy(raw_default_policy)
    allowed_strategy = _canonical_strategy(raw_allowed_strategy)
    default_value = _read(mapping, "default_value", "fallback_value", default=_MISSING)
    context_paths = _string_tuple(_read(mapping, "context_paths", "project_context_paths", default=[]))
    return _FieldContract(
        field_id=field_id,
        label=label,
        description=description,
        required=required,
        value_type=_text(_read(mapping, "value_type", "value_shape", default="text")) or "text",
        required_capabilities=required_capabilities,
        preferred_source_roles=preferred_roles,
        required_source_roles=required_roles,
        query_terms=query_terms,
        subject_aliases=aliases,
        allow_derivation=_as_bool(_read(mapping, "allow_derivation", default=False), default=False),
        missing_policy=missing_policy,
        default_policy=default_policy,
        allowed_default_strategy=allowed_strategy,
        default_value=default_value,
        context_paths=context_paths,
    )


def _normalise_evidence(value: Any, *, known_field_keys: set[str]) -> list[_EvidenceView]:
    raw_items = _evidence_items(value, known_field_keys=known_field_keys)
    result: list[_EvidenceView] = []
    for index, raw in enumerate(raw_items, start=1):
        mapping = _as_mapping(raw)
        if not mapping:
            mapping = {"value": raw}
        metadata = _as_mapping(_read(mapping, "metadata", default={}))
        field_keys = _string_tuple(_read(mapping, *_FIELD_KEYS, default=[]))
        field_keys = tuple(_normalise_field_key(item) for item in field_keys if _normalise_field_key(item))
        field_keys = tuple(_dedupe_strings(field_keys))
        evidence_ids = _string_tuple(_read(mapping, "evidence_ids", "evidence_refs", default=[]))
        identifier = _text(_read(mapping, *_EVIDENCE_ID_KEYS, default=""))
        if identifier:
            evidence_ids = tuple([identifier, *evidence_ids])
        evidence_ids = tuple(_dedupe_strings(evidence_ids))
        if not evidence_ids and _text(_read(mapping, "_resolver_evidence_id", default="")):
            evidence_ids = (_text(_read(mapping, "_resolver_evidence_id", default="")),)
        if not evidence_ids:
            evidence_ids = (f"evidence-{index}",)
        content = _text(_read(mapping, "content", "text", "excerpt", default=""))
        source_name = _text(_read(mapping, *_SOURCE_NAME_KEYS, default=""))
        if not source_name:
            source_name = _text(_read(metadata, *_SOURCE_NAME_KEYS, default=""))
        source_role = _text(_read(mapping, *_SOURCE_ROLE_KEYS, default=""))
        if not source_role:
            source_role = _text(_read(metadata, *_SOURCE_ROLE_KEYS, default=""))
        status = _canonical_coverage(_read(mapping, "coverage_status", "status", default=_MISSING))
        if status is None:
            status = _canonical_coverage(_read(metadata, "coverage_status", "status", default=_MISSING))
        candidate_values = _structured_candidates(mapping, metadata)
        explicitly_bound = bool(field_keys)
        result.append(
            _EvidenceView(
                evidence_id=evidence_ids[0],
                evidence_ids=evidence_ids,
                field_keys=field_keys,
                content=content,
                source_name=source_name,
                source_role=source_role,
                metadata=metadata,
                candidate_values=tuple(candidate_values),
                coverage_status=status,
                explicitly_bound=explicitly_bound,
            )
        )
    return result


def _evidence_items(value: Any, *, known_field_keys: set[str]) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        if _looks_like_evidence(value):
            return [value]
        items: list[Any] = []
        for key, item in value.items():
            key_normalized = _normalise_field_key(key)
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                children = list(item)
            else:
                children = [item]
            key_is_field = key_normalized in known_field_keys
            for child in children:
                child_mapping = _as_mapping(child)
                if child_mapping:
                    copy = dict(child_mapping)
                    if key_is_field and not any(_read(copy, name, default=_MISSING) is not _MISSING for name in _FIELD_KEYS):
                        copy["field_id"] = key
                    elif not key_is_field and not any(_read(copy, name, default=_MISSING) is not _MISSING for name in _EVIDENCE_ID_KEYS):
                        copy["id"] = key
                    items.append(copy)
                elif key_is_field:
                    items.append({"field_id": key, "value": child})
                else:
                    items.append({"id": key, "value": child})
        return items
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _structured_candidates(mapping: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[Any]:
    values: list[Any] = []
    for source in (mapping, metadata):
        for key in _CANDIDATE_KEYS:
            raw = _read(source, key, default=_MISSING)
            if raw is not _MISSING:
                values.extend(_flatten_candidates(raw))
    return _dedupe_candidates(values)


def _content_candidates(content: str, contract: _FieldContract) -> list[Any]:
    text = content.strip()
    if not text:
        return []
    terms = [
        contract.field_id,
        contract.label,
        contract.description,
        *contract.query_terms,
        *contract.subject_aliases,
    ]
    patterns: list[str] = []
    seen: set[str] = set()
    for term in sorted((_text(item) for item in terms), key=len, reverse=True):
        key = _normalise_key(term)
        if not term or len(key) < 2 or key in seen:
            continue
        seen.add(key)
        escaped = re.escape(term).replace(r"\ ", r"\s+").replace(r"\_", r"[\s_-]*")
        patterns.append(escaped)
    if patterns:
        anchor = "|".join(patterns)
        match = re.search(
            rf"(?i)(?<!\w)(?:{anchor})(?!\w)\s*(?::|：|=|\bis\b|为)\s*([^\n;；|]+)",
            text,
        )
        if match:
            candidate = _clean_candidate(match.group(1))
            if candidate is not None:
                return [candidate]
    return []


def _context_candidates(context: Mapping[str, Any], contract: _FieldContract) -> list[Any]:
    if not context:
        return []
    values: list[Any] = []
    keys = _field_match_keys(contract)
    containers: list[Any] = [context]
    for name in (
        "fields",
        "field_values",
        "values",
        "resolved_fields",
        "document_fields",
        "attributes",
        "project",
        "project_metadata",
        "metadata",
    ):
        nested = _read(context, name, default=_MISSING)
        if nested is not _MISSING:
            nested_mapping = _as_mapping(nested)
            if nested_mapping:
                containers.append(nested_mapping)
    for path in contract.context_paths:
        current: Any = context
        for part in path.split("."):
            current = _read(current, part, default=_MISSING)
            if current is _MISSING:
                break
        if current is not _MISSING:
            values.extend(_flatten_candidates(current))
    for container in containers:
        for key in keys:
            raw = _read(container, key, default=_MISSING)
            if raw is not _MISSING:
                values.extend(_flatten_candidates(raw))
        for key in (contract.field_id, contract.label):
            raw = _read(container, key, default=_MISSING)
            if raw is not _MISSING:
                values.extend(_flatten_candidates(raw))
    return _dedupe_candidates(values)


def _evidence_matches_field(item: _EvidenceView, contract: _FieldContract) -> bool:
    field_keys = set(_normalise_field_key(key) for key in item.field_keys if key)
    contract_keys = set(_field_match_keys(contract))
    if field_keys and field_keys.intersection(contract_keys):
        return True
    if field_keys:
        return False
    searchable = " ".join((item.content, json.dumps(dict(item.metadata), ensure_ascii=False, default=str)))
    searchable_folded = searchable.casefold()
    terms = [contract.field_id, contract.label, *contract.subject_aliases, *contract.query_terms]
    for term in terms:
        normalized = _text(term).strip()
        if len(_normalise_key(normalized)) < 2:
            continue
        if normalized.casefold() in searchable_folded:
            return True
    return False


def _coverage_for_field(
    *,
    matching_evidence: list[_EvidenceView],
    evidence_statuses: list[str],
    context_values: list[Any],
    all_values: list[Any],
    conflict: bool,
) -> tuple[str, float]:
    if conflict or len(all_values) > 1:
        return "conflicting", 1.0
    if context_values and not matching_evidence:
        return "covered", 1.0
    if not matching_evidence:
        return "missing", 0.0
    if any(status in {"retrieval_failed", "source_unavailable", "access_denied"} for status in evidence_statuses):
        return "retrieval_failed", 0.0 if not all_values else 0.5
    if any(status == "partial" for status in evidence_statuses):
        return "partial", 0.5
    if all_values:
        return "covered", 1.0
    return "partial", 0.5


def _available_source_capabilities(value: Any) -> tuple[set[str], set[str], bool]:
    if value is None:
        return set(), set(), False
    roles: set[str] = set()
    capabilities: set[str] = set()
    for item in _contract_items(value):
        mapping = _as_mapping(item)
        if not mapping:
            text = _text(item)
            if text:
                roles.add(text)
            continue
        role = _text(_read(mapping, *_SOURCE_ROLE_KEYS, default=""))
        if role:
            roles.add(role)
        roles.update(_string_tuple(_read(mapping, "roles", "source_roles", default=[])))
        capabilities.update(_string_tuple(_read(mapping, "capabilities", "supported_capabilities", default=[])))
        metadata = _as_mapping(_read(mapping, "metadata", default={}))
        roles.update(_string_tuple(_read(metadata, "roles", "source_roles", default=[])))
        capabilities.update(_string_tuple(_read(metadata, "capabilities", "supported_capabilities", default=[])))
    return {item.casefold() for item in roles}, {item.casefold() for item in capabilities}, True


def _effective_default_policy(contract: _FieldContract, policy: Mapping[str, Any]) -> str:
    field_policy = contract.default_policy or contract.missing_policy
    global_policy = _canonical_policy(_read(
        policy,
        "default_policy",
        "missing_data_policy",
        "missing_policy",
        default=_MISSING,
    ))
    if field_policy in {"ask", "none"}:
        return field_policy
    if global_policy in {"ask", "none"}:
        return global_policy
    candidates = [item for item in (field_policy, global_policy) if item in _POLICY_STRENGTH]
    if candidates:
        return max(candidates, key=lambda item: _POLICY_STRENGTH[item])
    if field_policy:
        return field_policy
    if global_policy:
        return global_policy
    return "ask" if contract.required else "optional"


def _effective_default_strategy(
    contract: _FieldContract,
    effective_policy: str,
    policy: Mapping[str, Any],
) -> str:
    if contract.allowed_default_strategy:
        return contract.allowed_default_strategy
    global_strategy = _canonical_strategy(_read(
        policy,
        "allowed_default_strategy",
        "default_strategy",
        "allowed_default_policy",
        default=_MISSING,
    ))
    if global_strategy:
        return global_strategy
    return _DEFAULT_STRATEGY.get(effective_policy, "ask")


def _requires_clarification(*, reason: str, allowed_default_strategy: str) -> bool:
    if reason in {"multiple_candidates", "context_evidence_conflict"}:
        return True
    if allowed_default_strategy in _ASK_POLICIES:
        return True
    return False


def _risk_level(required: bool, reason: str) -> str:
    if required:
        return "high"
    if reason in {"multiple_candidates", "context_evidence_conflict", "required_source_unavailable", "required_capability_unavailable"}:
        return "medium"
    return "low"


def _field_match_keys(contract: _FieldContract) -> tuple[str, ...]:
    values = [contract.field_id, contract.label, *contract.subject_aliases]
    return tuple(_normalise_field_key(value) for value in values if _normalise_field_key(value))


def _contract_identity(mapping: Mapping[str, Any]) -> str:
    value = _read(
        mapping,
        "field_id",
        "target_id",
        "semantic_unit_id",
        "unit_id",
        "field_name",
        "name",
        "field",
        default="",
    )
    return _normalise_field_key(value)


def _looks_like_contract(mapping: Mapping[str, Any]) -> bool:
    keys = {_normalise_key(str(key)) for key in mapping}
    return bool(keys.intersection({
        "field_id",
        "target_id",
        "field_name",
        "value_shape",
        "value_type",
        "required_capabilities",
        "missing_policy",
        "allow_derivation",
    }))


def _looks_like_evidence(mapping: Mapping[str, Any]) -> bool:
    keys = {_normalise_key(str(key)) for key in mapping}
    return bool(keys.intersection({
        "id",
        "evidence_id",
        "content",
        "text",
        "value",
        "normalized_value",
        "candidate_values",
        "evidence_ids",
        "coverage_status",
        "field_id",
        "target_id",
    }))


def _read(value: Any, *names: str, default: Any = _MISSING) -> Any:
    if value is None:
        return default
    mapping = value if isinstance(value, Mapping) else None
    if mapping is None:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                mapping = model_dump(mode="python")
            except TypeError:
                mapping = model_dump()
        else:
            mapping = _as_mapping(value)
    if isinstance(mapping, Mapping):
        by_folded = {_normalise_key(str(key)): item for key, item in mapping.items()}
        for name in names:
            if name in mapping:
                return mapping[name]
            folded = _normalise_key(name)
            if folded in by_folded:
                return by_folded[folded]
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="python")
        except TypeError:
            dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "__dataclass_fields__"):
        dumped = asdict(value)
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    try:
        return dict(vars(value))
    except TypeError:
        return {}


def _canonical_policy(value: Any) -> str | None:
    if value is _MISSING or value is None:
        return None
    text = _text(value).strip().casefold().replace(" ", "_")
    return _POLICY_ALIASES.get(text)


def _canonical_strategy(value: Any) -> str | None:
    policy = _canonical_policy(value)
    if policy is None:
        return None
    return _DEFAULT_STRATEGY.get(policy, policy)


def _canonical_coverage(value: Any) -> str | None:
    if value is _MISSING or value is None:
        return None
    return _COVERAGE_STATUS_ALIASES.get(_text(value).strip().casefold())


def _text(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is _MISSING or value is None:
        return default
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in {"true", "1", "yes", "y", "on", "是", "required"}:
            return True
        if folded in {"false", "0", "no", "n", "off", "否", "optional"}:
            return False
    return bool(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is _MISSING or value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Mapping):
        values = list(value.keys())
    elif isinstance(value, Iterable):
        values = list(value)
    else:
        values = [value]
    return tuple(_dedupe_strings(_text(item) for item in values if _text(item)))


def _normalise_field_key(value: Any) -> str:
    text = _text(value)
    if text.startswith("field:"):
        text = text[6:]
    return _normalise_key(text)


def _normalise_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _flatten_candidates(value: Any) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        result: list[Any] = []
        for item in value:
            result.extend(_flatten_candidates(item))
        return result
    candidate = _clean_candidate(value)
    return [] if candidate is None else [candidate]


def _clean_candidate(value: Any) -> Any | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str):
        text = value.strip().strip("\"'“”‘’")
        text = re.sub(r"[\s,，;；。]+$", "", text).strip()
        if text.casefold() in _NO_VALUE_STRINGS:
            return None
        return text
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return _json_safe(value)


def _dedupe_candidates(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for raw in values:
        value = _clean_candidate(raw)
        if value is None:
            continue
        key = _candidate_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _candidate_sets_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return {_candidate_key(item) for item in left} == {_candidate_key(item) for item in right}


def _candidate_key(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _present_default(value: Any) -> bool:
    return value is not _MISSING and bool(_flatten_candidates(value))


def _dedupe_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _text(raw)
        if not value:
            continue
        folded = value.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(value)
    return result


def _json_safe(value: Any) -> Any:
    if value is _MISSING:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except TypeError:
            return _json_safe(model_dump())
    return str(value)


__all__ = [
    "EvidenceCoverage",
    "RequirementResolutionResult",
    "RequirementResolver",
    "UnresolvedRequirement",
    "resolve_requirements",
]
