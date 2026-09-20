"""Checkpointer provider contract (P1-001).

Providers only assemble the official LangGraph saver for a configured backend;
the checkpoint protocol itself is never reimplemented.
"""

from __future__ import annotations

from typing import Any, Protocol


class CheckpointerProvider(Protocol):
    backend: str

    def create(self) -> Any: ...

    def close(self) -> None: ...


def normalize_backend(backend: str | None) -> str:
    """Return a supported backend name; unknown values fall back to sqlite."""
    value = str(backend or "sqlite").strip().lower()
    return value if value in {"sqlite", "postgres"} else "sqlite"
