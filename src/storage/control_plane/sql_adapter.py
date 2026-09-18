"""SQLite-dialect translation layer for PostgreSQL adapters (v3 §5.3).

Several legacy repositories speak a small SQLite dialect (``?`` parameters,
``BEGIN IMMEDIATE``, ``INSERT OR IGNORE``, JSON stored as TEXT, rows accessed
by column name).  The control-plane PostgreSQL twins reuse those repositories
unchanged by wrapping a psycopg connection in this adapter: it rewrites the
dialect on the way out and normalizes values on the way back so callers see
exactly the shapes they would get from a ``sqlite3.Row`` connection.

The wrappers deliberately do not own the underlying connection: callers decide
the transaction boundary (explicit BEGIN/COMMIT or psycopg autocommit).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any


def normalize_value(value: Any, key: str = "") -> Any:
    """Match the SQLite TEXT/JSON/TIMESTAMP shapes callers already expect."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if key.endswith("_json") and isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


class PgSqlAdapterCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self._cursor.fetchone()
        return (
            None
            if row is None
            else {k: normalize_value(v, k) for k, v in row.items()}
        )

    def fetchall(self):
        return [
            {k: normalize_value(v, k) for k, v in row.items()}
            for row in self._cursor.fetchall()
        ]


class PgSqlAdapterConnection:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self.rollback()
        self.close()

    @staticmethod
    def _sql(sql: str) -> str:
        text = str(sql).replace("BEGIN IMMEDIATE", "BEGIN")
        text = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", text, flags=re.I)
        # All INSERT OR IGNORE statements in the ported repositories have no
        # RETURNING clause; append the equivalent PostgreSQL conflict action.
        if (
            "INSERT INTO" in text.upper()
            and "ON CONFLICT" not in text.upper()
            and "OR IGNORE" in str(sql).upper()
        ):
            text = text.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return text.replace("?", "%s")

    def execute(self, sql: str, params=()):
        return PgSqlAdapterCursor(self._conn.execute(self._sql(sql), params))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


__all__ = [
    "PgSqlAdapterConnection",
    "PgSqlAdapterCursor",
    "normalize_value",
]
