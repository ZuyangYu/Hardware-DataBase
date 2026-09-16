"""SQLite checkpointer provider (single-host default)."""

from __future__ import annotations

import os
import sqlite3
from typing import Any


class SqliteCheckpointerProvider:
    backend = "sqlite"

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._connection: sqlite3.Connection | None = None

    def create(self) -> Any:
        from langgraph.checkpoint.sqlite import SqliteSaver

        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if self._connection is None:
            # LangGraph's saver serializes access internally; the connection is
            # shared across worker threads by design.
            self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        return SqliteSaver(self._connection)

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
