import unittest

from src.evaluation.schemas import AnswerSnapshot, EvaluationSample, MetricResult
from src.evaluation.service import EvaluationService


class FakeAdapter:
    def score(self, samples, snapshots, metric_names):
        return [
            MetricResult(sample_id="q1", metric_name="faithfulness", score=0.9),
            MetricResult(
                sample_id="q1",
                metric_name="answer_correctness",
                status="failed",
                reason="judge failed",
            ),
        ]


def _sample():
    return EvaluationSample(
        id="q1",
        question="Q",
        reference_answer="A",
        kb_name="ADAS",
        rubric={"required_facts": ["U1700"]},
    )


def _snapshot():
    return AnswerSnapshot(
        sample_id="q1",
        question="Q",
        kb_name="ADAS",
        response="U1700",
        retrieved_contexts=["context"],
    )


class EvaluationServiceTests(unittest.TestCase):
    def test_score_keeps_successful_metrics_when_one_fails(self):
        service = EvaluationService(ragas_adapter=FakeAdapter())

        summary, results = service.score(
            [_sample()],
            [_snapshot()],
            metric_names=["faithfulness", "answer_correctness"],
        )

        self.assertEqual(summary.metric_scores["faithfulness"], 0.9)
        self.assertEqual(summary.metric_failures["answer_correctness"], 1)
        self.assertIn("completeness", summary.metric_scores)
        self.assertEqual(results[0].sample_id, "q1")
        self.assertEqual(results[0].question, "Q")
        self.assertEqual(results[0].reference_answer, "A")
        self.assertEqual(results[0].response, "U1700")
        self.assertEqual(results[0].retrieved_contexts, ["context"])

    def test_score_marks_failed_snapshot_as_failed_sample(self):
        snapshot = _snapshot().model_copy(update={"status": "failed"})
        service = EvaluationService(ragas_adapter=FakeAdapter())

        summary, _ = service.score([_sample()], [snapshot], metric_names=[])

        self.assertEqual(summary.failed_samples, 1)
        self.assertEqual(summary.successful_samples, 0)


if __name__ == "__main__":
    unittest.main()
