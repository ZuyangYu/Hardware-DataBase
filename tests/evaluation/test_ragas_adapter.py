import unittest

from src.evaluation.config import EvaluationConfig
from src.evaluation.ragas_adapter import RAGAS_RESULT_KEYS, RagasAdapter
from src.evaluation.schemas import AnswerSnapshot, EvaluationSample


class FakeBackend:
    def __init__(self):
        self.records = []
        self.metric_names = []

    def score(self, records, metric_names):
        self.records = records
        self.metric_names = metric_names
        return [{"answer_correctness": 0.8, "faithfulness": RuntimeError("judge failed")}]


def _config():
    return EvaluationConfig(
        llm_provider="custom",
        llm_base_url="https://judge.test/v1",
        llm_api_key="key",
        llm_model="judge",
        embedding_base_url="https://embed.test/v1",
        embedding_api_key="key",
        embedding_model="embed",
    )


class RagasAdapterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
