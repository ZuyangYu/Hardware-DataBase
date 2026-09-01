import gc
import os
import sqlite3
import tempfile
import unittest

from src.pipelines.spreadsheet.table_store import TableIndexStore, _infer_headers, _sheet_semantic_rows
from src.pipelines.spreadsheet.pipeline import SpreadsheetPipeline


class SpreadsheetTableStoreTests(unittest.TestCase):
    def test_table_index_store_requires_scoped_db_path(self):
        with self.assertRaises(ValueError):
            TableIndexStore(db_path="")

    def test_spreadsheet_pipeline_requires_scoped_store(self):
        with self.assertRaises(ValueError):
            SpreadsheetPipeline(None)

    def test_table_documents_persist_kb_id_in_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "table_indexes.db")
            store = TableIndexStore(db_path=db_path)
            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(table_documents)").fetchall()
                }
                self.assertIn("kb_id", columns)
                conn.execute(
                    """
                    INSERT INTO table_documents (
                        record_id, kb_id, kb_name, department_id, document_name,
                        source_group, local_path, content_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (7, 99, "kb_hw", "dept_hw", "hardware.xlsx", "design", "design/hardware.xlsx", "abc"),
                )
                conn.execute(
                    """
                    INSERT INTO table_sheets (
                        record_id, sheet_name, row_count, column_count,
                        non_empty_row_count, non_empty_cell_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (7, "Sheet1", 2, 3, 2, 4),
                )
                conn.commit()
            finally:
                conn.close()

            profile = store.get_document_profile(7)

            self.assertEqual(profile["record_id"], 7)
            self.assertEqual(profile["kb_id"], 99)
            self.assertEqual(profile["document_name"], "hardware.xlsx")

            del store
            gc.collect()

    def test_routing_rank_prefers_exact_hardware_entity_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "table_indexes.db")
            store = TableIndexStore(db_path=db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.executemany(
                    """
                    INSERT INTO table_cells (record_id, sheet_name, row_index, col_index, cell_ref, value, header, raw_value)
                    VALUES (?, 'BOM', 2, 1, 'A2', ?, 'Part Number', ?)
                    """,
                    [
                        (7, "U1800", "U1800"),
                        (8, "U1700", "U1700"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            ranked = store.rank_documents_by_terms(["u1800", "part"])

            self.assertIn(7, ranked)
            self.assertNotIn(8, ranked)
            self.assertEqual(ranked[7]["matched_terms"], ["part", "u1800"])
            self.assertGreaterEqual(ranked[7]["score"], 11)

    def test_pure_numeric_rows_are_not_inferred_as_headers(self):
        rows = [
            ["1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008"],
            ["10", "20", "30", "40", "50", "60", "70", "80"],
        ]

        self.assertEqual(_infer_headers(rows), [])

    def test_labeled_rows_are_still_inferred_as_headers(self):
        rows = [
            ["Part", "Voltage", "Current", "Power"],
            ["A1", "12", "2", "24"],
        ]

        headers = _infer_headers(rows)

        self.assertEqual([item["header"] for item in headers], ["Part", "Voltage", "Current", "Power"])

    def test_pre_header_data_rows_produce_semantic_rows(self):
        # PL01: the X1900 connector info row sits above the inferred header and
        # must still produce a semantic row.
        sheet = _FakeSheet(
            name="Connectors",
            rows=[
                ["X1900", "Connector", "12-pin", ""],
                ["Part", "Type", "Pins", "Notes"],
                ["A1", "Header", "8", "Main"],
            ],
        )

        semantic_rows = _sheet_semantic_rows(sheet)

        self.assertEqual(len(semantic_rows), 2)
        first = semantic_rows[0]
        self.assertEqual(first["row_index"], 1)
        self.assertEqual(first["inference_type"], "pre_header_row")
        self.assertEqual(first["raw_values"], {"Part": "X1900", "Type": "Connector", "Pins": "12-pin"})
        self.assertIn("row_above_inferred_header", first["confidence_reasons"])
        # The pre-header row must not seed forward-fill context.
        self.assertEqual(semantic_rows[1]["inherited"], {})

    def test_pre_header_banner_rows_are_filtered(self):
        # 横幅行(同一值重复所有列, 如 "Matrix"/"Summary")不是数据, 不应产出语义行。
        sheet = _FakeSheet(
            name="DFT",
            rows=[
                ["Banner", "Banner", "Banner"],
                ["Part", "Type", "Count"],
                ["A1", "Header", "8"],
            ],
        )

        semantic_rows = _sheet_semantic_rows(sheet)

        self.assertEqual(len(semantic_rows), 1)
        self.assertEqual(semantic_rows[0]["row_index"], 3)
        self.assertEqual(semantic_rows[0]["inference_type"], "forward_fill_context")


class _FakeSheet:
    def __init__(self, name: str, rows: list[list[str]]):
        self.name = name
        self.rows = rows
        self.row_indices = list(range(1, len(rows) + 1))
        self.merged_ranges: list[str] = []


if __name__ == "__main__":
    unittest.main()
