"""Validation helpers of the SQLite->PostgreSQL migration script stay sound."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.production_hardening.migrate_sqlite_to_postgres import (  # noqa: E402
    _coerce_for_target,
    _content_hash,
    _is_derived_table,
    _map_type,
    _normalize_value,
)


class MigrationValidationTests(unittest.TestCase):
    def test_content_hash_is_order_insensitive_multiset(self):
        columns = ["a", "b"]
        rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        shuffled = list(reversed(rows))
        assert _content_hash(rows, columns) == _content_hash(shuffled, columns)
        # Different content must differ.
        assert _content_hash(rows, columns) != _content_hash([{"a": 1, "b": "x"}], columns)

    def test_normalize_value_handles_bytes_and_bools(self):
        assert _normalize_value(True) == 1
        assert _normalize_value(b"abc") == "YWJj"
        assert _normalize_value(None) is None

    def test_coerce_for_target_decodes_blob_in_text_column(self):
        assert _coerce_for_target(b'{"k": 1}', "TEXT") == '{"k": 1}'
        assert _coerce_for_target(b"\x00\x01", "BYTEA") == b"\x00\x01"
        assert _coerce_for_target(True, "BIGINT") == 1

    def test_hash_matches_after_target_coercion(self):
        columns = ["value"]
        source = [{"value": b'{"k": 1}'}]
        target = [{"value": '{"k": 1}'}]
        source_rows = [
            {column: _coerce_for_target(row[column], "TEXT") for column in columns} for row in source
        ]
        target_rows = [
            {column: _coerce_for_target(row[column], "TEXT") for column in columns} for row in target
        ]
        assert _content_hash(source_rows, columns) == _content_hash(target_rows, columns)

    def test_derived_tables_are_skipped(self):
        assert _is_derived_table("attachment_parts_fts_data")
        assert _is_derived_table("attachment_parts_trigram_idx")
        assert not _is_derived_table("chat_attachment_parts")
        assert not _is_derived_table("chat_turns")

    def test_type_mapping(self):
        assert _map_type("INTEGER") == "BIGINT"
        assert _map_type("BOOLEAN") == "BIGINT"
        assert _map_type("REAL") == "DOUBLE PRECISION"
        assert _map_type("BLOB") == "BYTEA"
        assert _map_type("TEXT") == "TEXT"
        assert _map_type("") == "TEXT"


if __name__ == "__main__":
    unittest.main()
