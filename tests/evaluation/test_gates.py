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
                metric_name="completeness",
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
            MetricResult(sample_id=sample_id, metric_name="completeness", score=1.0),
        ],
    )


class GateTests(unittest.TestCase):
    def test_gate_ignores_not_applicable(self):
        gate = evaluate_gate(
            [_result("ok", 0.8), _result("na", status="not_applicable")],
            {"completeness": 0.75},
            fail_on_threshold=True,
        )
        self.assertTrue(gate.passed)
        self.assertEqual(gate.metric_counts["completeness"], 1)

    def test_critical_sample_failure_fails_gate(self):
        gate = evaluate_gate(
            [_result("ok", 0.8), _result("critical", 0.5, critical=True)],
            {"completeness": 0.75},
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
            {"completeness": 0.75},
            fail_on_threshold=True,
        )
        self.assertFalse(gate.passed)
        self.assertEqual(gate.exit_code, 2)
        self.assertIn("evaluation failed", " ".join(gate.failures))

    def test_non_retrieval_samples_do_not_contribute_ragas_metrics(self):
        result = _ragas_and_policy_result("direct")

        gate = evaluate_gate(
            [result],
            {"answer_relevancy": 0.7, "completeness": 0.75},
            sample_cohorts={"direct": "non_retrieval"},
        )

        self.assertNotIn("answer_relevancy", gate.metric_scores)
        self.assertEqual(gate.metric_scores["completeness"], 1.0)


if __name__ == "__main__":
    unittest.main()
