"""Process-local cache of discovered MCP tools.

The cache key is ``slug:connection_fingerprint``; only the connection identity
participates, so toggling a tool's disabled flag does not force a reconnect.
Cached objects are the stateless async adapter tools plus discovery stats —
per-turn policy, events and truncation belong to the executor, never here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CachedServerTools:
    """One discovery result for a server at a specific connection fingerprint."""

    slug: str
    fingerprint: str
    tools: tuple[Any, ...]
    stats: dict[str, int] = field(default_factory=dict)
    fetched_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "fingerprint": self.fingerprint,
            "tool_count": len(self.tools),
            "stats": dict(self.stats),
            "fetched_at": self.fetched_at,
        }


class McpToolCache:
    """Thread-safe cache; at most one fingerprint per slug is retained."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, CachedServerTools] = {}

    def get(self, slug: str, fingerprint: str) -> CachedServerTools | None:
        with self._lock:
            item = self._items.get(str(slug))
        if item is None or item.fingerprint != str(fingerprint):
            return None
        return item

    def put(self, item: CachedServerTools) -> None:
        with self._lock:
            self._items[item.slug] = item

    def clear(self, slug: str) -> None:
        with self._lock:
            self._items.pop(str(slug), None)

    def clear_all(self) -> None:
        with self._lock:
            self._items.clear()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {slug: item.to_dict() for slug, item in self._items.items()}

    def build_item(
        self,
        *,
        slug: str,
        fingerprint: str,
        tools: list[Any],
        stats: dict[str, int] | None = None,
    ) -> CachedServerTools:
        return CachedServerTools(
            slug=str(slug),
            fingerprint=str(fingerprint),
            tools=tuple(tools),
            stats=dict(stats or {}),
            fetched_at=time.time(),
        )


_CACHE = McpToolCache()


def get_tool_cache() -> McpToolCache:
    """Process-wide MCP tool cache."""

    return _CACHE


__all__ = ["CachedServerTools", "McpToolCache", "get_tool_cache"]
