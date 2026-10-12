"""Execution-store factory driven by ``CONTROL_PLANE_BACKEND`` (v3 §5.2).

The provider guard keeps the PostgreSQL backend fail-closed until the full
conversation/memory port and the cutover rehearsal land; this factory is what
the ``ConversationService`` facade will call once the switch is enabled.
"""

from __future__ import annotations

import threading
from typing import Any

_CONVERSATION_LOCK = threading.Lock()
_CONVERSATION_SERVICES: dict[tuple[str, int], Any] = {}


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

        pool_size = int(getattr(settings_module, "CONTROL_PLANE_POOL_SIZE", 8) or 8)
        key = (dsn, pool_size)
        # 每次调用都新建会各自带一个连接池且从不关闭；进程内共享同一个实例。
        with _CONVERSATION_LOCK:
            service = _CONVERSATION_SERVICES.get(key)
            if service is None:
                service = PostgresConversationService(dsn, pool_size=pool_size)
                _CONVERSATION_SERVICES[key] = service
            return service
    if backend != "sqlite":
        raise RuntimeError(f"unsupported CONTROL_PLANE_BACKEND: {backend!r}")
    from src.core.conversation import ConversationService

    return ConversationService(getattr(settings_module, "AUTH_DB_PATH", None))


def close_conversation_services() -> None:
    """关闭并清空进程内共享的 PostgreSQL conversation service（测试/关停用）。"""
    with _CONVERSATION_LOCK:
        services = list(_CONVERSATION_SERVICES.values())
        _CONVERSATION_SERVICES.clear()
    for service in services:
        close = getattr(service, "close", None)
        if close is not None:
            close()


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
