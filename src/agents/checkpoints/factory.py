"""Process-level agent checkpointer assembly (P1-001)."""

from __future__ import annotations

import threading
from typing import Any

from .base import CheckpointerProvider, normalize_backend
from .postgres_provider import PostgresCheckpointerProvider
from .sqlite_provider import SqliteCheckpointerProvider

_PROVIDER: CheckpointerProvider | None = None
_CHECKPOINTER: Any | None = None
_LOCK = threading.Lock()


def build_checkpointer_provider(
    *,
    backend: str | None = None,
    sqlite_path: str | None = None,
    database_url: str | None = None,
    setup: bool = True,
) -> CheckpointerProvider:
    import src.settings as settings

    resolved = normalize_backend(backend if backend is not None else getattr(settings, "AGENT_CHECKPOINT_BACKEND", "sqlite"))
    if resolved == "postgres":
        return PostgresCheckpointerProvider(
            database_url
            if database_url is not None
            else str(getattr(settings, "AGENT_CHECKPOINT_DATABASE_URL", "") or ""),
            setup=setup,
        )
    return SqliteCheckpointerProvider(
        sqlite_path
        if sqlite_path is not None
        else str(getattr(settings, "AGENT_CHECKPOINT_DB_PATH", "storage/agent_checkpoints.db"))
    )


def get_checkpointer() -> Any:
    """Return the process-level saver singleton for the configured backend."""
    global _PROVIDER, _CHECKPOINTER
    with _LOCK:
        if _CHECKPOINTER is None:
            _PROVIDER = build_checkpointer_provider()
            _CHECKPOINTER = _PROVIDER.create()
        return _CHECKPOINTER


def get_checkpointer_provider() -> CheckpointerProvider | None:
    return _PROVIDER


def reset_checkpointer() -> None:
    """Close and drop the singleton; used by tests and graceful shutdown."""
    global _PROVIDER, _CHECKPOINTER
    with _LOCK:
        if _PROVIDER is not None:
            try:
                _PROVIDER.close()
            finally:
                _PROVIDER = None
        _CHECKPOINTER = None
