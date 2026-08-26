import unittest

from src.evaluation import answer_runner
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
    def test_extract_scored_response_removes_trailing_administrative_sections(self):
        response = (
            "核心结论：U1700 的输入为 VCC3V3。\n\n"
            "### 来源说明\n"
            "证据 [1] 来自网表。\n\n"
            "子问题 sq_1 已完全覆盖，无需额外补充。"
        )

        scored_response, diagnostic = answer_runner.extract_scored_response(response)

        self.assertEqual(scored_response, "核心结论：U1700 的输入为 VCC3V3。")
        self.assertTrue(diagnostic["filtered"])
        self.assertEqual(diagnostic["removed_sections"], ["来源说明", "子问题覆盖状态"])

    def test_extract_scored_response_keeps_subquestion_body_and_missing_disclosure(self):
        response = (
            "### 子问题 1：U1700 的输入是什么？\n"
            "结论：输入为 VCC3V3。\n\n"
            "### 缺失信息\n"
            "未找到输出引脚的直接证据。"
        )

        scored_response, diagnostic = answer_runner.extract_scored_response(response)

        self.assertEqual(scored_response, response)
        self.assertFalse(diagnostic["filtered"])

    def test_extract_scored_response_keeps_missing_disclosure_after_source_section(self):
        response = (
            "核心结论：输入为 VCC3V3。\n\n"
            "**来源说明**\n"
            "证据 [1] 来自网表。\n\n"
            "**缺失信息**\n"
            "未找到输出引脚的直接证据。"
        )

        scored_response, _ = answer_runner.extract_scored_response(response)

        self.assertEqual(
            scored_response,
            "核心结论：输入为 VCC3V3。\n\n**缺失信息**\n未找到输出引脚的直接证据。",
        )

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
                "roles": ["dept_admin"],
                "department_id": 96,
                "allowed_kbs": ["96:ADAS"],
                "kb_permissions": {"96:ADAS": "read"},
            }
        )

        AnswerRunner(lambda: pipeline).collect(sample)

        # Security posture: dataset-supplied identity is untrusted. user_id is
        # pinned to "evaluation", roles are pinned to plain user, and the
        # declared identity survives only as metadata.
        self.assertEqual(pipeline.received_ctx.user_id, "evaluation")
        self.assertEqual(pipeline.received_ctx.roles, ["user"])
        self.assertEqual(pipeline.received_ctx.metadata["department_id"], 96)
        self.assertEqual(pipeline.received_ctx.metadata.get("declared_user"), "evaluator")

    def test_collect_returns_failed_snapshot_when_pipeline_raises(self):
        class BrokenPipeline(FakePipeline):
            def query(self, *args, **kwargs):
                raise RuntimeError("secret-token must not leak")

        snapshot = AnswerRunner(lambda: BrokenPipeline()).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error_stage, "answer_collection")
        self.assertIn("RuntimeError", snapshot.error_message)
        self.assertNotIn("secret-token", snapshot.error_message)

    def test_collect_redacts_sensitive_values_from_failure_diagnostics(self):
        class BrokenPipeline(FakePipeline):
            def query(self, *args, **kwargs):
                raise RuntimeError("api_key=secret-value")

        snapshot = AnswerRunner(lambda: BrokenPipeline()).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertIn("RuntimeError", snapshot.error_message)
        self.assertIn("api_key=[redacted]", snapshot.error_message)
        self.assertNotIn("secret-value", snapshot.error_message)

    def test_collect_marks_streamed_system_error_as_failed_snapshot(self):
        class ErrorTextPipeline(FakePipeline):
            def query(self, *args, **kwargs):
                yield "系统错误: provider token must not leak"

        snapshot = AnswerRunner(lambda: ErrorTextPipeline()).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error_stage, "answer_collection")
        self.assertNotIn("provider token", snapshot.error_message)
        self.assertEqual(snapshot.response, "")

    def test_collect_marks_failed_retrieval_summary_as_failed_snapshot(self):
        class FailedSummaryPipeline(FakePipeline):
            def get_last_retrieval_summary(self):
                return {"status": "failed", "evidence": [{"content": "partial evidence"}]}

        snapshot = AnswerRunner(lambda: FailedSummaryPipeline()).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error_stage, "answer_collection")
        self.assertEqual(snapshot.response, "")

    def test_collect_preserves_sanitized_failed_retrieval_diagnostics(self):
        class FailedSummaryPipeline(FakePipeline):
            def get_last_retrieval_summary(self):
                return {
                    "status": "failed",
                    "error_stage": "answer",
                    "error_message": "api_key=secret-value",
                    "evidence": [{"content": "partial evidence"}],
                    "tool_diagnostics": [{"status": "failed", "error": "timeout"}],
                }

        snapshot = AnswerRunner(lambda: FailedSummaryPipeline()).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error_stage, "answer")
        self.assertIn("api_key=[redacted]", snapshot.error_message)
        self.assertNotIn("secret-value", snapshot.error_message)
        self.assertEqual(snapshot.evidence, [{"content": "partial evidence"}])
        self.assertEqual(snapshot.retrieved_contexts, ["partial evidence"])
        self.assertEqual(snapshot.retrieval_summary["tool_diagnostics"][0]["error"], "timeout")
        self.assertEqual(snapshot.response, "")

    def test_collect_reports_pipeline_initialization_failure(self):
        def broken_factory():
            raise RuntimeError("cannot initialize")

        snapshot = AnswerRunner(broken_factory).collect(_sample())

        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error_stage, "pipeline_initialization")


if __name__ == "__main__":
    unittest.main()
