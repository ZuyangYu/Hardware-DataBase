"""PostgreSQL checkpointer provider (multi-instance target, P1-001).

Requires ``langgraph-checkpoint-postgres`` and ``psycopg``.  The schema is
initialized with the official ``setup()``; migrations and history/delete
compatibility must be validated by the P1 migration runbook before switching
production writes.

The saver runs on a ``psycopg_pool.ConnectionPool`` when available, so
concurrent API/Worker threads never share one connection (psycopg connections
are not safe for concurrent use).  Pool size is bounded by
``AGENT_CHECKPOINT_POOL_SIZE`` (default 4) and the pool is closed with the
provider.
"""

from __future__ import annotations

from typing import Any

import src.settings as settings


class PostgresCheckpointerProvider:
    backend = "postgres"

    def __init__(self, database_url: str, *, setup: bool = True) -> None:
        self.database_url = str(database_url or "").strip()
        self.setup = bool(setup)
        self._connection: Any | None = None
        self._pool: Any | None = None
        # Per-graph savers share this provider; schema setup runs once.
        self._setup_done = False

    def _pool_size(self) -> int:
        try:
            return max(1, int(getattr(settings, "AGENT_CHECKPOINT_POOL_SIZE", 4) or 4))
        except (TypeError, ValueError):
            return 4

    def create(self) -> Any:
        if not self.database_url:
            raise ValueError("AGENT_CHECKPOINT_DATABASE_URL is required for the postgres backend")
        try:
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "postgres checkpoint backend requires langgraph-checkpoint-postgres and psycopg"
            ) from exc

        pool_cls = None
        try:
            from psycopg_pool import ConnectionPool as pool_cls  # type: ignore[assignment]
        except ImportError:  # pragma: no cover - optional dependency
            pool_cls = None

        if pool_cls is not None:
            if self._pool is None:
                self._pool = pool_cls(
                    self.database_url,
                    min_size=1,
                    max_size=self._pool_size(),
                    kwargs={"autocommit": True},
                    open=True,
                    name="hdb-agent-checkpoint",
                )
            saver = PostgresSaver(self._pool)
        else:
            if self._connection is None:
                self._connection = psycopg.connect(self.database_url, autocommit=True)
            saver = PostgresSaver(self._connection)
        # ``setup`` is idempotent.  Re-run it for each saver so a maintenance
        # rehearsal or test that recreates the target schema cannot leave a
        # process-level provider pointing at missing checkpoint tables.
        if self.setup:
            saver.setup()
            self._setup_done = True
        return saver

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.close()
            finally:
                self._pool = None
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
