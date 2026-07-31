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
                scored_response="正文回答",
                retrieved_contexts=["检索上下文"],
                metadata={
                    "evaluation_cohort": "retrieval",
                    "ragas_scoring": {
                        "original_context_count": 3,
                        "original_context_characters": 120,
                        "scored_context_count": 2,
                        "scored_context_characters": 80,
                        "contexts_truncated": True,
                        "selected_claim_ids": ["c1"],
                    }
                },
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
        result_json = json.loads(paths.results_jsonl.read_text(encoding="utf-8"))
        self.assertEqual(result_json["metadata"]["ragas_scoring"]["scored_context_count"], 2)
        self.assertEqual(result_json["metadata"]["evaluation_cohort"], "retrieval")
        self.assertEqual(result_json["metadata"]["ragas_scoring"]["selected_claim_ids"], ["c1"])
        self.assertEqual(result_json["scored_response"], "正文回答")
        with paths.summary_csv.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["faithfulness"], "0.8")
        self.assertIn("ragas_scoring", rows[0])
        self.assertEqual(rows[0]["scored_response"], "正文回答")
        self.assertIn('"scored_context_count": 2', rows[0]["ragas_scoring"])
        html = paths.report_html.read_text(encoding="utf-8")
        self.assertIn("评分上下文", html)
        self.assertIn("2/3", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("参考答案", html)
        self.assertIn("评分正文", html)
        self.assertIn("正文回答", html)
        self.assertIn("检索上下文", html)

    def test_report_write_leaves_no_temp_files(self):
        summary = EvaluationSummary(run_id="run-1")
        write_reports(self.run_dir, summary, [])
        self.assertEqual(list(self.run_dir.glob("*.tmp")), [])
        self.assertTrue((self.run_dir / "report_complete.json").is_file())

    def test_report_writer_adds_run_outcome_metadata(self):
        summary = EvaluationSummary(run_id="run-1")

        paths = write_reports(
            self.run_dir,
            summary,
            [],
            metadata={
                "run_outcome": {
                    "kind": "partial_cancelled",
                    "completed_groups": 1,
                    "total_groups": 5,
                }
            },
        )

        loaded = json.loads(paths.summary_json.read_text(encoding="utf-8"))
        self.assertEqual(loaded["metadata"]["run_outcome"]["kind"], "partial_cancelled")
        self.assertIn("partial_cancelled", paths.report_html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
