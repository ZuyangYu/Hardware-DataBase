"""Execution-store factory driven by ``CONTROL_PLANE_BACKEND`` (v3 §5.2).

The provider guard keeps the PostgreSQL backend fail-closed until the full
conversation/memory port and the cutover rehearsal land; this factory is what
the ``ConversationService`` facade will call once the switch is enabled.
"""

from __future__ import annotations

from typing import Any


def build_conversation_service(settings_module: Any | None = None):
    """Return the configured conversation service (SQLite default).

    ``ConversationService.__new__`` calls this so every existing construction
    site keeps working; the PostgreSQL branch is reachable only after the
    provider guard is released.
    """
    if settings_module is None:
        import src.settings as settings_module  # type: ignore[no-redef]

    backend = (
        str(getattr(settings_module, "CONTROL_PLANE_BACKEND", "sqlite") or "sqlite")
        .strip()
        .lower()
    )
    if backend == "postgres":
        dsn = str(
            getattr(settings_module, "CONTROL_PLANE_DATABASE_URL", "") or ""
        ).strip()
        if not dsn:
            raise RuntimeError(
                "CONTROL_PLANE_BACKEND=postgres requires CONTROL_PLANE_DATABASE_URL"
            )
        from .conversation_service import PostgresConversationService

        return PostgresConversationService(
            dsn,
            pool_size=int(getattr(settings_module, "CONTROL_PLANE_POOL_SIZE", 8) or 8),
        )
    if backend != "sqlite":
        raise RuntimeError(f"unsupported CONTROL_PLANE_BACKEND: {backend!r}")
    from src.core.conversation import ConversationService

    return ConversationService(getattr(settings_module, "AUTH_DB_PATH", None))


def build_execution_store(settings_module: Any | None = None):
    """Return the configured execution store (SQLite default)."""
    if settings_module is None:
        import src.settings as settings_module  # type: ignore[no-redef]

    backend = (
        str(getattr(settings_module, "CONTROL_PLANE_BACKEND", "sqlite") or "sqlite")
        .strip()
        .lower()
    )
    if backend == "sqlite":
        from .sqlite import SqliteExecutionStore

        return SqliteExecutionStore(getattr(settings_module, "AUTH_DB_PATH", None))
    if backend == "postgres":
        dsn = str(
            getattr(settings_module, "CONTROL_PLANE_DATABASE_URL", "") or ""
        ).strip()
        if not dsn:
            raise RuntimeError(
                "CONTROL_PLANE_BACKEND=postgres requires CONTROL_PLANE_DATABASE_URL"
            )
        from .postgres import PostgresExecutionStore

        return PostgresExecutionStore(
            dsn,
            max_size=int(getattr(settings_module, "CONTROL_PLANE_POOL_SIZE", 8) or 8),
        )
    raise RuntimeError(f"unsupported CONTROL_PLANE_BACKEND: {backend!r}")
