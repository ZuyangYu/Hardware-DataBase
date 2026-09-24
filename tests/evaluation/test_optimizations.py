"""新增评估优化点的回归测试：退避、降级、去重、分段、覆盖率、预检。"""

import unittest
from unittest.mock import patch

from src.agents.runner import _AnswerSplitter, strip_narration_segments
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


class AnswerSplitterTests(unittest.TestCase):
    """增量 narration/answer 分类：叙述外的文本实时下发，叙述文本经事件回收。"""

    def _run(self, chunks):
        narrations = []
        splitter = _AnswerSplitter(on_narration=narrations.append)
        deltas = []
        for chunk in chunks:
            delta = splitter.feed(chunk, chunk.content)
            if delta:
                deltas.append(delta)
        return deltas, narrations, splitter.finish()

    def test_interim_narration_with_tool_calls_is_not_answer(self):
        chunks = [
            _FakeChunk("我来帮您查找资料。", tid="m1"),
            _FakeChunk("", tid="m1", tool_calls=[{"name": "search"}]),
            _FakeChunk("MCU 的供电为 VCC3V3_MCU。", tid="m2"),
        ]
        deltas, narrations, answer = self._run(chunks)
        self.assertEqual(deltas, ["我来帮您查找资料。", "MCU 的供电为 VCC3V3_MCU。"])
        self.assertEqual(narrations, ["我来帮您查找资料。"])
        self.assertEqual(answer, "MCU 的供电为 VCC3V3_MCU。")

    def test_text_then_tool_call_then_answer(self):
        chunks = [
            _FakeChunk("让我确认一下引脚。", tid="m1"),
            _FakeChunk("调用网表检索。", tid="m2", tool_calls=[{"name": "circuit"}]),
            _FakeChunk("最终答案。", tid="m3"),
        ]
        deltas, narrations, answer = self._run(chunks)
        self.assertEqual(deltas, ["让我确认一下引脚。", "最终答案。"])
        # m2 的文本与工具调用同块到达，从未下发，无需回收。
        self.assertEqual(narrations, ["让我确认一下引脚。"])
        self.assertEqual(answer, "最终答案。")

    def test_plain_answer_without_tools_kept_whole(self):
        deltas, narrations, answer = self._run([_FakeChunk("直接回答。", tid="m1")])
        self.assertEqual(deltas, ["直接回答。"])
        self.assertEqual(narrations, [])
        self.assertEqual(answer, "直接回答。")

    def test_positional_classification_without_tool_metadata(self):
        """真实观测形态：工具调用信息不在 chunk 上，只有后续消息开始了才知道前一段是叙述。"""

        chunks = [
            _FakeChunk("我先了解知识库中有哪些资料源。", tid="m1"),
            _FakeChunk("初步检索发现…证据不够完整。", tid="m2"),
            _FakeChunk("我再检索一次。", tid="m3"),
            _FakeChunk("基于知识库检索，最终结论是 VCC3V3_MCU。", tid="m4"),
        ]
        deltas, narrations, answer = self._run(chunks)
        self.assertEqual(deltas, [chunk.content for chunk in chunks])
        self.assertEqual(narrations, [chunk.content for chunk in chunks[:3]])
        self.assertEqual(answer, "基于知识库检索，最终结论是 VCC3V3_MCU。")

    def test_chunks_without_id_join_last_segment(self):
        chunks = [
            _FakeChunk("最终答案", tid=None),
            _FakeChunk(" continued。", tid=None),
        ]
        deltas, narrations, answer = self._run(chunks)
        self.assertEqual(deltas, ["最终答案", " continued。"])
        self.assertEqual(narrations, [])
        self.assertEqual(answer, "最终答案 continued。")

    def test_text_after_tool_calls_starts_new_segment(self):
        """跨调用复用同一 id（或无 id）时，叙述后的文本必须开新段成为答案。"""
        chunks = [
            _FakeChunk("让我检索。", tid="m1", tool_calls=[{"name": "search"}]),
            _FakeChunk("最终结论。", tid="m1"),
        ]
        deltas, narrations, answer = self._run(chunks)
        self.assertEqual(deltas, ["最终结论。"])
        self.assertEqual(narrations, [])
        self.assertEqual(answer, "最终结论。")

    def test_strip_narration_segments_recovers_authoritative_answer(self):
        joined = "我先了解…初步检索发现…最终结论是 VCC3V3_MCU。"
        self.assertEqual(
            strip_narration_segments(joined, ["我先了解…", "初步检索发现…"]),
            "最终结论是 VCC3V3_MCU。",
        )
        # 顺序位置找不到时退化为全局首次出现；完全找不到则原样保留。
        self.assertEqual(strip_narration_segments("最终结论", ["不存在文本"]), "最终结论")
        self.assertEqual(strip_narration_segments("叙述最终", ["叙述"]), "最终")


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
