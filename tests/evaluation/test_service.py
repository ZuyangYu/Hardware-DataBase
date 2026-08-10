import tempfile
import threading
import time
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


class GroupedAdapter:
    def __init__(self):
        self.metric_batches = []

    def score(self, samples, snapshots, metric_names):
        self.metric_batches.append(tuple(metric_names))
        return [
            MetricResult(sample_id="q1", metric_name=metric_name, score=0.8)
            for metric_name in metric_names
        ]


class IncrementalAdapter:
    def score(self, samples, snapshots, metric_names, *, on_result=None):
        result = MetricResult(sample_id=samples[0].id, metric_name=metric_names[0], score=0.8)
        if on_result is not None:
            on_result(result)
        return [result]


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

    def test_collect_uses_configured_workers_without_callbacks(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def collect(sample):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return AnswerSnapshot(
                sample_id=sample.id,
                question=sample.question,
                kb_name=sample.kb_name,
                response="A",
            )

        service = EvaluationService(config=_config(max_workers=4))
        service.answer_runner.collect = collect
        samples = [_sample(f"q{index}") for index in range(8)]

        snapshots = service.collect(samples, self.snapshot_path)

        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, 4)
        self.assertEqual(
            {item.sample_id for item in snapshots},
            {item.id for item in samples},
        )

    def test_collect_uses_configured_workers_with_progress_callbacks(self):
        active = 0
        max_active = 0
        lock = threading.Lock()
        persisted_ids = []

        def collect(sample):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return AnswerSnapshot(
                sample_id=sample.id,
                question=sample.question,
                kb_name=sample.kb_name,
                response="A",
            )

        service = EvaluationService(config=_config(max_workers=2))
        service.answer_runner.collect = collect
        samples = [_sample(f"q{index}") for index in range(4)]

        snapshots = service.collect(
            samples,
            self.snapshot_path,
            before_sample=lambda _sample, _done, _total: True,
            after_sample=lambda snapshot, _done, _total: persisted_ids.append(
                snapshot.sample_id
            ),
        )

        self.assertEqual(max_active, 2)
        self.assertEqual(set(persisted_ids), {item.id for item in samples})
        self.assertEqual({item.sample_id for item in snapshots}, {item.id for item in samples})

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

    def test_score_invokes_progress_callback_after_each_ragas_metric_group(self):
        adapter = GroupedAdapter()
        progress = []
        service = EvaluationService(ragas_adapter=adapter)

        summary, results = service.score(
            [_sample()],
            [_snapshot()],
            metric_names=["faithfulness", "answer_correctness"],
            progress_callback=lambda current_summary, current_results, completed, total: (
                progress.append(
                    (completed, total, len(current_results[0].metrics), current_summary.metric_scores)
                )
                or True
            ),
        )

        self.assertEqual(adapter.metric_batches, [("faithfulness",), ("answer_correctness",)])
        self.assertEqual([(item[0], item[1]) for item in progress], [(1, 2), (2, 2)])
        self.assertEqual(
            {metric.metric_name for metric in results[0].metrics},
            {
                "completeness",
                "evidence_consistency",
                "missing_information_honesty",
                "conflict_disclosure",
                "faithfulness",
                "answer_correctness",
            },
        )
        self.assertEqual(summary.metric_scores["faithfulness"], 0.8)

    def test_score_invokes_item_progress_callback_for_each_metric_result(self):
        progress = []
        service = EvaluationService(ragas_adapter=IncrementalAdapter())

        summary, results = service.score(
            [_sample()],
            [_snapshot()],
            metric_names=["faithfulness"],
            item_progress_callback=lambda current_summary, current_results, completed, total: progress.append(
                (completed, total, len(current_results[0].metrics), current_summary.scoring_completed_items)
            ),
        )

        self.assertEqual(progress, [(1, 1, 5, 1)])
        self.assertEqual(summary.scoring_completed_items, 1)
        self.assertEqual(summary.scoring_total_items, 1)
        self.assertEqual(results[0].metrics[-1].metric_name, "faithfulness")

    def test_score_stops_after_progress_callback_returns_false(self):
        adapter = GroupedAdapter()
        service = EvaluationService(ragas_adapter=adapter)

        summary, results = service.score(
            [_sample()],
            [_snapshot()],
            metric_names=["faithfulness", "answer_correctness"],
            progress_callback=lambda _summary, _results, completed, _total: completed < 1,
        )

        self.assertEqual(adapter.metric_batches, [("faithfulness",)])
        self.assertEqual(
            [metric.metric_name for metric in results[0].metrics],
            [
                "completeness",
                "evidence_consistency",
                "missing_information_honesty",
                "conflict_disclosure",
                "faithfulness",
            ],
        )
        self.assertEqual(summary.metric_scores["faithfulness"], 0.8)

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
        self.assertEqual(results[0].metadata["evaluation_cohort"], "retrieval")

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

    def test_score_skips_ragas_when_every_snapshot_failed(self):
        adapter = GroupedAdapter()
        snapshot = _snapshot().model_copy(update={"status": "failed"})

        summary, results = EvaluationService(ragas_adapter=adapter).score(
            [_sample()], [snapshot], metric_names=["answer_correctness"]
        )

        self.assertEqual(adapter.metric_batches, [])
        self.assertEqual(summary.metric_failures, {})
        self.assertEqual(results[0].metrics, [])
        self.assertEqual(summary.metadata["scoring_skipped"], "no_successful_snapshots")

    def test_non_retrieval_sample_skips_ragas_backend_and_marks_metrics_not_applicable(self):
        backend = CapturingBackend()
        service = EvaluationService(ragas_adapter=RagasAdapter(_config(), backend=backend))
        sample = _sample().model_copy(update={"tags": ["direct", "small-talk"]})

        summary, results = service.score(
            [sample],
            [_snapshot().model_copy(update={"retrieved_contexts": []})],
            metric_names=["answer_correctness", "answer_relevancy"],
        )

        self.assertEqual(backend.records, [])
        ragas_metrics = [
            metric for metric in results[0].metrics if metric.metric_name.startswith("answer_")
        ]
        self.assertTrue(all(metric.status == "not_applicable" for metric in ragas_metrics))
        self.assertNotIn("answer_correctness", summary.metric_scores)

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
                "selected_evidence_ids": [],
                "selected_claim_ids": [],
                "excluded_evidence_ids": [],
            },
        )
        self.assertEqual(backend.records[0][0]["retrieved_contexts"], ["aaaa", "bb"])


if __name__ == "__main__":
    unittest.main()
