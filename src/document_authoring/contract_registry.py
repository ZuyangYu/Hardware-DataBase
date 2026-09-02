"""Single-source allowlist registry for inferred field contracts (Task 2).

Every value_type / capability / source role / missing policy that the
deterministic contract inference may emit must be registered here with a
version. Unknown capabilities never disappear silently: callers report them as
diagnostics or route to human confirmation. The capability allowlist is kept
consistent with the retriever registry's supported set.
"""

from __future__ import annotations

from typing import Literal

CONTRACT_REGISTRY_VERSION = "1"

ValueType = Literal[
    "text", "number", "integer", "float", "date", "version", "boolean", "enum", "table",
]

VALUE_TYPES: frozenset[str] = frozenset({
    "text", "number", "integer", "float", "date", "version", "boolean", "enum", "table",
})

CAPABILITIES: frozenset[str] = frozenset({
    "entity_lookup", "relationship_lookup", "tabular_lookup",
    "document_claim_lookup", "revision_lookup",
})

SOURCE_ROLES: frozenset[str] = frozenset({
    "released_schematic", "released_design", "datasheet", "specification",
    "test_report", "standard_document", "interface_control_document",
})

MISSING_POLICIES: frozenset[str] = frozenset({"mark_tbd", "keep_blank", "block_section"})


def supported_capabilities(values: list[str]) -> tuple[list[str], list[str]]:
    """Split capability names into (supported, unsupported) allowlist sets.

    Unsupported capabilities are returned so callers can surface diagnostics
    or require human confirmation instead of silently dropping them.
    """
    supported: list[str] = []
    unsupported: list[str] = []
    for value in values:
        if value in CAPABILITIES:
            if value not in supported:
                supported.append(value)
        elif value not in unsupported:
            unsupported.append(value)
    return supported, unsupported


def normalized_value_type(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return text if text in VALUE_TYPES else None


def normalized_missing_policy(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return text if text in MISSING_POLICIES else None


def normalized_source_roles(values: list[str] | None) -> list[str]:
    return [role for role in (values or []) if role in SOURCE_ROLES]
