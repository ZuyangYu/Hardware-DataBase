"""v3 §12.1: atomic publication keeps the previous file on failure."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from src.core.atomic_files import atomic_write_bytes, atomic_write_text


class AtomicFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "artifact.json")

    def test_writes_content_and_leaves_no_temporary_files(self):
        atomic_write_text(self.path, "hello")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")
        atomic_write_bytes(self.path, b"world")
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"world")
        self.assertEqual(
            [name for name in os.listdir(self.tmp.name) if name.startswith(".tmp-")], []
        )

    def test_nested_directory_is_created(self):
        nested = os.path.join(self.tmp.name, "a", "b", "file.txt")
        atomic_write_text(nested, "nested")
        with open(nested, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "nested")

    def test_failure_keeps_the_previous_version_and_cleans_up(self):
        atomic_write_text(self.path, "original")
        with mock.patch("src.core.atomic_files.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write_text(self.path, "replacement")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original")
        self.assertEqual(
            [name for name in os.listdir(self.tmp.name) if name.startswith(".tmp-")], []
        )


if __name__ == "__main__":
    unittest.main()
