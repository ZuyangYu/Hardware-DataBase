import unittest
from dataclasses import replace
from math import nan
from unittest.mock import patch

from src.evaluation.config import EvaluationConfig
from src.evaluation.ragas_adapter import RAGAS_RESULT_KEYS, RagasAdapter, _NativeRagasBackend
from src.evaluation.schemas import AnswerSnapshot, EvaluationSample


def _has_native_ragas_deps() -> bool:
    """Whether the optional ragas/openai/langchain_openai stack is importable.

    Only the three ``_NativeRagasBackend`` tests need the real stack; the rest
    of the suite uses in-memory fake backends. Missing optional deps skip those
    three rather than erroring.
    """
    try:
        import langchain_openai  # noqa: F401
        import openai  # noqa: F401
        import ragas  # noqa: F401
        return True
    except ImportError:
        return False


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


class CapturingBackend:
    def __init__(self):
        self.calls = []

    def score(self, records, metric_names):
        self.calls.append((metric_names, records))
        return [{metric_names[0]: 0.7} for _ in records]


class ContextBudgetBackend:
    def __init__(self):
        self.contexts = []

    def score(self, records, metric_names):
        contexts = records[0].get("retrieved_contexts", [])
        self.contexts.append(contexts)
        if sum(map(len, contexts)) > 3:
            raise TimeoutError("judge timed out")
        return [{metric_names[0]: 0.9}]


class NanThenContextBudgetBackend:
    def __init__(self):
        self.calls = 0
        self.contexts = []

    def score(self, records, metric_names):
        self.calls += 1
        contexts = records[0].get("retrieved_contexts", [])
        self.contexts.append(contexts)
        if self.calls == 1:
            return [{metric_names[0]: nan}]
        if sum(map(len, contexts)) > 3:
            raise TimeoutError("judge timed out")
        return [{metric_names[0]: 0.9}]


class AlwaysFailingContextBackend:
    def score(self, records, metric_names):
        raise TimeoutError("judge timed out")


class RecoveringTimeoutBackend:
    def __init__(self):
        self.calls = 0

    def score(self, records, metric_names):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("judge timed out")
        return [{metric_names[0]: 0.8}]


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

    def test_sends_only_ragas_required_fields_for_each_metric(self):
        sample = EvaluationSample(
            id="q1",
            question="Q",
            reference_answer="reference",
            reference_contexts=["gold context"],
            kb_name="kb",
        )
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="response",
            retrieved_contexts=["context"],
        )
        backend = CapturingBackend()

        RagasAdapter(_config(), backend=backend).score(
            [sample],
            [snapshot],
            [
                "answer_correctness",
                "answer_relevancy",
                "faithfulness",
                "context_precision",
                "context_recall",
            ],
        )

        records_by_metric = {metric_names[0]: records[0] for metric_names, records in backend.calls}
        self.assertEqual(
            set(records_by_metric["answer_correctness"]),
            {"sample_id", "user_input", "response", "reference"},
        )
        self.assertEqual(
            set(records_by_metric["answer_relevancy"]),
            {"sample_id", "user_input", "response"},
        )
        self.assertEqual(
            set(records_by_metric["faithfulness"]),
            {"sample_id", "user_input", "response", "retrieved_contexts"},
        )
        self.assertEqual(
            set(records_by_metric["context_precision"]),
            {"sample_id", "user_input", "reference", "retrieved_contexts"},
        )
        self.assertEqual(
            set(records_by_metric["context_recall"]),
            {"sample_id", "user_input", "reference", "retrieved_contexts"},
        )

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

        results = RagasAdapter(_config(), backend=backend).score([sample], [snapshot], ["answer_correctness"])

        self.assertEqual(backend.records[0]["user_input"], "问题")
        self.assertEqual(backend.records[0]["response"], "实际答案")
        self.assertEqual(backend.records[0]["reference"], "参考答案")
        self.assertEqual(results[0].score, 0.8)

    def test_emits_each_metric_result_to_incremental_callback(self):
        samples = [
            EvaluationSample(id="q1", question="Q1", reference_answer="A1", kb_name="kb"),
            EvaluationSample(id="q2", question="Q2", reference_answer="A2", kb_name="kb"),
        ]
        snapshots = [
            AnswerSnapshot(
                sample_id="q1", question="Q1", kb_name="kb", response="A1", retrieved_contexts=["c1"]
            ),
            AnswerSnapshot(
                sample_id="q2", question="Q2", kb_name="kb", response="A2", retrieved_contexts=["c2"]
            ),
        ]
        emitted = []

        backend = CapturingBackend()
        RagasAdapter(_config(), backend=backend).score(
            samples,
            snapshots,
            ["faithfulness"],
            on_result=emitted.append,
        )

        self.assertEqual([(item.sample_id, item.metric_name) for item in emitted], [("q1", "faithfulness"), ("q2", "faithfulness")])
        self.assertTrue(all(item.status == "success" for item in emitted))
        self.assertEqual([len(records) for _metrics, records in backend.calls], [1, 1])

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

    @unittest.skipUnless(_has_native_ragas_deps(), "requires ragas/openai/langchain_openai")
    def test_native_backend_uses_langchain_embedding_interface(self):
        embeddings = _NativeRagasBackend(_config())._build_embeddings()

        self.assertTrue(callable(embeddings.embed_query))
        self.assertTrue(callable(embeddings.embed_documents))
        self.assertFalse(embeddings.check_embedding_ctx_length)

    @unittest.skipUnless(_has_native_ragas_deps(), "requires ragas/openai/langchain_openai")
    def test_native_backend_passes_configured_embedding_dimensions(self):
        config = replace(_config(), embedding_dims=2048)

        embeddings = _NativeRagasBackend(config)._build_embeddings()

        self.assertEqual(embeddings.dimensions, 2048)

    @unittest.skipUnless(_has_native_ragas_deps(), "requires ragas/openai/langchain_openai")
    def test_native_backend_passes_limit_and_uses_ragas_default_relevancy_samples(self):
        backend = _NativeRagasBackend(_config(llm_max_tokens=2048))
        llm = backend._build_llm()

        metrics = backend._build_metrics(["answer_relevancy"])

        self.assertEqual(llm.model_args["max_tokens"], 2048)
        self.assertEqual(metrics[0].strictness, 3)

    @unittest.skipUnless(_has_native_ragas_deps(), "requires ragas/openai/langchain_openai")
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
                "selected_evidence_ids": [],
                "selected_claim_ids": [],
                "excluded_evidence_ids": [],
                "scored_contexts": ["aaaa", "bb"],
            },
        )

    def test_prepared_contexts_deduplicate_and_apply_per_item_limit(self):
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["aaaaaa", "aaaaaa", "bbbbbb"],
        )

        prepared, diagnostics = RagasAdapter(
            _config(
                max_contexts_per_sample=4,
                max_context_chars=10,
                max_context_chars_per_item=4,
            )
        ).prepare_snapshots_for_scoring([snapshot])

        self.assertEqual(prepared[0].retrieved_contexts, ["aaaa", "bbbb"])
        self.assertEqual(diagnostics["q1"]["original_context_count"], 3)
        self.assertEqual(diagnostics["q1"]["scored_context_count"], 2)
        self.assertEqual(diagnostics["q1"]["scored_context_characters"], 8)

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

    def test_prepares_highest_quality_evidence_for_each_claim_before_remaining_contexts(self):
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["noise", "context e1", "context e2", "context e3"],
            evidence=[
                {"id": "e1", "content": "context e1"},
                {"id": "e2", "content": "context e2"},
                {"id": "e3", "content": "context e3"},
            ],
            retrieval_summary={
                "claim_coverage": [
                    {"claim_id": "c1", "status": "supported", "evidence_ids": ["e1", "e2"]},
                    {"claim_id": "c2", "status": "supported", "evidence_ids": ["e3"]},
                ],
                "evidence_quality": [
                    {"evidence_id": "e1", "score": 0.2},
                    {"evidence_id": "e2", "score": 0.9},
                    {"evidence_id": "e3", "score": 0.7},
                ],
            },
        )

        prepared, diagnostics = RagasAdapter(
            _config(max_contexts_per_sample=4, max_context_chars=100)
        ).prepare_snapshots_for_scoring([snapshot])

        self.assertEqual(prepared[0].retrieved_contexts[:2], ["context e2", "context e3"])
        self.assertEqual(diagnostics["q1"]["selected_claim_ids"], ["c1", "c2"])
        self.assertEqual(diagnostics["q1"]["selected_evidence_ids"], ["e2", "e3"])

    def test_prioritizes_question_relevant_evidence_over_generic_high_quality_evidence(self):
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="TPS62872 最大输出电流是多少？",
            kb_name="kb",
            response="12 A",
            retrieved_contexts=["资料源目录与模板说明", "TPS62872 最大输出电流为 12 A"],
            evidence=[
                {"id": "catalog", "content": "资料源目录与模板说明"},
                {"id": "fact", "content": "TPS62872 最大输出电流为 12 A"},
            ],
            retrieval_summary={
                "evidence_quality": [
                    {"evidence_id": "catalog", "score": 0.99},
                    {"evidence_id": "fact", "score": 0.70},
                ]
            },
        )

        prepared, diagnostics = RagasAdapter(
            _config(max_contexts_per_sample=1, max_context_chars=100)
        ).prepare_snapshots_for_scoring([snapshot])

        self.assertEqual(prepared[0].retrieved_contexts, ["TPS62872 最大输出电流为 12 A"])
        self.assertEqual(diagnostics["q1"]["quality_prioritized_evidence_ids"][0], "fact")

    def test_prepares_best_evidence_from_each_content_kind_for_joint_claim(self):
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["circuit", "document", "noise"],
            evidence=[
                {"id": "c1", "content": "circuit", "content_kind": "circuit_design"},
                {"id": "d1", "content": "document", "content_kind": "document"},
                {"id": "n1", "content": "noise", "content_kind": "document"},
            ],
            retrieval_summary={
                "claim_coverage": [
                    {
                        "claim_id": "joint-claim",
                        "status": "supported",
                        "evidence_ids": ["c1", "d1", "n1"],
                    }
                ],
                "evidence_quality": [
                    {"evidence_id": "c1", "score": 0.8},
                    {"evidence_id": "d1", "score": 0.9},
                    {"evidence_id": "n1", "score": 0.1},
                ],
            },
        )

        prepared, diagnostics = RagasAdapter(
            _config(max_contexts_per_sample=2, max_context_chars=100)
        ).prepare_snapshots_for_scoring([snapshot])

        self.assertEqual(prepared[0].retrieved_contexts, ["document", "circuit"])
        self.assertEqual(diagnostics["q1"]["selected_evidence_ids"], ["d1", "c1"])

    def test_prioritizes_quality_evidence_when_claim_coverage_is_unavailable(self):
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["generic helper text", "supported fact"],
            evidence=[
                {"id": "fact-1", "content": "supported fact"},
            ],
            retrieval_summary={
                "evidence_quality": [
                    {"evidence_id": "fact-1", "score": 0.95},
                ],
            },
        )

        prepared, diagnostics = RagasAdapter(
            _config(max_contexts_per_sample=2, max_context_chars=100)
        ).prepare_snapshots_for_scoring([snapshot])

        self.assertEqual(prepared[0].retrieved_contexts, ["supported fact", "generic helper text"])
        self.assertEqual(diagnostics["q1"]["context_selection"], "evidence_quality")
        self.assertEqual(diagnostics["q1"]["quality_prioritized_evidence_ids"], ["fact-1"])

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

    @patch("src.evaluation.ragas_adapter._backoff_sleep")
    def test_retries_transient_exception_for_answer_correctness(self, backoff_sleep):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["source"],
        )
        backend = RecoveringTimeoutBackend()

        result = RagasAdapter(_config(max_retries=1), backend=backend).score(
            [sample], [snapshot], ["answer_correctness"]
        )[0]

        self.assertEqual(backend.calls, 2)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.score, 0.8)
        self.assertEqual(result.details["evaluator_diagnostic"]["kind"], "recovered_after_retry")
        self.assertEqual(result.details["evaluator_diagnostic"]["attempts"], 2)
        backoff_sleep.assert_called_once()

    def test_retries_context_metric_with_a_smaller_context_budget_after_timeout(self):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["abcdef"],
        )
        backend = ContextBudgetBackend()

        result = RagasAdapter(
            _config(
                max_context_chars=6,
                max_context_chars_per_item=6,
                scoring_max_budget_attempts=3,
                scoring_context_shrink_factor=0.5,
            ),
            backend=backend,
        ).score([sample], [snapshot], ["faithfulness"])[0]

        self.assertEqual(backend.contexts, [["abcdef"], ["abc"]])
        self.assertEqual(result.score, 0.9)
        self.assertEqual(result.details["evaluator_diagnostic"]["kind"], "recovered_with_smaller_context")
        self.assertEqual(result.details["evaluator_diagnostic"]["attempts"], 2)

    def test_retries_with_smaller_context_after_a_nan_then_timeout(self):
        sample = EvaluationSample(id="q1", question="Q", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="Q",
            kb_name="kb",
            response="A",
            retrieved_contexts=["abcdef"],
        )
        backend = NanThenContextBudgetBackend()

        result = RagasAdapter(
            _config(
                max_context_chars=6,
                max_context_chars_per_item=6,
                max_retries=1,
                scoring_max_budget_attempts=3,
                scoring_context_shrink_factor=0.5,
            ),
            backend=backend,
        ).score([sample], [snapshot], ["faithfulness"])[0]

        self.assertEqual(backend.contexts, [["abcdef"], ["abcdef"], ["abc"]])
        self.assertEqual(result.score, 0.9)
        self.assertEqual(result.details["evaluator_diagnostic"]["attempts"], 3)

    def test_context_failure_diagnostic_identifies_metric_and_budget_without_prompt_text(self):
        sample = EvaluationSample(id="q1", question="secret question", reference_answer="A", kb_name="kb")
        snapshot = AnswerSnapshot(
            sample_id="q1",
            question="secret question",
            kb_name="kb",
            response="secret response",
            retrieved_contexts=["abcdef"],
        )

        result = RagasAdapter(
            _config(
                max_context_chars=6,
                max_context_chars_per_item=6,
                scoring_max_budget_attempts=2,
            ),
            backend=AlwaysFailingContextBackend(),
        ).score([sample], [snapshot], ["faithfulness"])[0]

        diagnostic = result.details["evaluator_diagnostic"]
        self.assertEqual(diagnostic["sample_id"], "q1")
        self.assertEqual(diagnostic["metric_name"], "faithfulness")
        self.assertEqual(diagnostic["context_count"], 1)
        self.assertEqual(diagnostic["context_characters"], 3)
        self.assertLessEqual(len(diagnostic["error_message"]), 200)
        self.assertNotIn("secret question", diagnostic["error_message"])
        self.assertNotIn("secret response", diagnostic["error_message"])

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
