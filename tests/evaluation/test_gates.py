import unittest

from src.evaluation.gates import DEFAULT_THRESHOLDS, evaluate_gate
from src.evaluation.schemas import MetricResult, SampleResult


def _result(sample_id, score=None, *, critical=False, status="success"):
    return SampleResult(
        sample_id=sample_id,
        critical=critical,
        metrics=[
            MetricResult(
                sample_id=sample_id,
                metric_name="evidence_consistency",
                score=score,
                status=status,
            )
        ],
    )


def _ragas_and_policy_result(sample_id: str) -> SampleResult:
    return SampleResult(
        sample_id=sample_id,
        metrics=[
            MetricResult(sample_id=sample_id, metric_name="answer_relevancy", score=0.0),
            MetricResult(sample_id=sample_id, metric_name="evidence_consistency", score=1.0),
        ],
    )


class GateTests(unittest.TestCase):
    def test_gate_ignores_not_applicable(self):
        gate = evaluate_gate(
            [_result("ok", 0.8), _result("na", status="not_applicable")],
            {"evidence_consistency": 0.75},
            fail_on_threshold=True,
        )
        self.assertTrue(gate.passed)
        self.assertEqual(gate.metric_counts["evidence_consistency"], 1)

    def test_critical_sample_failure_fails_gate(self):
        gate = evaluate_gate(
            [_result("ok", 0.8), _result("critical", 0.5, critical=True)],
            {"evidence_consistency": 0.75},
            fail_on_threshold=True,
        )
        self.assertFalse(gate.passed)
        self.assertEqual(gate.exit_code, 2)
        self.assertIn("critical", " ".join(gate.failures))

    def test_threshold_failure_does_not_change_exit_code_without_flag(self):
        gate = evaluate_gate([_result("low", 0.4)], DEFAULT_THRESHOLDS, fail_on_threshold=False)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.exit_code, 0)

    def test_failed_metric_on_critical_sample_fails_gate(self):
        gate = evaluate_gate(
            [_result("critical", status="failed", critical=True)],
            {"evidence_consistency": 0.75},
            fail_on_threshold=True,
        )
        self.assertFalse(gate.passed)
        self.assertEqual(gate.exit_code, 3)
        self.assertEqual(gate.verdict, "insufficient_evaluation")
        self.assertIn("evaluation failed", " ".join(gate.failures))

    def test_non_retrieval_samples_do_not_contribute_ragas_metrics(self):
        result = _ragas_and_policy_result("direct")

        gate = evaluate_gate(
            [result],
            {"answer_relevancy": 0.7, "evidence_consistency": 0.75},
            sample_cohorts={"direct": "non_retrieval"},
        )

        self.assertNotIn("answer_relevancy", gate.metric_scores)
        self.assertEqual(gate.metric_scores["evidence_consistency"], 1.0)



    def test_unusable_scores_never_pass_release_gate(self):
        cases = [
            [_result("failed", status="failed")],
            [SampleResult(sample_id="missing")],
            [_result("bad", score=float("nan"))],
            [_result("bad", score=float("inf"))],
            [_result("bad", score=-0.1)],
            [_result("bad", score=1.1)],
            [],
        ]
        for results in cases:
            with self.subTest(results=results):
                gate = evaluate_gate(results, {"evidence_consistency": 0.75}, fail_on_threshold=True)
                self.assertFalse(gate.passed)
                self.assertEqual(gate.verdict, "insufficient_evaluation")
                self.assertEqual(gate.exit_code, 3)

    def test_low_coverage_uses_applicable_denominator(self):
        results = [_result(str(i), 0.9 if i == 0 else None, status="success" if i == 0 else "failed") for i in range(10)]
        gate = evaluate_gate(results, {"evidence_consistency": 0.75}, fail_on_threshold=True)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.metric_expected_counts["evidence_consistency"], 10)
        self.assertEqual(gate.metric_coverage["evidence_consistency"], 0.1)

    def test_declared_applicable_na_is_not_valid_coverage(self):
        gate = evaluate_gate(
            [_result("x", status="not_applicable")], {"evidence_consistency": 0.75},
            expected_metrics={"x": ["evidence_consistency"]}, fail_on_threshold=True,
        )
        self.assertEqual(gate.verdict, "insufficient_evaluation")

    def test_critical_missing_metric_cannot_hide_in_high_coverage(self):
        results = [_result(str(i), 0.9) for i in range(20)] + [SampleResult(sample_id="critical", critical=True)]
        gate = evaluate_gate(results, {"evidence_consistency": 0.75}, min_coverage=0.95, fail_on_threshold=True)
        self.assertEqual(gate.verdict, "insufficient_evaluation")

    def test_incomplete_report_cannot_pass(self):
        gate = evaluate_gate([_result("x", 0.9)], {"evidence_consistency": 0.75}, complete=False, fail_on_threshold=True)
        self.assertEqual(gate.verdict, "insufficient_evaluation")

    def test_duplicate_cells_cannot_inflate_coverage(self):
        result = _result("x", 0.9)
        result.metrics *= 3
        gate = evaluate_gate([result], {"evidence_consistency": 0.75}, fail_on_threshold=True)
        self.assertFalse(gate.passed)
        self.assertNotIn("evidence_consistency", gate.metric_counts)

    def test_required_metrics_without_threshold_still_need_coverage(self):
        gate = evaluate_gate(
            [SampleResult(sample_id="x")], {},
            expected_metrics={"x": ["faithfulness"]}, fail_on_threshold=True,
        )
        self.assertEqual(gate.verdict, "insufficient_evaluation")
        self.assertEqual(gate.metric_expected_counts, {"faithfulness": 1})

    def test_missing_applicability_contract_is_not_an_empty_cohort(self):
        gate = evaluate_gate(
            [_result("x", 0.9)], {"evidence_consistency": 0.75},
            expected_metrics={}, fail_on_threshold=True,
        )
        self.assertEqual(gate.verdict, "insufficient_evaluation")

    def test_denied_access_cannot_pass_with_a_permission_leak(self):
        gate = evaluate_gate(
            [SampleResult(sample_id="permission", access_check={"expected": "denied", "observed": "allowed"})],
            {}, fail_on_threshold=True,
        )
        self.assertEqual(gate.verdict, "fail")
        self.assertEqual(gate.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
