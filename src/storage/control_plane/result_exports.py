"""PostgreSQL adapter for the result-export repository (v3 §5.1).

The export ledger is part of the control-plane atomic group: ``complete_turn``
must register the snapshot and jobs in the same transaction as the answer, and
the export worker must claim/publish from the same authority.  This adapter
reuses the SQLite repository implementation through the shared SQL dialect
translation layer, mirroring the stable value shapes callers already expect.
"""

from __future__ import annotations

import os
from pathlib import Path

import src.settings as settings

from .sql_adapter import PgSqlAdapterConnection


class PostgresResultExportStore:
    """Drop-in ``ResultExportStore`` backed by the control-plane schema."""

    def __init__(self, database_url: str, *, storage_dir: str | None = None) -> None:
        self.database_url = str(database_url)
        # ``db_path`` is only used by auxiliary SQLite services (log center);
        # keep it pointing at the local auth DB for those references.
        self.db_path = str(getattr(settings, "AUTH_DB_PATH", "") or "")
        self.storage_dir = Path(
            storage_dir
            or getattr(
                settings,
                "RESULT_EXPORT_STORAGE_DIR",
                os.path.join(settings.STORAGE_DIR, "exports"),
            )
        ).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.storage_dir, 0o700)
        except OSError:
            pass

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        # Mirror sqlite3 ``isolation_level=None``: standalone repository
        # methods commit themselves; explicit BEGIN remains supported.
        return PgSqlAdapterConnection(
            psycopg.connect(
                self.database_url, row_factory=dict_row, autocommit=True
            )
        )

    def __getattr__(self, name):
        from src.result_exports.store import ResultExportStore

        attr = getattr(ResultExportStore, name, None)
        if attr is None:
            raise AttributeError(name)
        return attr.__get__(self, type(self)) if callable(attr) else attr


__all__ = ["PostgresResultExportStore"]
