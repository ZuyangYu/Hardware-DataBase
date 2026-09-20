"""P3-001: the deterministic reviewer evaluation harness stays green."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.production_hardening.reviewer_eval import load_cases, main, score_cases

FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "fixtures" / "reviewer_cases.jsonl"


class ReviewerEvalHarnessTests(unittest.TestCase):
    def test_fixture_cases_are_well_formed(self):
        cases = load_cases(FIXTURE)
        self.assertGreaterEqual(len(cases), 10)
        for case in cases:
            self.assertIn("case_id", case)
            self.assertIn("expected_status", case)
            self.assertIn("expected_codes", case)

    def test_harness_reports_full_recall_on_shipped_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "reviewer.json")
            self.assertEqual(main(["--fixture", str(FIXTURE), "--output", output]), 0)
            report = json.loads(Path(output).read_text(encoding="utf-8"))
        self.assertEqual(report["deterministic"]["status_accuracy"], 1.0)
        for metrics in report["deterministic"]["per_code"].values():
            self.assertEqual(metrics["precision"], 1.0)
            self.assertEqual(metrics["recall"], 1.0)
        self.assertFalse(report["semantic_pass"]["evaluated"])

    def test_scoring_counts_false_positives_and_negatives(self):
        cases = [
            {"case_id": "a", "expected_codes": ["x"], "expected_status": "fail"},
            {"case_id": "b", "expected_codes": [], "expected_status": "pass"},
        ]
        results = {
            "a": {"status": "pass", "issues": []},
            "b": {"status": "fail", "issues": [{"code": "x"}]},
        }
        scored = score_cases(cases, results)
        self.assertEqual(scored["status_accuracy"], 0.0)
        self.assertEqual(scored["per_code"]["x"], {"tp": 0, "fp": 1, "fn": 1, "precision": 0.0, "recall": 0.0})


if __name__ == "__main__":
    unittest.main()
