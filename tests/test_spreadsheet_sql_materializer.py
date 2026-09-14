import os
import sqlite3
import tempfile
import unittest

from src.pipelines.spreadsheet.sql_materializer import (
    drop_record_tables,
    ensure_registry_table,
    list_registry_for_document,
    materialize_sheet,
)
from src.agents.tools.spreadsheet_tools import (
    _execute_readonly_sql,
    _format_sql_result,
    _validate_readonly_sql,
)
def _semantic_row(row_index: int, values: dict, raw_values: dict | None = None) -> dict:
    return {
        "sheet_name": "S",
        "row_index": row_index,
        "header_row_index": 1,
        "section_title": "",
        "values": values,
        "raw_values": raw_values if raw_values is not None else values,
    }


class SqlMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_registry_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_types_are_inferred_per_column(self):
        rows = [
            _semantic_row(2, {"名称": "R1921", "数量": "12", "容值": "22.5"}),
            _semantic_row(3, {"名称": "R1922", "数量": "1,034", "容值": "0.1"}),
        ]
        schema = materialize_sheet(self.conn, 1, "S", rows, table_seq=1)
        dtypes = {col["header"]: col["dtype"] for col in schema["columns"]}
        self.assertEqual(dtypes["名称"], "TEXT")
        self.assertEqual(dtypes["数量"], "INTEGER")
        self.assertEqual(dtypes["容值"], "REAL")

    def test_samples_and_header_mapping_present(self):
        rows = [
            _semantic_row(2, {"器件": "C1618", "容值": "22uF"}),
            _semantic_row(3, {"器件": "C1617", "容值": "47uF"}),
            _semantic_row(4, {"器件": "C1700", "容值": "10uF"}),
            _semantic_row(5, {"器件": "C1701", "容值": "100nF"}),
        ]
        schema = materialize_sheet(self.conn, 1, "S", rows, table_seq=1)
        by_header = {col["header"]: col for col in schema["columns"]}
        self.assertEqual(by_header["器件"]["samples"][:3], ["C1618", "C1617", "C1700"])
        self.assertEqual(by_header["器件"]["column"], "col_1")
        self.assertEqual(by_header["容值"]["column"], "col_2")

    def test_physical_table_queryable_and_values_materialized(self):
        # 生产链路里 _sheet_semantic_rows 已对 values 做前向填充(第 3 行类别
        # 由上一行继承); 物化层忠实落库 values, 不重复实现填充.
        rows = [
            _semantic_row(2, {"类别": "Memory", "子项": "RAM"}),
            _semantic_row(3, {"类别": "Memory", "子项": "DDR"}),
            _semantic_row(4, {"类别": "Power", "子项": "VCC"}),
        ]
        schema = materialize_sheet(self.conn, 1, "S", rows, table_seq=1)
        rows_out = self.conn.execute(
            f'SELECT row_index, col_1, col_2 FROM "{schema["table_name"]}" ORDER BY row_index'
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows_out], [
            (2, "Memory", "RAM"),
            (3, "Memory", "DDR"),
            (4, "Power", "VCC"),
        ])

    def test_row_index_preserved_for_hierarchy_queries(self):
        rows = [
            _semantic_row(30, {"调试项": "Memory"}),
            _semantic_row(31, {"子项": "RAM 时序波形"}),
            _semantic_row(32, {"子项": "DDR 压力测试"}),
        ]
        schema = materialize_sheet(self.conn, 1, "S", rows, table_seq=1)
        count = self.conn.execute(
            f'SELECT COUNT(*) FROM "{schema["table_name"]}" WHERE row_index > 30'
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_registry_lists_tables_and_drop_removes_them(self):
        rows = [_semantic_row(2, {"A": "1"})]
        schema = materialize_sheet(self.conn, 7, "S", rows, table_seq=3)
        self.assertEqual(schema["table_name"], "t_7_3")
        registry = list_registry_for_document(self.conn, 7)
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry[0]["table_name"], "t_7_3")
        drop_record_tables(self.conn, 7)
        tables = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t_7_3'"
        ).fetchall()
        self.assertEqual(tables, [])
        self.assertEqual(list_registry_for_document(self.conn, 7), [])

    def test_empty_sheet_returns_none(self):
        self.assertIsNone(materialize_sheet(self.conn, 1, "S", [], table_seq=1))

    def test_partial_first_row_does_not_shift_column_order(self):
        # pre_header_row 只覆盖部分列; 列序必须由覆盖列最多的行决定,
        # 否则 col_N 编号整体错位(物理表列与表头映射错位)。
        rows = [
            _semantic_row(1, {"Part": "X1900", "Type": "Conn"}),          # 部分列
            _semantic_row(2, {"Part": "A1", "Type": "T1", "Val": "5", "Note": "n1"}),
            _semantic_row(3, {"Part": "A2", "Type": "T2", "Val": "6", "Note": "n2"}),
        ]
        schema = materialize_sheet(self.conn, 1, "S", rows)
        mapping = {c["column"]: c["header"] for c in schema["columns"]}
        self.assertEqual(mapping, {
            "col_1": "Part", "col_2": "Type", "col_3": "Val", "col_4": "Note",
        })
        # 物理表数据也对齐: col_1 是 Part 列
        data = self.conn.execute(
            'SELECT col_1, col_3 FROM t_1_1 WHERE row_index = 3'
        ).fetchone()
        self.assertEqual((data["col_1"], data["col_3"]), ("A2", 6))


class _FakeRuntime:
    kb_name = "kb"
    ctx = None


class SqlValidationTests(unittest.TestCase):
    def setUp(self):
        self.allowed = {"t_1_1", "t_1_2"}

    def test_select_allowed_and_limit_injected(self):
        ast, error = _validate_readonly_sql("SELECT col_1 FROM t_1_1", self.allowed)
        self.assertIsNone(error)
        self.assertIn("LIMIT", ast.sql().upper())

    def test_non_select_rejected(self):
        for bad in (
            "UPDATE t_1_1 SET col_1 = 'x'",
            "DELETE FROM t_1_1",
            "INSERT INTO t_1_1 VALUES (1)",
            "PRAGMA query_only = OFF",
            "ATTACH DATABASE 'x' AS y",
        ):
            _, error = _validate_readonly_sql(bad, self.allowed)
            self.assertTrue(error, bad)

    def test_unknown_table_rejected(self):
        _, error = _validate_readonly_sql("SELECT * FROM sqlite_master", self.allowed)
        self.assertIn("未登记的表", error)

    def test_multiple_statements_rejected(self):
        _, error = _validate_readonly_sql(
            "SELECT 1 FROM t_1_1; SELECT 2 FROM t_1_1", self.allowed
        )
        self.assertTrue(error)

    def test_cte_select_allowed(self):
        ast, error = _validate_readonly_sql(
            "WITH a AS (SELECT col_1 FROM t_1_1) SELECT * FROM a", self.allowed
        )
        self.assertIsNone(error)

    def test_existing_large_limit_clamped(self):
        ast, error = _validate_readonly_sql(
            "SELECT col_1 FROM t_1_1 LIMIT 100000", self.allowed
        )
        self.assertIsNone(error)
        self.assertIn("LIMIT 200", ast.sql())

    def test_execute_readonly_blocks_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t_1_1 (col_1 TEXT)")
            conn.commit()
            conn.close()
            records, error = _execute_readonly_sql(db_path, "SELECT col_1 FROM t_1_1")
            self.assertIsNone(error)
            self.assertEqual(records, [])
            _, error = _execute_readonly_sql(db_path, "INSERT INTO t_1_1 VALUES ('x')")
            self.assertTrue(error)

    def test_format_result_truncates(self):
        records = [{"i": i, "pad": "x" * 120} for i in range(500)]
        text = _format_sql_result(records)
        self.assertIn("截断", text)


if __name__ == "__main__":
    unittest.main()
