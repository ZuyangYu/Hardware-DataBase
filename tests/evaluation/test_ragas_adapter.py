import unittest
from dataclasses import replace
from math import nan

from src.evaluation.config import EvaluationConfig
from src.evaluation.ragas_adapter import RAGAS_RESULT_KEYS, RagasAdapter, _NativeRagasBackend
from src.evaluation.schemas import AnswerSnapshot, EvaluationSample


class FakeBackend:
    def __init__(self):
        self.records = []
        self.metric_names = []

    def score(self, records, metric_names):
        self.records = records
        self.metric_names = metric_names
        return [{"answer_correctness": 0.8, "faithfulness": RuntimeError("judge failed")}]


class IsolatingBackend:
    def __init__(self):
        self.calls = []

    def score(self, records, metric_names):
        self.calls.append(metric_names)
        if metric_names == ["answer_correctness"]:
            raise RuntimeError("correctness failed")
        return [{"faithfulness": 0.7} for _ in records]


class NanBackend:
    def score(self, records, metric_names):
        return [{metric_names[0]: nan} for _ in records]


class RecoveringNanBackend:
    def __init__(self):
        self.calls = 0

    def score(self, records, metric_names):
        self.calls += 1
        value = nan if self.calls == 1 else 0.7
        return [{metric_names[0]: value} for _ in records]


def _config(**overrides):
    config = EvaluationConfig(
        llm_provider="custom",
        llm_base_url="https://judge.test/v1",
        llm_api_key="key",
        llm_model="judge",
        embedding_base_url="https://embed.test/v1",
        embedding_api_key="key",
        embedding_model="embed",
    )
    return replace(config, **overrides)


class RagasAdapterTests(unittest.TestCase):
    def test_ragas_record_prefers_scored_response_and_falls_back_to_raw_response(self):
        sample = EvaluationSample(id="q1", question="问题", reference_answer="参考答案", kb_name="ADAS")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="问题",
            kb_name="ADAS",
            response="正文。\n\n来源说明：证据 [1]。",
            scored_response="正文。",
            retrieved_contexts=["检索上下文"],
        )
        backend = FakeBackend()

        RagasAdapter(_config(), backend=backend).score([sample], [snapshot], ["answer_correctness"])

        self.assertEqual(backend.records[0]["response"], "正文。")

    def test_context_precision_uses_ragas_runtime_result_column(self):
        self.assertEqual(RAGAS_RESULT_KEYS["context_precision"], "llm_context_precision_with_reference")

    def test_maps_sample_and_snapshot_to_ragas_fields(self):
        sample = EvaluationSample(
            id="q1",
            question="问题",
            reference_answer="参考答案",
            reference_contexts=["参考上下文"],
            kb_name="ADAS",
        )
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="问题",
            kb_name="ADAS",
            response="实际答案",
            retrieved_contexts=["检索上下文"],
        )
        backend = FakeBackend()

        results = RagasAdapter(_config(), backend=backend).score(
            [sample], [snapshot], ["answer_correctness", "faithfulness"]
        )

        self.assertEqual(backend.records[0]["user_input"], "问题")
        self.assertEqual(backend.records[0]["response"], "实际答案")
        self.assertEqual(backend.records[0]["retrieved_contexts"], ["检索上下文"])
        self.assertEqual(backend.records[0]["reference"], "参考答案")
        self.assertEqual(results[0].score, 0.8)
        self.assertEqual(results[1].status, "failed")

    def test_marks_context_recall_not_applicable_without_reference_contexts(self):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(sample_id="q1", question="Q", kb_name="kb", response="A")
        backend = FakeBackend()

        results = RagasAdapter(_config(), backend=backend).score([sample], [snapshot], ["context_recall"])

        self.assertEqual(results[0].status, "not_applicable")
        self.assertEqual(backend.records, [])

    def test_failed_snapshot_does_not_call_backend(self):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1", question="Q", kb_name="kb", status="failed", error_stage="answer_collection"
        )
        backend = FakeBackend()

        results = RagasAdapter(_config(), backend=backend).score([sample], [snapshot], ["answer_correctness"])

        self.assertEqual(results[0].status, "failed")
        self.assertEqual(backend.records, [])

    def test_native_backend_uses_langchain_embedding_interface(self):
        embeddings = _NativeRagasBackend(_config())._build_embeddings()

        self.assertTrue(callable(embeddings.embed_query))
        self.assertTrue(callable(embeddings.embed_documents))

    def test_native_backend_passes_limit_and_uses_single_relevancy_sample(self):
        backend = _NativeRagasBackend(_config(llm_max_tokens=2048))
        llm = backend._build_llm()

        metrics = backend._build_metrics(["answer_relevancy"])

        self.assertEqual(llm.model_args["max_tokens"], 2048)
        self.assertEqual(metrics[0].strictness, 1)

    def test_native_backend_applies_runtime_limits_to_ragas(self):
        backend = _NativeRagasBackend(
            _config(timeout_seconds=23, max_workers=3, max_retries=4)
        )

        run_config = backend._build_run_config()
        metric = backend._build_metrics(["faithfulness"])[0]

        self.assertEqual(run_config.timeout, 23)
        self.assertEqual(run_config.max_workers, 3)
        self.assertEqual(run_config.max_retries, 4)
        self.assertEqual(metric.max_retries, 4)

    def test_prepares_bounded_scoring_snapshots_without_mutating_evidence(self):
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["aaaa", "bbbb", "cccc"],
        )

        prepared, diagnostics = RagasAdapter(
            _config(max_contexts_per_sample=2, max_context_chars=6)
        ).prepare_snapshots_for_scoring([snapshot])

        self.assertEqual(prepared[0].retrieved_contexts, ["aaaa", "bb"])
        self.assertEqual(snapshot.retrieved_contexts, ["aaaa", "bbbb", "cccc"])
        self.assertEqual(
            diagnostics["q1"],
            {
                "original_context_count": 3,
                "original_context_characters": 12,
                "scored_context_count": 2,
                "scored_context_characters": 6,
                "contexts_truncated": True,
                "context_selection": "original_order",
            },
        )

    def test_prepares_claim_supported_evidence_before_original_order(self):
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["noise", "supported fact"],
            evidence=[
                {"id": "noise", "content": "noise"},
                {"id": "support-1", "content": "supported fact"},
            ],
            retrieval_summary={
                "claim_coverage": [
                    {
                        "claim_id": "claim-1",
                        "status": "supported",
                        "evidence_ids": ["support-1"],
                    }
                ]
            },
        )

        prepared, diagnostics = RagasAdapter(
            _config(max_contexts_per_sample=2, max_context_chars=100)
        ).prepare_snapshots_for_scoring([snapshot])

        self.assertEqual(prepared[0].retrieved_contexts, ["supported fact", "noise"])
        self.assertEqual(diagnostics["q1"]["context_selection"], "claim_coverage")

    def test_isolates_metric_failures_to_the_metric_that_raised(self):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["source"],
        )
        backend = IsolatingBackend()

        results = RagasAdapter(_config(), backend=backend).score(
            [sample],
            [snapshot],
            ["answer_correctness", "faithfulness"],
        )

        self.assertEqual(backend.calls, [["answer_correctness"], ["faithfulness"]])
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(results[1].score, 0.7)

    def test_nan_metric_records_structured_evaluator_diagnostic(self):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["source"],
        )

        result = RagasAdapter(_config(), backend=NanBackend()).score(
            [sample], [snapshot], ["faithfulness"]
        )[0]

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.details["evaluator_diagnostic"]["kind"], "nan")

    def test_nan_metric_retries_as_a_single_record_and_recovers(self):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["source"],
        )
        backend = RecoveringNanBackend()

        result = RagasAdapter(_config(max_retries=1), backend=backend).score(
            [sample], [snapshot], ["faithfulness"]
        )[0]

        self.assertEqual(result.score, 0.7)
        self.assertEqual(backend.calls, 2)


if __name__ == "__main__":
    unittest.main()
