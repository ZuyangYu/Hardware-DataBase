from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from llama_index.core.schema import BaseNode


ParserFactory = Callable[
    [str, str, str, Callable[[int, str], None] | None],
    list[BaseNode],
]


@dataclass(frozen=True)
class DomainManifest:
    name: str
    source_groups: tuple[str, ...] = ()
    parser_factories: dict[str, ParserFactory] = field(default_factory=dict)


class ParserRegistry:
    def __init__(self):
        self._parsers: dict[str, ParserFactory] = {}

    def register_manifest(self, manifest: DomainManifest):
        for group, parser in manifest.parser_factories.items():
            self._parsers[group] = parser

    def get_parser(self, source_group: str) -> ParserFactory | None:
        return self._parsers.get(source_group)

    def implemented_groups(self) -> set[str]:
        return set(self._parsers)


PARSER_REGISTRY = ParserRegistry()
