import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.evaluation.reporters import write_reports
from src.evaluation.schemas import EvaluationSummary, MetricResult, SampleResult


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.run_dir = Path(self.temp_dir.name) / "run"

    def test_writes_consistent_json_csv_and_escaped_html(self):
        summary = EvaluationSummary(
            run_id="run-1",
            sample_count=1,
            successful_samples=1,
            metric_scores={"faithfulness": 0.8},
            metric_counts={"faithfulness": 1},
        )
        results = [
            SampleResult(
                sample_id="q1",
                question="问题",
                reference_answer="参考答案",
                response="<script>alert(1)</script>",
                retrieved_contexts=["检索上下文"],
                metrics=[
                    MetricResult(
                        sample_id="q1",
                        metric_name="faithfulness",
                        score=0.8,
                    )
                ],
            )
        ]

        paths = write_reports(self.run_dir, summary, results)

        loaded = json.loads(paths.summary_json.read_text(encoding="utf-8"))
        self.assertEqual(loaded["metric_scores"]["faithfulness"], 0.8)
        with paths.summary_csv.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["faithfulness"], "0.8")
        html = paths.report_html.read_text(encoding="utf-8")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("参考答案", html)
        self.assertIn("检索上下文", html)

    def test_report_write_leaves_no_temp_files(self):
        summary = EvaluationSummary(run_id="run-1")
        write_reports(self.run_dir, summary, [])
        self.assertEqual(list(self.run_dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
