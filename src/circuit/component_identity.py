"""Evidence-backed component identity projection and bounded resolution.

Governed inputs only: EDF instance fields/properties plus an optional local
curated catalog. RAGFlow datasheet text, vector recall and LLM guesses never
contribute role assertions here (plan task 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from src.circuit.models import (
    ComponentAlias,
    ComponentIdentifier,
    ComponentIdentity,
    ComponentRoleAssertion,
    CircuitDesign,
)

CONTROLLED_ROLES = (
    "system_on_chip",
    "mcu",
    "pmic",
    "ethernet_phy",
    "memory",
    "transceiver",
    "connector",
)

CONTROLLER_FAMILY_ROLES = ("system_on_chip", "mcu")
CONTROLLER_FAMILY = "controller_family"

MAX_RETURNED_CANDIDATES = 20

EXPLICIT_ASSERTION_CONFIDENCE = 0.95

# Only these explicit EDF property keys may carry identifiers. Values are kept
# verbatim; splitting multi-MPN strings is the datasheet matcher's job (5b).
PROPERTY_IDENTIFIER_RULES = {
    "Manufacturer Part Number": "manufacturer_part_number",
    "Part Number": "internal_part_number",
    "ERP NUM": "erp_number",
}

# Controlled property-key → normalized allowed value → role id. Anything not
# listed here (library cells, Description, Part Type, module names) must never
# produce a role assertion.
ROLE_PROPERTY_RULES = {
    "Device Role": {
        "soc": "system_on_chip",
        "system on chip": "system_on_chip",
        "system_on_chip": "system_on_chip",
        "mcu": "mcu",
        "microcontroller": "mcu",
        "pmic": "pmic",
        "power management": "pmic",
        "ethernet phy": "ethernet_phy",
        "memory": "memory",
        "transceiver": "transceiver",
        "connector": "connector",
    },
}

# Query-term vocabulary: normalized term → controlled role id.
ROLE_SYNONYMS = {
    "soc": "system_on_chip",
    "system on chip": "system_on_chip",
    "system_on_chip": "system_on_chip",
    "mcu": "mcu",
    "microcontroller": "mcu",
    "单片机": "mcu",
    "pmic": "pmic",
    "电源管理": "pmic",
    "电源管理芯片": "pmic",
    "ethernet phy": "ethernet_phy",
    "以太网phy": "ethernet_phy",
    "以太网 phy": "ethernet_phy",
    "memory": "memory",
    "存储": "memory",
    "存储器": "memory",
    "transceiver": "transceiver",
    "收发器": "transceiver",
    "connector": "connector",
    "连接器": "connector",
}

# Retrieval-only intent terms; they never become a role assertion themselves.
FAMILY_SYNONYMS = {
    "主控": CONTROLLER_FAMILY_ROLES,
    "主控芯片": CONTROLLER_FAMILY_ROLES,
    "处理器": CONTROLLER_FAMILY_ROLES,
    "controller": CONTROLLER_FAMILY_ROLES,
    "processor": CONTROLLER_FAMILY_ROLES,
}


def normalize_identifier_value(value: Any) -> str:
    """Case/whitespace-insensitive form; ``raw_value`` always stays intact."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


@dataclass(frozen=True)
class CuratedCatalogEntry:
    """One governed catalog row mapping a namespaced value to a role."""

    entry_id: str
    catalog_version: str
    source_file: str
    match_namespace: str
    match_raw_value: str
    role_id: str
    display_name: str
    confidence: float = 1.0

    def __post_init__(self):
        for name in ("entry_id", "catalog_version", "source_file", "match_raw_value", "display_name"):
            if not str(getattr(self, name)):
                raise ValueError(f"{name} 必须是非空字符串")
        if self.match_namespace not in (
            "internal_part_number",
            "manufacturer_part_number",
            "erp_number",
            "refdes",
            "library_cell",
            "value",
        ):
            raise ValueError(f"match_namespace 取值不受支持: {self.match_namespace!r}")
        if self.role_id not in CONTROLLED_ROLES:
            raise ValueError(f"role_id 不在受控词表中: {self.role_id!r}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence 必须在 [0, 1] 区间内")


@dataclass(frozen=True)
class IdentityCandidate:
    design_id: str
    kb_name: str
    refdes: str
    matched_by: str
    matched_value: str
    matched_role_ids: tuple[str, ...] = ()
    identifiers: tuple[ComponentIdentifier, ...] = ()
    roles: tuple[ComponentRoleAssertion, ...] = ()


@dataclass
class EntityResolutionResult:
    intent_kind: str
    term: str
    role_query: str | None = None
    controller_family_roles: tuple[str, ...] | None = None
    candidates: list[IdentityCandidate] = field(default_factory=list)
    candidate_count: int = 0
    returned_candidate_count: int = 0
    resolution_status: str = "no_evidence"


def _identifiers_for_instance(instance) -> list[ComponentIdentifier]:
    identifiers: list[ComponentIdentifier] = []
    seen: set[tuple[str, str]] = set()

    def add(namespace: str, raw_value: Any, locator: dict[str, Any]) -> None:
        raw_text = str(raw_value or "").strip()
        if not raw_text:
            return
        key = (namespace, normalize_identifier_value(raw_text))
        if key in seen:
            return
        seen.add(key)
        identifiers.append(
            ComponentIdentifier(
                namespace=namespace,
                raw_value=raw_text,
                normalized_value=key[1],
                source_kind="edf_property",
                source_locator=locator,
            )
        )

    add("refdes", instance.refdes, {"field": "refdes"})
    add("internal_part_number", instance.part_number, {"field": "part_number"})
    add("erp_number", instance.erp_number, {"field": "erp_number"})
    add("library_cell", instance.library_cell, {"field": "library_cell"})
    add("value", instance.value, {"field": "value"})
    properties = instance.properties or {}
    for property_key, namespace in PROPERTY_IDENTIFIER_RULES.items():
        add(
            namespace,
            properties.get(property_key),
            {"property_key": property_key},
        )
    return identifiers


def _explicit_role_assertions(instance, source_file: str) -> list[ComponentRoleAssertion]:
    assertions: list[ComponentRoleAssertion] = []
    properties = instance.properties or {}
    for property_key, allowed_values in ROLE_PROPERTY_RULES.items():
        raw_value = properties.get(property_key)
        if raw_value is None:
            continue
        role_id = allowed_values.get(normalize_identifier_value(raw_value))
        if role_id is None:
            continue
        assertions.append(
            ComponentRoleAssertion(
                role_id=role_id,
                display_name=role_id,
                source_kind="edf_property",
                source_file=source_file,
                source_locator={
                    "property_key": property_key,
                    "property_value": str(raw_value),
                },
                confidence=EXPLICIT_ASSERTION_CONFIDENCE,
                assertion_mode="explicit",
            )
        )
    return assertions


def _catalog_role_assertions(
    identifiers: Sequence[ComponentIdentifier],
    entries: Sequence[CuratedCatalogEntry],
    source_default: str,
) -> list[ComponentRoleAssertion]:
    assertions: list[ComponentRoleAssertion] = []
    for entry in entries:
        normalized_match = normalize_identifier_value(entry.match_raw_value)
        hits = [
            identifier
            for identifier in identifiers
            if identifier.namespace == entry.match_namespace
            and identifier.normalized_value == normalized_match
        ]
        if not hits:
            continue
        # Deterministic one-to-one per component; one-to-many values surface
        # as multiple candidates at resolution time, never a silent pick.
        assertions.append(
            ComponentRoleAssertion(
                role_id=entry.role_id,
                display_name=entry.display_name,
                source_kind="curated_catalog",
                source_file=entry.source_file or source_default,
                source_locator={
                    "catalog_entry_id": entry.entry_id,
                    "catalog_version": entry.catalog_version,
                    "matched_namespace": entry.match_namespace,
                },
                confidence=float(entry.confidence),
                assertion_mode="catalog_match",
            )
        )
    return assertions


def build_component_identities(
    design: CircuitDesign,
    catalog_entries: Iterable[CuratedCatalogEntry] = (),
) -> list[ComponentIdentity]:
    """Derive the fully recomputable identity projection for one design."""
    entries = list(catalog_entries)
    source_file = design.files[0].file_name if design.files else design.design_id
    identities: list[ComponentIdentity] = []
    for instance in sorted(design.instances, key=lambda item: item.refdes):
        if not instance.refdes:
            continue
        identifiers = _identifiers_for_instance(instance)
        aliases = [
            ComponentAlias(
                value=identifier.raw_value,
                origin_kind="identifier",
                origin_key=f"{identifier.namespace}:{identifier.raw_value}",
            )
            for identifier in identifiers
        ]
        roles = _explicit_role_assertions(instance, source_file)
        roles.extend(_catalog_role_assertions(identifiers, entries, source_file))
        identities.append(
            ComponentIdentity(
                refdes=instance.refdes,
                identifiers=identifiers,
                aliases=aliases,
                roles=roles,
            )
        )
    return identities


def identities_for_designs(
    designs: Sequence[CircuitDesign],
    catalog_entries: Iterable[CuratedCatalogEntry] = (),
) -> dict[str, dict[str, ComponentIdentity]]:
    """Per-design identity maps, computing projections when absent."""
    entries = list(catalog_entries)
    result: dict[str, dict[str, ComponentIdentity]] = {}
    for design in designs:
        stored = {identity.refdes: identity for identity in design.component_identities}
        if not stored:
            stored = {
                identity.refdes: identity
                for identity in build_component_identities(design, entries)
            }
        result[design.design_id] = stored
    return result


def resolve_entity_mention(
    mention: str,
    designs: Sequence[CircuitDesign],
    catalog_entries: Iterable[CuratedCatalogEntry] = (),
) -> EntityResolutionResult:
    """Resolve one mention to evidence-backed candidates over authorized designs.

    Match priority: exact refdes > exact namespaced identifier > controlled
    alias > role assertion. Substring guessing is intentionally unsupported.
    """
    term = str(mention or "").strip()
    normalized_term = normalize_identifier_value(term)
    result = EntityResolutionResult(intent_kind="identifier", term=term)

    family_roles: tuple[str, ...] | None = None
    role_query: str | None = None
    if normalized_term in FAMILY_SYNONYMS:
        result.intent_kind = "role"
        family_roles = FAMILY_SYNONYMS[normalized_term]
        role_query = CONTROLLER_FAMILY
    elif normalized_term in ROLE_SYNONYMS:
        result.intent_kind = "role"
        role_query = ROLE_SYNONYMS[normalized_term]
    result.role_query = role_query
    result.controller_family_roles = family_roles

    identity_maps = identities_for_designs(designs, catalog_entries)
    candidates: list[IdentityCandidate] = []
    for design in sorted(designs, key=lambda item: (item.design_id, item.kb_name)):
        identities = identity_maps.get(design.design_id, {})
        for refdes in sorted(identities):
            identity = identities[refdes]
            candidate = _match_identity(
                design,
                identity,
                result.intent_kind,
                normalized_term,
                role_query,
                family_roles,
            )
            if candidate is not None:
                candidates.append(candidate)

    result.candidates = candidates[:MAX_RETURNED_CANDIDATES]
    result.candidate_count = len(candidates)
    result.returned_candidate_count = len(result.candidates)
    if result.candidate_count == 0:
        result.resolution_status = "no_evidence"
    elif result.candidate_count == 1:
        result.resolution_status = "unique"
    else:
        result.resolution_status = "ambiguous"
    return result


def _match_identity(
    design: CircuitDesign,
    identity: ComponentIdentity,
    intent_kind: str,
    normalized_term: str,
    role_query: str | None,
    family_roles: tuple[str, ...] | None,
) -> IdentityCandidate | None:
    if intent_kind == "role":
        matched_roles = [
            role
            for role in identity.roles
            if role_query and (role.role_id == role_query or (family_roles and role.role_id in family_roles))
        ]
        if not matched_roles:
            return None
        return IdentityCandidate(
            design_id=design.design_id,
            kb_name=design.kb_name,
            refdes=identity.refdes,
            matched_by="role_assertion",
            matched_value=str(role_query),
            matched_role_ids=tuple(role.role_id for role in matched_roles),
            identifiers=tuple(identity.identifiers),
            roles=tuple(identity.roles),
        )

    if identity.refdes.casefold() == normalized_term:
        return IdentityCandidate(
            design_id=design.design_id,
            kb_name=design.kb_name,
            refdes=identity.refdes,
            matched_by="refdes_exact",
            matched_value=identity.refdes,
            identifiers=tuple(identity.identifiers),
            roles=tuple(identity.roles),
        )
    for identifier in identity.identifiers:
        if identifier.normalized_value == normalized_term:
            return IdentityCandidate(
                design_id=design.design_id,
                kb_name=design.kb_name,
                refdes=identity.refdes,
                matched_by="identifier_exact",
                matched_value=identifier.raw_value,
                identifiers=tuple(identity.identifiers),
                roles=tuple(identity.roles),
            )
    for alias in identity.aliases:
        if normalize_identifier_value(alias.value) == normalized_term:
            return IdentityCandidate(
                design_id=design.design_id,
                kb_name=design.kb_name,
                refdes=identity.refdes,
                matched_by="alias",
                matched_value=alias.value,
                identifiers=tuple(identity.identifiers),
                roles=tuple(identity.roles),
            )
    return None
