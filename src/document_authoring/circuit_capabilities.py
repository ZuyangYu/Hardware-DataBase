"""Infer circuit retrieval capabilities from template field semantics.

The inference is deliberately additive: template authors may still declare a
capability explicitly, while common circuit terminology makes the structured
circuit index available without tying the document flow to any one project.
"""

from __future__ import annotations

from collections.abc import Iterable


_RELATIONSHIP_TERMS = (
    "pin",
    "pinout",
    "connector",
    "net",
    "network",
    "connection",
    "interconnect",
    "引脚",
    "管脚",
    "针脚",
    "接插件",
    "连接器",
    "网络",
    "连接",
)
_ENTITY_TERMS = (
    "model",
    "part number",
    "part_number",
    "part no",
    "manufacturer part",
    "reference designator",
    "refdes",
    "component",
    "型号",
    "料号",
    "物料号",
    "位号",
    "器件",
)


def enrich_circuit_capabilities(
    declared_capabilities: Iterable[str],
    *,
    label: str,
    description: str = "",
    query_terms: Iterable[str] | None = None,
) -> list[str]:
    """Return declared capabilities plus generic circuit capabilities.

    The function only looks at field wording.  It intentionally has no
    knowledge of reference designators, net names, projects, or templates.
    """

    capabilities = list(dict.fromkeys(value for value in declared_capabilities if value))
    text = " ".join(
        value.strip().casefold()
        for value in (label, description, *(query_terms or ()))
        if isinstance(value, str) and value.strip()
    )
    if any(term in text for term in _RELATIONSHIP_TERMS):
        capabilities.append("relationship_lookup")
    if any(term in text for term in _ENTITY_TERMS):
        capabilities.append("entity_lookup")
    return list(dict.fromkeys(capabilities))
