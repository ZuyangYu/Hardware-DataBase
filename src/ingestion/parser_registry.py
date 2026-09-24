from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.ingestion.evidence_capability import EvidenceCapability


# ParserFactory previously returned list[llama_index.core.schema.BaseNode],
# which hard-coupled the live agent import chain (agents.graph ->
# ingestion.parser_registry) to LlamaIndex -- a dependency the architecture
# doc (§8) says must not be reintroduced. The factories are unused on the live
# path (only capabilities_for is consumed by the agent), so the return type is
# loosened to break that coupling without changing runtime behavior.
ParserFactory = Callable[
    [str, str, str, Callable[[int, str], None] | None],
    list[Any],
]


@dataclass(frozen=True)
class DomainManifest:
    name: str
    source_groups: tuple[str, ...] = ()
    parser_factories: dict[str, ParserFactory] = field(default_factory=dict)
    capabilities: dict[str, tuple[EvidenceCapability, ...]] = field(default_factory=dict)


class ParserRegistry:
    def __init__(self):
        self._parsers: dict[str, ParserFactory] = {}
        self._capabilities: dict[str, tuple[EvidenceCapability, ...]] = {}

    def register_manifest(self, manifest: DomainManifest):
        for group, parser in manifest.parser_factories.items():
            self._parsers[group] = parser
        for group, capabilities in manifest.capabilities.items():
            self._capabilities[group] = tuple(capabilities)

    def get_parser(self, source_group: str) -> ParserFactory | None:
        return self._parsers.get(source_group)

    def implemented_groups(self) -> set[str]:
        return set(self._parsers)

    def capabilities_for(self, source_group: str) -> tuple[EvidenceCapability, ...]:
        return self._capabilities.get(source_group, ())


PARSER_REGISTRY = ParserRegistry()
