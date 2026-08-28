"""新增评估优化点的回归测试：退避、降级、去重、分段、覆盖率、预检。"""

import unittest
from unittest.mock import patch

from src.agents.runner import _split_answer_segments
from src.evaluation.config import EvaluationConfig
from src.evaluation.gates import DEFAULT_THRESHOLDS, evaluate_gate
from src.evaluation.ragas_adapter import RagasAdapter, _NativeRagasBackend, strip_markup
from src.evaluation.schemas import MetricResult, SampleResult


class _FakeChunk:
    def __init__(self, text="", *, tid=None, tool_calls=None):
        self.content = text
        self.id = tid
        self.tool_calls = tool_calls or []
        self.tool_call_chunks = []


class SplitAnswerSegmentsTests(unittest.TestCase):
    def test_interim_narration_with_tool_calls_is_not_answer(self):
        chunks = [
            ("a", _FakeChunk("我来帮您查找资料。", tid="m1")),
            ("b", _FakeChunk("", tid="m1", tool_calls=[{"name": "search"}])),
            ("c", _FakeChunk("MCU 的供电为 VCC3V3_MCU。", tid="m2")),
        ]
        answer, narration = _split_answer_segments(chunks)
        self.assertEqual(answer, "MCU 的供电为 VCC3V3_MCU。")
        self.assertIn("我来帮您查找资料", narration)

    def test_text_then_tool_call_then_answer(self):
        chunks = [
            ("a", _FakeChunk("让我确认一下引脚。", tid="m1")),
            ("b", _FakeChunk("调用网表检索。", tid="m2", tool_calls=[{"name": "circuit"}])),
            ("c", _FakeChunk("最终答案。", tid="m3")),
        ]
        answer, narration = _split_answer_segments(chunks)
        self.assertEqual(answer, "最终答案。")
        self.assertIn("让我确认一下引脚", narration)
        self.assertIn("调用网表检索", narration)

    def test_plain_answer_without_tools_kept_whole(self):
        chunks = [("a", _FakeChunk("直接回答。", tid="m1"))]
        answer, narration = _split_answer_segments(chunks)
        self.assertEqual(answer, "直接回答。")
        self.assertEqual(narration, "")

    def test_positional_split_without_tool_metadata(self):
        """真实观测形态：工具调用信息不在 chunk 上，只有消息序列。"""

        chunks = [
            ("a", _FakeChunk("我先了解知识库中有哪些资料源。", tid="m1")),
            ("b", _FakeChunk("初步检索发现…证据不够完整。", tid="m2")),
            ("c", _FakeChunk("我再检索一次。", tid="m3")),
            ("d", _FakeChunk("基于知识库检索，最终结论是 VCC3V3_MCU。", tid="m4")),
        ]
        answer, narration = _split_answer_segments(chunks)
        self.assertEqual(answer, "基于知识库检索，最终结论是 VCC3V3_MCU。")
        for w in ("我先了解", "初步检索", "我再检索"):
            self.assertIn(w, narration)
        self.assertNotIn("最终结论", narration)

    def test_chunks_without_id_join_last_segment(self):
        chunks = [
            ("a", _FakeChunk("最终答案", tid=None)),
            ("b", _FakeChunk(" continued。", tid=None)),
        ]
        answer, _ = _split_answer_segments(chunks)
        self.assertEqual(answer, "最终答案 continued。")


class StripMarkupDedupTests(unittest.TestCase):
    def _adapter(self):
        return RagasAdapter(EvaluationConfig(
            llm_provider="custom", llm_base_url="http://x/v1", llm_api_key="k",
            llm_model="m", embedding_base_url="http://x/v1", embedding_api_key="ek",
            embedding_model="e",
            max_contexts_per_sample=10, max_context_chars=10_000,
            max_context_chars_per_item=5_000,
        ))

    def test_near_duplicate_contexts_collapse(self):
        adapter = self._adapter()
        ctx_a = "MCU 供电网络为 VCC3V3_MCU 与 VCC1V25_MCU，经过过压保护电路后供电。" + "细节" * 80
        ctx_b = "MCU 供电网络为 VCC3V3_MCU 与 VCC1V25_MCU，经过过压保护电路后供电。" + "细节" * 80 + "尾部差异"
        bounded, diag = adapter._bounded_contexts([ctx_a, ctx_b])
        self.assertEqual(len(bounded), 1)
        self.assertTrue(diag["contexts_truncated"])

    def test_html_tables_are_stripped_before_budget(self):
        adapter = self._adapter()
        ctx = "<table><tr><td>序号</td><td>VCC3V3_MCU</td></tr></table>"
        bounded, _ = adapter._bounded_contexts([ctx])
        self.assertEqual(bounded, ["序号 VCC3V3_MCU"])

    def test_plain_text_untouched(self):
        self.assertEqual(strip_markup("a < b 且 c > d"), "a < b 且 c > d")


class GateCoverageTests(unittest.TestCase):
    def _results(self, *, n=25, extra=None):
        results = []
        for i in range(n):
            metrics = [MetricResult(sample_id=f"s{i}", metric_name="completeness", score=0.9)]
            if extra and i == 0:
                metrics.extend(extra)
            results.append(SampleResult(sample_id=f"s{i}", metrics=metrics))
        return results

    def test_single_sample_metric_never_fails_gate(self):
        results = self._results(extra=[MetricResult(sample_id="s0", metric_name="context_recall", score=1.0)])
        gate = evaluate_gate(results, DEFAULT_THRESHOLDS)
        self.assertTrue(gate.passed)
        self.assertEqual(gate.metric_counts["context_recall"], 1)

    def test_full_coverage_metric_below_threshold_fails(self):
        results = [
            SampleResult(sample_id=f"s{i}", metrics=[
                MetricResult(sample_id=f"s{i}", metric_name="completeness", score=0.9),
                MetricResult(sample_id=f"s{i}", metric_name="context_recall", score=0.5),
            ])
            for i in range(25)
        ]
        gate = evaluate_gate(results, DEFAULT_THRESHOLDS)
        self.assertFalse(gate.passed)
        self.assertTrue(any("context_recall" in f for f in gate.failures))


class ConfigFallbackTests(unittest.TestCase):
    def test_fallback_fields_parse_from_environment(self):
        import os
        env = {
            "AGENT_LLM_PROVIDER": "custom",
            "AGENT_CUSTOM_BASE_URL": "https://judge.test/v1",
            "AGENT_CUSTOM_API_KEY": "k",
            "AGENT_CUSTOM_MODEL": "judge",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
            "EVAL_LLM_FALLBACK_BASE_URL": "https://backup.test/v1",
            "EVAL_LLM_FALLBACK_API_KEY": "bk",
            "EVAL_LLM_FALLBACK_MODEL": "backup",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()
        self.assertTrue(config.fallback_ready)
        self.assertEqual(config.llm_fallback_model, "backup")

    def test_fallback_absent_by_default(self):
        import os
        env = {
            "AGENT_LLM_PROVIDER": "custom",
            "AGENT_CUSTOM_BASE_URL": "https://judge.test/v1",
            "AGENT_CUSTOM_API_KEY": "k",
            "AGENT_CUSTOM_MODEL": "judge",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()
        self.assertFalse(config.fallback_ready)


class NativeBackendFallbackTests(unittest.TestCase):
    def test_is_failed_value_detects_nan_and_exception(self):
        from math import nan
        self.assertTrue(_NativeRagasBackend._is_failed_value(nan))
        self.assertTrue(_NativeRagasBackend._is_failed_value(RuntimeError("x")))
        self.assertTrue(_NativeRagasBackend._is_failed_value(None))
        self.assertFalse(_NativeRagasBackend._is_failed_value(0.5))
        self.assertFalse(_NativeRagasBackend._is_failed_value(0.0))


if __name__ == "__main__":
    unittest.main()
