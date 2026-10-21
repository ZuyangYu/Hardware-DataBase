"""timed_tool_call 重复检索去重（优化点 #2）：

同一 (tool, query) 二次调用返回短提示而非重复证据块，控制多轮检索下的
上下文 token 膨胀；出现新证据 id 时仍全量渲染。另含 SQL evidence id
内容化的回归测试（不同 SQL 不能共享静态 id，否则引用编号错位）。
"""

import unittest

from src.agents.schemas import Evidence
from src.agents.tools.runtime import (
    ToolRuntime,
    empty_result_message,
    format_tool_result,
    timed_tool_call,
)
from src.agents.tools.spreadsheet_tools import _sql_evidence_id


def _evidence(eid: str, content: str = "MCU 供电为 VCC3V3_MCU。") -> Evidence:
    return Evidence(
        id=eid,
        content=content,
        source_name="设计说明.docx",
        content_kind="document_text",
        processor_kind="ragflow",
        score=0.9,
        locator={},
        metadata={},
    )


class TimedToolCallRepeatTests(unittest.TestCase):
    def test_first_call_renders_full_evidence(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        items, adds_nothing = timed_tool_call(
            rt, "document_search", "电源方案", None, lambda: [_evidence("document:1")]
        )
        self.assertFalse(adds_nothing)
        rendered = format_tool_result(rt, adds_nothing, items)
        self.assertIn("MCU 供电为 VCC3V3_MCU。", rendered)
        self.assertIn("[1]", rendered)
        self.assertNotIn("完全相同", rendered)

    def test_repeat_query_with_same_evidence_returns_hint(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        timed_tool_call(
            rt,
            "document_search",
            "电源方案",
            None,
            lambda: [_evidence("document:1"), _evidence("document:2")],
        )
        items, adds_nothing = timed_tool_call(
            rt,
            "document_search",
            "电源方案",
            None,
            lambda: [_evidence("document:1"), _evidence("document:2")],
        )
        self.assertTrue(adds_nothing)
        rendered = format_tool_result(rt, adds_nothing, items)
        self.assertIn("完全相同", rendered)
        self.assertIn("[1]", rendered)
        self.assertIn("[2]", rendered)
        # 证据原文不重复下发
        self.assertNotIn("MCU 供电为 VCC3V3_MCU。", rendered)
        # 证据台账只注册一次
        self.assertEqual(len(rt.evidence), 2)

    def test_repeat_query_with_new_evidence_renders_full(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        timed_tool_call(rt, "document_search", "电源方案", None, lambda: [_evidence("document:1")])
        items, adds_nothing = timed_tool_call(
            rt, "document_search", "电源方案", None, lambda: [_evidence("document:2")]
        )
        # 同名重复查询但返回了新证据 id：不算 no-op，必须全量渲染。
        self.assertFalse(adds_nothing)
        rendered = format_tool_result(rt, adds_nothing, items)
        self.assertNotIn("完全相同", rendered)
        self.assertIn("MCU 供电为 VCC3V3_MCU。", rendered)

    def test_different_query_is_not_repeat(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        timed_tool_call(rt, "document_search", "电源方案", None, lambda: [_evidence("document:1")])
        items, adds_nothing = timed_tool_call(
            rt, "document_search", "复位方案", None, lambda: [_evidence("document:1")]
        )
        self.assertFalse(adds_nothing)
        rendered = format_tool_result(rt, adds_nothing, items)
        self.assertNotIn("完全相同", rendered)

    def test_empty_input_never_repeat(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        timed_tool_call(rt, "document_search", "", None, lambda: [])
        _, adds_nothing = timed_tool_call(rt, "document_search", "", None, lambda: [])
        self.assertFalse(adds_nothing)
        self.assertEqual(rt.queries, [])

    def test_citation_number_preserved_on_repeat(self):
        """重复调用返回同一证据时，引用编号必须稳定（模型可继续引用 [n]）。"""
        rt = ToolRuntime(kb_name="kb", ctx=None)
        first, _ = timed_tool_call(rt, "document_search", "电源", None, lambda: [_evidence("document:1")])
        second, _ = timed_tool_call(rt, "document_search", "电源", None, lambda: [_evidence("document:1")])
        self.assertEqual(
            first[0].metadata["citation_number"],
            second[0].metadata["citation_number"],
        )


class EvidenceBudgetVisibilityTests(unittest.TestCase):
    """证据预算耗尽必须显式告知模型，而不是伪装成“没有内容”。"""

    def test_exhausted_budget_explains_limit_instead_of_empty_content(self):
        rt = ToolRuntime(kb_name="kb", ctx=None, evidence_budget=1)
        first, _ = timed_tool_call(
            rt, "document_search", "q1", None, lambda: [_evidence("document:1")]
        )
        self.assertEqual(len(rt.evidence), 1)
        self.assertFalse(first.budget_blocked)

        items, adds_nothing = timed_tool_call(
            rt, "document_search", "q2", None, lambda: [_evidence("document:2")]
        )

        self.assertEqual(items, [])
        self.assertTrue(items.budget_blocked)
        rendered = format_tool_result(rt, adds_nothing, items)
        self.assertIn("证据已达上限", rendered)
        self.assertIn("不代表知识库或附件中没有相关内容", rendered)
        self.assertNotIn("（未找到相关内容）", rendered)

    def test_empty_source_result_is_not_reported_as_budget_limit(self):
        rt = ToolRuntime(kb_name="kb", ctx=None, evidence_budget=5)
        items, adds_nothing = timed_tool_call(
            rt, "document_search", "q1", None, lambda: []
        )

        self.assertFalse(items.budget_blocked)
        self.assertEqual(format_tool_result(rt, adds_nothing, items), "（未找到相关内容）")

    def test_budget_flag_is_not_shared_with_other_calls(self):
        rt = ToolRuntime(kb_name="kb", ctx=None, evidence_budget=1)
        timed_tool_call(rt, "document_search", "q1", None, lambda: [_evidence("document:1")])
        blocked, _ = timed_tool_call(
            rt, "document_search", "q2", None, lambda: [_evidence("document:2")]
        )
        self.assertTrue(blocked.budget_blocked)

        items, adds_nothing = timed_tool_call(
            rt, "document_search", "q3", None, lambda: [_evidence("document:1")]
        )

        self.assertFalse(items.budget_blocked)
        self.assertEqual(len(items), 1)


class SqlEvidenceIdTests(unittest.TestCase):
    """回归：不同 SQL 的结果不能共享静态 evidence id（否则引用编号错位）。"""

    def test_different_sql_yields_different_ids(self):
        self.assertNotEqual(
            _sql_evidence_id("SELECT a FROM t1", "result"),
            _sql_evidence_id("SELECT b FROM t2", "result"),
        )

    def test_same_sql_yields_stable_id(self):
        self.assertEqual(
            _sql_evidence_id("SELECT a FROM t1", "result"),
            _sql_evidence_id("SELECT a FROM t1", "result"),
        )
        self.assertNotEqual(
            _sql_evidence_id("SELECT a FROM t1", "result"),
            _sql_evidence_id("SELECT a FROM t1", "error"),
        )


class ToolFailureMessageTests(unittest.TestCase):
    """回归：工具故障不能被渲染成“没有资料”。

    故障（超时/后端不可用/权限拒绝）和“检索结果为空”是两种事实。前者若被
    压成"（未找到相关内容）"，模型会自信地回答“知识库里没有”，而真实原因
    可能只是 RAGFlow 挂了。
    """

    def test_failed_tool_renders_failure_not_empty(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)

        def _boom() -> list[Evidence]:
            raise TimeoutError("ragflow timeout")

        items, adds_nothing = timed_tool_call(
            rt, "document_search", "电源方案", None, _boom
        )
        rendered = format_tool_result(rt, adds_nothing, items)

        self.assertIn("本次调用 document_search 失败", rendered)
        self.assertIn("不代表知识库/附件中没有相关内容", rendered)
        self.assertNotIn("未找到相关内容", rendered)

    def test_failed_tool_renders_failure_through_empty_result_message(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)

        def _boom() -> list[Evidence]:
            raise ConnectionError("backend unavailable")

        items, _ = timed_tool_call(rt, "attachment_search", "q", None, _boom)
        rendered = empty_result_message(rt, "附件中没有找到相关内容。", items=items)

        self.assertIn("本次调用 attachment_search 失败", rendered)
        self.assertNotIn("附件中没有找到相关内容", rendered)

    def test_genuinely_empty_result_keeps_no_content_wording(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        items, adds_nothing = timed_tool_call(
            rt, "document_search", "不存在的主题", None, lambda: []
        )
        rendered = format_tool_result(rt, adds_nothing, items)
        self.assertEqual(rendered, "（未找到相关内容）")

    def test_budget_blocked_still_explains_execution_limit(self):
        rt = ToolRuntime(kb_name="kb", ctx=None, evidence_budget=1)
        timed_tool_call(
            rt, "document_search", "q1", None, lambda: [_evidence("document:1")]
        )
        items, adds_nothing = timed_tool_call(
            rt, "document_search", "q2", None, lambda: [_evidence("document:2")]
        )
        rendered = format_tool_result(rt, adds_nothing, items)
        self.assertIn("证据已达上限", rendered)
        self.assertNotIn("未找到相关内容", rendered)


if __name__ == "__main__":
    unittest.main()
