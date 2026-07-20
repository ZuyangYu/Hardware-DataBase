import unittest

from src.evaluation.answer_runner import AnswerRunner
from src.evaluation.schemas import EvaluationSample


def _sample(**overrides):
    values = {
        "id": "q1",
        "question": "问题",
        "reference_answer": "参考答案",
        "kb_name": "ADAS",
    }
    values.update(overrides)
    return EvaluationSample(**values)


class FakePipeline:
    def __init__(self):
        self.received_ctx = None

    def query(self, msg, kb_name, history, ctx=None, agent_thread_id=""):
        self.received_ctx = ctx
        yield "第一段"
        yield "第二段"

    def get_last_retrieval_summary(self):
        return {
            "status": "success",
            "evidence": [
                {"content": "电路上下文", "content_kind": "circuit_design"},
                {"content": "文档上下文", "metadata": {"content_kind": "document"}},
            ],
        }


class AnswerRunnerTests(unittest.TestCase):
    def test_collect_joins_stream_and_extracts_contexts(self):
        pipeline = FakePipeline()

        snapshot = AnswerRunner(lambda: pipeline).collect(_sample())

        self.assertEqual(snapshot.status, "success")
        self.assertEqual(snapshot.response, "第一段第二段")
        self.assertEqual(snapshot.retrieved_contexts, ["电路上下文", "文档上下文"])
        self.assertEqual(len(snapshot.evidence), 2)

    def test_collect_maps_department_to_request_metadata(self):
        pipeline = FakePipeline()
        sample = _sample(
            request_context={
                "user_id": "evaluator",
                "department_id": 96,
                "allowed_kbs": ["96:ADAS"],
                "kb_permissions": {"96:ADAS": "read"},
            }
        )

        AnswerRunner(lambda: pipeline).collect(sample)

        self.assertEqual(pipeline.received_ctx.user_id, "evaluator")
        self.assertEqual(pipeline.received_ctx.metadata["department_id"], 96)

    def test_collect_returns_failed_snapshot_when_pipeline_raises(self):
        class BrokenPipeline(FakePipeline):
            def query(self, *args, **kwargs):
                raise RuntimeError("secret-token must not leak")

        snapshot = AnswerRunner(lambda: BrokenPipeline()).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error_stage, "answer_collection")
        self.assertNotIn("secret-token", snapshot.error_message)

    def test_collect_reports_pipeline_initialization_failure(self):
        def broken_factory():
            raise RuntimeError("cannot initialize")

        snapshot = AnswerRunner(broken_factory).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error_stage, "pipeline_initialization")


if __name__ == "__main__":
    unittest.main()
