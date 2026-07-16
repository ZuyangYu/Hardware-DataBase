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


if __name__ == "__main__":
    unittest.main()
