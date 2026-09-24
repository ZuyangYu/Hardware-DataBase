import unittest

from src.evaluation.presentation import (
    build_baseline_chart_rows,
    build_comparison,
    build_credibility_summary,
    build_current_metric_chart_rows,
    build_sample_rows,
    classify_sample_result,
    metric_display_label,
)
from src.evaluation.schemas import EvaluationSummary, MetricResult, SampleResult


class EvaluationPresentationTests(unittest.TestCase):
    def test_classifies_collection_failure_before_all_other_states(self):
        result = SampleResult(
            sample_id="q1",
            snapshot_status="failed",
            retrieved_contexts=["evidence"],
            metrics=[MetricResult(sample_id="q1", metric_name="faithfulness", score=0.9)],
        )

        self.assertEqual(classify_sample_result(result), "采集失败")

    def test_classifies_metric_failure(self):
        result = SampleResult(
            sample_id="q1",
            metrics=[
                MetricResult(
                    sample_id="q1",
                    metric_name="faithfulness",
                    status="failed",
                    reason="judge unavailable",
                )
            ],
        )

        self.assertEqual(classify_sample_result(result), "评分失败")

    def test_classifies_critical_sample_without_evidence_as_review_needed(self):
        result = SampleResult(sample_id="q1", critical=True)

        self.assertEqual(classify_sample_result(result), "关键样本待复核")

    def test_credibility_summary_exposes_evidence_and_score_coverage(self):
        summary = EvaluationSummary(
            run_id="run-1",
            sample_count=3,
            successful_samples=2,
            failed_samples=1,
            metric_scores={"faithfulness": 0.8},
        )
        results = [
            SampleResult(
                sample_id="q1",
                retrieved_contexts=["context"],
                metrics=[MetricResult(sample_id="q1", metric_name="faithfulness", score=0.9)],
            ),
            SampleResult(
                sample_id="q2",
                metrics=[MetricResult(sample_id="q2", metric_name="faithfulness", score=0.7)],
            ),
            SampleResult(sample_id="q3", snapshot_status="failed"),
        ]

        report = build_credibility_summary(summary, results)

        self.assertEqual(report["status"], "存在技术失败")
        self.assertEqual(report["evidence_samples"], 1)
        self.assertEqual(report["scored_samples"], 2)
        self.assertEqual(report["collection_failures"], 1)

    def test_comparison_only_includes_shared_metrics(self):
        current = EvaluationSummary(
            run_id="current",
            metric_scores={"faithfulness": 0.8, "answer_relevancy": 0.7},
        )
        baseline = EvaluationSummary(
            run_id="baseline",
            metric_scores={"faithfulness": 0.6, "context_precision": 0.9},
        )

        self.assertEqual(
            build_comparison(current, baseline),
            [{"指标": "faithfulness", "当前": 0.8, "基线": 0.6, "变化": 0.2}],
        )

    def test_current_metric_chart_rows_keep_coverage_and_failure_counts(self):
        summary = EvaluationSummary(
            run_id="run-1",
            metric_scores={"faithfulness": 0.7, "answer_relevancy": 0.5},
            metric_counts={"faithfulness": 4},
            metric_failures={"faithfulness": 1, "answer_relevancy": 3},
        )

        self.assertEqual(
            build_current_metric_chart_rows(summary),
            [
                {
                    "metric": "faithfulness",
                    "metric_label": "忠实性 (faithfulness)",
                    "score": 0.7,
                    "threshold": 0.75,
                    "meets_threshold": False,
                    "applicable_samples": 4,
                    "scoring_failures": 1,
                },
                {
                    "metric": "answer_relevancy",
                    "metric_label": "答案相关性 (answer_relevancy)",
                    "score": 0.5,
                    "threshold": 0.7,
                    "meets_threshold": False,
                    "applicable_samples": 0,
                    "scoring_failures": 3,
                },
            ],
        )

    def test_current_metric_chart_rows_keep_configured_order_and_unknown_metrics(self):
        summary = EvaluationSummary(
            run_id="run-1",
            metric_scores={
                "future_metric": 0.2,
                "answer_relevancy": 0.7,
                "faithfulness": 0.8,
            },
        )

        rows = build_current_metric_chart_rows(summary)

        self.assertEqual(
            [row["metric"] for row in rows],
            ["faithfulness", "answer_relevancy", "future_metric"],
        )
        self.assertEqual(rows[0]["threshold"], 0.75)
        self.assertTrue(rows[0]["meets_threshold"])
        self.assertEqual(rows[1]["threshold"], 0.7)
        self.assertTrue(rows[1]["meets_threshold"])
        self.assertIsNone(rows[2]["threshold"])
        self.assertFalse(rows[2]["meets_threshold"])

    def test_baseline_chart_rows_only_include_shared_metrics(self):
        current = EvaluationSummary(run_id="current", metric_scores={"faithfulness": 0.8})
        baseline = EvaluationSummary(
            run_id="baseline",
            metric_scores={"faithfulness": 0.6, "context_recall": 0.7},
        )

        self.assertEqual(
            build_baseline_chart_rows(current, baseline),
            [
                {
                    "metric": "faithfulness",
                    "metric_label": "忠实性 (faithfulness)",
                    "current": 0.8,
                    "baseline": 0.6,
                    "change": 0.2,
                }
            ],
        )

    def test_baseline_chart_rows_keep_configured_order_and_delta(self):
        current = EvaluationSummary(
            run_id="current",
            metric_scores={"answer_relevancy": 0.8, "faithfulness": 0.7},
        )
        baseline = EvaluationSummary(
            run_id="baseline",
            metric_scores={"answer_relevancy": 0.6, "faithfulness": 0.8},
        )

        rows = build_baseline_chart_rows(current, baseline)

        self.assertEqual([row["metric"] for row in rows], ["faithfulness", "answer_relevancy"])
        self.assertEqual(rows[0]["change"], -0.1)
        self.assertEqual(rows[1]["change"], 0.2)

    def test_metric_display_label_uses_chinese_name_and_preserves_unknown_codes(self):
        self.assertEqual(metric_display_label("faithfulness"), "忠实性 (faithfulness)")
        self.assertEqual(metric_display_label("future_metric"), "future_metric")

    def test_sample_rows_include_filterable_state_and_metric_failure_reason(self):
        result = SampleResult(
            sample_id="q1",
            critical=True,
            retrieved_contexts=["context"],
            metrics=[
                MetricResult(
                    sample_id="q1",
                    metric_name="faithfulness",
                    status="failed",
                    reason="judge unavailable",
                )
            ],
        )

        self.assertEqual(
            build_sample_rows([result]),
            [
                {
                    "样本": "q1",
                    "状态": "评分失败",
                    "关键样本": "是",
                    "证据数": 1,
                    "已评分指标": 0,
                    "失败说明": "faithfulness: judge unavailable",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
