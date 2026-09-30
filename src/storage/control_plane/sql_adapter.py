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


class AdapterRow(dict):
    """``sqlite3.Row`` 形状：既支持 ``row["col"]`` 也支持 ``row[0]``。

    移植的域 store 里两种写法都有（``row[0]`` 常见于 COUNT/GROUP BY 查询），
    因此行对象必须同时支持列名与位置访问；迭代语义也与 sqlite3.Row 一致
    （迭代的是**值**而不是键）。
    """

    __slots__ = ("_columns",)

    def __init__(self, values: dict, columns: tuple[str, ...]):
        super().__init__(values)
        self._columns = columns

    def __getitem__(self, key):
        if isinstance(key, int):
            return super().__getitem__(self._columns[key])
        return super().__getitem__(key)

    def __iter__(self):
        for name in self._columns:
            yield super().__getitem__(name)


def _wrap_row(row, description):
    if row is None:
        return None
    if not isinstance(row, dict):
        return row
    # 某些游标（测试替身/自定义驱动）没有 description：退化为按值字典的键序。
    columns = tuple(str(column[0]) for column in (description or ()))
    values = {key: normalize_value(value, key) for key, value in row.items()}
    if not columns:
        columns = tuple(values.keys())
    return AdapterRow(values, columns)


class PgSqlAdapterCursor:
    def __init__(self, cursor, *, lastrowid: int | None = None):
        self._cursor = cursor
        self.rowcount = cursor.rowcount
        self._lastrowid = lastrowid

    @property
    def lastrowid(self):
        """SQLite ``cursor.lastrowid`` 的 PG 等价物。

        INSERT 执行时立刻用 ``lastval()`` 取值并缓存（连接随后就会被关闭，
        因此不能等到访问属性时再查）。只对使用 IDENTITY 序列的插入有意义；
        显式提供 id 的插入不要读它。
        """
        return int(self._lastrowid or 0)

    def fetchone(self):
        return _wrap_row(
            self._cursor.fetchone(), getattr(self._cursor, "description", None)
        )

    def fetchall(self):
        description = getattr(self._cursor, "description", None)
        return [_wrap_row(row, description) for row in self._cursor.fetchall()]

    def __iter__(self):
        """支持 ``for row in conn.execute(...)`` 的 SQLite 用法。"""
        description = getattr(self._cursor, "description", None)
        for row in self._cursor:
            yield _wrap_row(row, description)

    def executemany(self, sql, seq_of_params):
        self._cursor.executemany(sql, seq_of_params)
        self.rowcount = self._cursor.rowcount
        return self


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
        # SQLite 的 datetime(...) 在 PostgreSQL 上不存在，按三种实际用法翻译：
        #   datetime('now', X)   -> (now() + CAST(X AS interval))   例如 '-7 days' / '-' || ? || ' days'
        #   datetime('now')      -> now()
        #   datetime(<列名>)      -> CAST(<列名> AS timestamptz)      text 列(ISO 串)与 timestamptz 列都适用
        text = re.sub(
            r"datetime\(\s*'now'\s*,\s*([^()]+?)\s*\)",
            r"(now() + CAST(\1 AS interval))",
            text,
            flags=re.I,
        )
        text = re.sub(r"datetime\(\s*'now'\s*\)", "now()", text, flags=re.I)
        text = re.sub(
            r"datetime\(\s*([A-Za-z_][\w.]*)\s*\)",
            r"CAST(\1 AS timestamptz)",
            text,
            flags=re.I,
        )
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
        text = self._sql(sql)
        cursor = self._conn.execute(text, params)
        lastrowid = None
        if text.lstrip().upper().startswith("INSERT") and "RETURNING" not in text.upper():
            # SQLite 的 cursor.lastrowid 在游标关闭后依然可读，所以必须当场取。
            # lastval() 在没有序列的会话里会报错：autocommit 下失败只影响该语句，
            # 但在显式事务里**会中止外层事务**，因此事务内要用 SAVEPOINT 包住
            # 探测（autocommit 下 PG 不允许 SAVEPOINT）。
            try:
                from psycopg.pq import TransactionStatus

                in_transaction = (
                    self._conn.info.transaction_status != TransactionStatus.IDLE
                )
            except Exception:  # noqa: BLE001 - 探测失败按 autocommit 处理
                in_transaction = False
            try:
                if in_transaction:
                    self._conn.execute("SAVEPOINT hdb_lastrowid_probe")
                row = self._conn.execute("SELECT lastval()").fetchone()
                if in_transaction:
                    self._conn.execute("RELEASE SAVEPOINT hdb_lastrowid_probe")
                if row is None:
                    lastrowid = None
                elif isinstance(row, dict):  # dict_row 行工厂
                    lastrowid = int(next(iter(row.values())))
                else:
                    lastrowid = int(row[0])
            except Exception:  # noqa: BLE001 - 无序列/未定义时按 0 处理
                lastrowid = None
                if in_transaction:
                    for cleanup in (
                        "ROLLBACK TO SAVEPOINT hdb_lastrowid_probe",
                        "RELEASE SAVEPOINT hdb_lastrowid_probe",
                    ):
                        try:
                            self._conn.execute(cleanup)
                        except Exception:  # noqa: BLE001 - 清理尽力而为
                            break
        return PgSqlAdapterCursor(cursor, lastrowid=lastrowid)

    def executemany(self, sql: str, seq_of_params):
        # psycopg3 的 Connection 没有 executemany（只有 Cursor 有）。
        cursor = self._conn.cursor()
        cursor.executemany(self._sql(sql), seq_of_params)
        return PgSqlAdapterCursor(cursor)

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
