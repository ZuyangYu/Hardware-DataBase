"""v3 §12.2: per-KB spreadsheet index has a single cross-process writer.

The index is a plain SQLite file; API-side daemon parsers and dedicated
workers both mutate it.  The service-level exclusive lock must serialise
those mutations without blocking readers (SQLite WAL keeps their snapshot).
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

from src.pipelines.spreadsheet.pipeline import (
    SpreadsheetIndexRequest,
    SpreadsheetIndexResult,
)
from src.services.spreadsheet_index_service import SpreadsheetIndexService


class _FakePipeline:
    def __init__(self, tracker: "_Tracker") -> None:
        self._tracker = tracker

    def parse_and_index(self, request, progress_callback=None):
        self._tracker.enter()
        try:
            time.sleep(0.05)
            return SpreadsheetIndexResult(ok=True, status="indexed", message="ok")
        finally:
            self._tracker.exit()


class _Tracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.overlap = False

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            if self.active > 1:
                self.overlap = True

    def exit(self) -> None:
        with self._lock:
            self.active -= 1


class SpreadsheetIndexWriteLockTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = SpreadsheetIndexService(storage_root=self.tmp.name)
        self.tracker = _Tracker()

    def _request(self) -> SpreadsheetIndexRequest:
        return SpreadsheetIndexRequest(
            record_id=1,
            kb_name="kb",
            department_id="1",
            document_name="book.xlsx",
            source_group="物料数据",
            file_path="/tmp/book.xlsx",
            local_path="book.xlsx",
            content_hash="hash",
        )

    def test_concurrent_mutations_are_serialised(self):
        with patch.object(
            SpreadsheetIndexService,
            "_pipeline",
            lambda _self, _department, _kb: _FakePipeline(self.tracker),
        ):
            threads = [
                threading.Thread(target=self.service.parse_and_index, args=(self._request(),))
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertFalse(self.tracker.overlap)
        self.assertEqual(self.tracker.active, 0)

    def test_write_lock_file_lives_under_the_kb_index_dir(self):
        with patch.object(
            SpreadsheetIndexService,
            "_pipeline",
            lambda _self, _department, _kb: _FakePipeline(self.tracker),
        ):
            self.service.parse_and_index(self._request())
        kb_dir = self.service.kb_index_path("1", "kb")
        self.assertTrue(os.path.isdir(os.path.join(kb_dir, ".index-locks")))


if __name__ == "__main__":
    unittest.main()
