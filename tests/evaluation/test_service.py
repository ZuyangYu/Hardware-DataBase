import tempfile
import unittest
from pathlib import Path

from src.evaluation.config import EvaluationConfig
from src.evaluation.ragas_adapter import RagasAdapter
from src.evaluation.schemas import AnswerSnapshot, EvaluationSample, MetricResult
from src.evaluation.service import EvaluationService
from src.evaluation.snapshot_store import SnapshotStore


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


class CapturingBackend:
    def __init__(self):
        self.records = []

    def score(self, records, metric_names):
        self.records.append(records)
        return [{metric_name: 0.5 for metric_name in metric_names} for _ in records]


def _config(**overrides):
    values = {
        "llm_provider": "custom",
        "llm_base_url": "https://judge.test/v1",
        "llm_api_key": "key",
        "llm_model": "judge",
        "embedding_base_url": "https://embed.test/v1",
        "embedding_api_key": "key",
        "embedding_model": "embed",
        "max_contexts_per_sample": 2,
        "max_context_chars": 6,
    }
    values.update(overrides)
    return EvaluationConfig(**values)


def _sample(sample_id: str = "q1"):
    return EvaluationSample(
        id=sample_id,
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


class FakePipeline:
    def query(self, *args, **kwargs):
        yield "A"

    def get_last_retrieval_summary(self):
        return {"evidence": []}


class EvaluationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.snapshot_path = Path(self.temp_dir.name) / "snapshot.jsonl"

    def test_collect_reports_each_persisted_snapshot(self):
        progress = []
        service = EvaluationService(pipeline_factory=lambda: FakePipeline())

        def after_sample(snapshot, done, total):
            persisted_ids = [item.sample_id for item in SnapshotStore(self.snapshot_path).load_all()]
            self.assertIn(snapshot.sample_id, persisted_ids)
            progress.append((snapshot.sample_id, done, total))

        snapshots = service.collect(
            [_sample("q1"), _sample("q2")],
            self.snapshot_path,
            after_sample=after_sample,
        )

        self.assertEqual([item.sample_id for item in snapshots], ["q1", "q2"])
        self.assertEqual(progress, [("q1", 1, 2), ("q2", 2, 2)])

    def test_collect_preserves_completed_snapshot_when_callback_stops(self):
        service = EvaluationService(pipeline_factory=lambda: FakePipeline())

        snapshots = service.collect(
            [_sample("q1"), _sample("q2")],
            self.snapshot_path,
            before_sample=lambda sample, _done, _total: sample.id != "q2",
        )

        self.assertEqual([item.sample_id for item in snapshots], ["q1"])

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

    def test_score_copies_snapshot_retrieval_summary_to_sample_metadata(self):
        snapshot = _snapshot().model_copy(
            update={"retrieval_summary": {"final_top_k": 2, "claim_coverage": []}}
        )
        service = EvaluationService(ragas_adapter=FakeAdapter())

        _, results = service.score([_sample()], [snapshot], metric_names=[])

        self.assertEqual(
            results[0].metadata["retrieval_summary"],
            {"final_top_k": 2, "claim_coverage": []},
        )

    def test_score_retains_raw_response_and_exposes_scored_response(self):
        snapshot = _snapshot().model_copy(
            update={
                "response": "正文。\n\n来源说明：证据 [1]。",
                "scored_response": "正文。",
                "metadata": {"scored_response_filter": {"filtered": True}},
            }
        )
        service = EvaluationService(ragas_adapter=FakeAdapter())

        _, results = service.score([_sample()], [snapshot], metric_names=[])

        self.assertEqual(results[0].response, "正文。\n\n来源说明：证据 [1]。")
        self.assertEqual(results[0].scored_response, "正文。")
        self.assertEqual(results[0].metadata["scored_response_filter"], {"filtered": True})

    def test_score_marks_failed_snapshot_as_failed_sample(self):
        snapshot = _snapshot().model_copy(update={"status": "failed"})
        service = EvaluationService(ragas_adapter=FakeAdapter())

        summary, _ = service.score([_sample()], [snapshot], metric_names=[])

        self.assertEqual(summary.failed_samples, 1)
        self.assertEqual(summary.successful_samples, 0)

    def test_score_preserves_original_contexts_and_records_scoring_budget(self):
        backend = CapturingBackend()
        service = EvaluationService(ragas_adapter=RagasAdapter(_config(), backend=backend))
        snapshot = _snapshot().model_copy(
            update={"retrieved_contexts": ["aaaa", "bbbb", "cccc"]}
        )

        _, results = service.score([_sample()], [snapshot], metric_names=["faithfulness"])

        self.assertEqual(results[0].retrieved_contexts, ["aaaa", "bbbb", "cccc"])
        self.assertEqual(
            results[0].metadata["ragas_scoring"],
            {
                "original_context_count": 3,
                "original_context_characters": 12,
                "scored_context_count": 2,
                "scored_context_characters": 6,
                "contexts_truncated": True,
                "context_selection": "original_order",
            },
        )
        self.assertEqual(backend.records[0][0]["retrieved_contexts"], ["aaaa", "bb"])


if __name__ == "__main__":
    unittest.main()
