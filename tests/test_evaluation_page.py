import json
import tempfile
import unittest
from pathlib import Path

from src.ui.evaluation_page import can_access_evaluation, list_evaluation_runs, load_evaluation_summary


class EvaluationPageTests(unittest.TestCase):
    def test_only_system_admin_can_access(self):
        self.assertTrue(can_access_evaluation("system_admin"))
        self.assertFalse(can_access_evaluation("dept_admin"))
        self.assertFalse(can_access_evaluation("user"))
        self.assertFalse(can_access_evaluation(None))

    def test_load_summary_validates_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "summary.json"
            path.write_text(
                json.dumps({"run_id": "run-1", "sample_count": 2, "successful_samples": 2}),
                encoding="utf-8",
            )
            summary = load_evaluation_summary(path)
            self.assertEqual(summary.run_id, "run-1")
            self.assertEqual(summary.sample_count, 2)

    def test_list_runs_returns_newest_first_and_skips_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ["20260101T000000Z-a", "20260102T000000Z-b"]:
                run = root / name
                run.mkdir()
                (run / "summary.json").write_text(json.dumps({"run_id": name}), encoding="utf-8")
            (root / "incomplete").mkdir()

            runs = list_evaluation_runs(root)

            self.assertEqual([path.name for path in runs], ["20260102T000000Z-b", "20260101T000000Z-a"])


if __name__ == "__main__":
    unittest.main()
