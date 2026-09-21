"""PgSqlAdapter：把 SQLite 写法的仓储接到 PostgreSQL（方言改写 + 结果规范化）。

这层是"同一套仓储跑两个后端"的关键；缺失的方法会以 500 的形式在运行期暴露
（记忆 consent 流程曾因缺 `executemany` 直接报 AttributeError）。
"""

from __future__ import annotations

import unittest

from src.storage.control_plane.sql_adapter import PgSqlAdapterConnection, PgSqlAdapterCursor


class _FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.rowcount = len(self._rows)
        self.many: list[tuple] = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def executemany(self, sql, seq_of_params):
        self.many.append((sql, list(seq_of_params)))
        self.rowcount = len(seq_of_params)


class _FakeConn:
    def __init__(self):
        self.executed: list[tuple] = []
        self.many: list[tuple] = []
        self.closed_cursors: list = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return _FakeCursor()

    def cursor(self):
        cursor = _FakeCursor()
        self.closed_cursors.append(cursor)
        return cursor


class SqlAdapterDialectTests(unittest.TestCase):
    def test_dialect_rewriting(self):
        rewrite = PgSqlAdapterConnection._sql
        self.assertEqual(
            rewrite("SELECT * FROM t WHERE a = ?"), "SELECT * FROM t WHERE a = %s"
        )
        self.assertEqual(rewrite("BEGIN IMMEDIATE"), "BEGIN")
        self.assertIn(
            "ON CONFLICT DO NOTHING", rewrite("INSERT OR IGNORE INTO t (a) VALUES (?)")
        )
        self.assertNotIn(
            "ON CONFLICT", rewrite("INSERT INTO t (a) VALUES (?)")
        )

    def test_executemany_forwards_with_rewriting(self):
        conn = _FakeConn()
        adapter = PgSqlAdapterConnection(conn)
        adapter.executemany(
            "INSERT OR IGNORE INTO t (a) VALUES (?)", [(1,), (2,)]
        )
        self.assertEqual(len(conn.closed_cursors), 1)
        cursor = conn.closed_cursors[0]
        self.assertEqual(len(cursor.many), 1)
        sql, seq = cursor.many[0]
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertIn("%s", sql)
        self.assertEqual(seq, [(1,), (2,)])

    def test_cursor_executemany_is_available(self):
        fake = _FakeCursor()
        cursor = PgSqlAdapterCursor(fake)
        cursor.executemany("INSERT INTO t (a) VALUES (%s)", [(1,), (2,)])
        self.assertEqual(fake.many[0][1], [(1,), (2,)])
        self.assertEqual(cursor.rowcount, 2)

    def test_cursor_normalizes_rows(self):
        from datetime import datetime, timezone

        fake = _FakeCursor([{"created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)}])
        row = PgSqlAdapterCursor(fake).fetchone()
        self.assertEqual(row["created_at"], "2026-01-02T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
