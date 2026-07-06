import gc
import os
import sqlite3
import tempfile
import unittest

from src.pipelines.spreadsheet.table_store import TableIndexStore, _infer_headers
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


if __name__ == "__main__":
    unittest.main()
