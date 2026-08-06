"""Tests for the agent substrate fixes: native tool-calling transport, real
grounding, and degraded-event surfacing. Mirrors the flat unittest.TestCase
convention used by the rest of tests/."""

import json
import unittest
from unittest import mock

import requests

from config import settings
from src.agents.graph import _chat_structured, _response_tool, judge_sufficiency, route_query
from src.core.llm_client import ChatToolResult, LLMClient, LLMClientConfig


class _ToolCallLLM:
    """Fake LLM whose chat_with_tools returns a canned ChatToolResult."""

    def __init__(self, *, tool_calls=None, content="", tool_call_supported=True, exc=None):
        self._tool_calls = tool_calls
        self._content = content
        self._supported = tool_call_supported
        self._exc = exc
        self.calls = []

    def chat_with_tools(self, messages, *, tools, tool_choice, usage_stage=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "stage": usage_stage})
        if self._exc is not None:
            raise self._exc
        return ChatToolResult(
            content=self._content,
            tool_calls=self._tool_calls,
            tool_call_supported=self._supported,
        )

    def stream_chat_with_tools(self, messages, *, tools, tool_choice, usage_stage=None, on_delta=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "stage": usage_stage, "streamed": True})
        if self._exc is not None:
            raise self._exc
        if on_delta is not None and self._content:
            on_delta(self._content)
        return ChatToolResult(
            content=self._content,
            tool_calls=self._tool_calls,
            tool_call_supported=self._supported,
        )


class ChatStructuredTests(unittest.TestCase):
    def test_returns_tool_call_arguments_when_present(self):
        tool = _response_tool("emit_plan", "plan", {"calls": {"type": "array"}}, ["calls"])
        llm = _ToolCallLLM(tool_calls=[{"id": "1", "name": "emit_plan", "arguments": {"calls": [1, 2]}}])
        payload = _chat_structured(llm, [{"role": "user", "content": "x"}], tool, "plan")
        self.assertEqual(payload, {"calls": [1, 2]})

    def test_falls_back_to_json_text_when_no_tool_call(self):
        tool = _response_tool("emit_plan", "plan", {"calls": {"type": "array"}}, ["calls"])
        llm = _ToolCallLLM(
            tool_calls=None,
            content='{"calls": [9]}',
            tool_call_supported=False,
        )
        payload = _chat_structured(llm, [{"role": "user", "content": "x"}], tool, "plan")
        self.assertEqual(payload, {"calls": [9]})

    def test_raises_when_neither_tool_call_nor_parseable_content(self):
        tool = _response_tool("emit_plan", "plan", {"calls": {"type": "array"}}, ["calls"])
        llm = _ToolCallLLM(tool_calls=None, content="", tool_call_supported=False)
        with self.assertRaises(RuntimeError):
            _chat_structured(llm, [{"role": "user", "content": "x"}], tool, "plan")


class JudgeSufficiencyToolCallTests(unittest.TestCase):
    def _state(self):
        return {
            "user_query": "R-123 的用量是多少？",
            "retrieval_round": 1,
            "coverage_matrix": {"coverage": [], "conflicts": []},
            "question_analysis": {
                "sub_questions": [
                    {"id": "sq_1", "question": "R-123 的用量是多少？", "expected_evidence": ["spreadsheet_table"]}
                ]
            },
            "retrieval_ledger": [],
            "merged_evidence": [{"id": "e1", "source_name": "bom.xlsx", "content": "片段", "content_kind": "spreadsheet_table", "score": 0.5}],
            "intermediate_answer": "草稿：料号 R-123 用量未知。",
        }

    def test_native_tool_call_transport_carries_status_and_suggested_queries(self):
        args = {
            "status": "insufficient_need_more",
            "reason": "需查 BOM 用量",
            "missing": ["R-123 在 BOM 中的用量"],
            "suggested_queries": [
                {"query": "R-123 用量", "tool_name": "spreadsheet_semantic", "source_name": "bom.xlsx", "reason": "查 BOM"}
            ],
        }
        llm = _ToolCallLLM(tool_calls=[{"id": "1", "name": "report_sufficiency", "arguments": args}])
        result = judge_sufficiency(self._state(), llm)
        decision = result["sufficiency"]
        self.assertEqual(decision["status"], "insufficient_need_more")
        self.assertEqual(len(decision["suggested_queries"]), 1)
        self.assertEqual(decision["suggested_queries"][0]["query"], "R-123 用量")

    def test_llm_failure_fails_open_to_partial(self):
        llm = _ToolCallLLM(exc=RuntimeError("model down"))
        result = judge_sufficiency(self._state(), llm)
        self.assertEqual(result["sufficiency"]["status"], "partial_but_answerable")
        self.assertEqual(result["sufficiency"]["suggested_queries"], [])


class VerifyGroundingTests(unittest.TestCase):
    def _runner(self, llm):
        from src.agents.runner import MultiSourceAgentRunner

        return MultiSourceAgentRunner(rag_backend=mock.MagicMock(), llm_client=llm)

    def _state(self, answer="料号 R-123 用量为 100。"):
        return {
            "answer": answer,
            "merged_evidence": [
                {"id": "e1", "evidence_id": "e1", "source_name": "bom.xlsx", "content": "R-123 用量 100", "content_kind": "spreadsheet_table", "score": 0.8}
            ],
            "coverage_matrix": {"conflicts": []},
            "evidence_quality": [],
            "retrieval_round": 1,
            "trace": [],
        }

    def test_unsupported_claims_make_answer_ungrounded(self):
        args = {
            "assertions": [
                {"text": "R-123 用量为 100。", "evidence_ids": ["e1"], "assertion_kind": "confirmed_fact"},
                {"text": "供应商是 ACME。", "evidence_ids": [], "assertion_kind": "inference"},
            ],
            "unsupported_claims": ["供应商是 ACME。"],
        }
        runner = self._runner(_ToolCallLLM(tool_calls=[{"id": "1", "name": "verify_grounding", "arguments": args}]))
        result = runner._verify_grounding(self._state())
        verification = result["verification"]
        self.assertFalse(verification["grounded"])
        self.assertEqual(verification["grounding_method"], "llm_claim_check")
        self.assertEqual(verification["unsupported_claims"], ["供应商是 ACME。"])
        self.assertLess(verification["citation_coverage"], 1.0)

    def test_all_claims_supported_means_grounded(self):
        args = {
            "assertions": [
                {"text": "R-123 用量为 100。", "evidence_ids": ["e1"], "assertion_kind": "confirmed_fact"},
            ],
            "unsupported_claims": [],
        }
        runner = self._runner(_ToolCallLLM(tool_calls=[{"id": "1", "name": "verify_grounding", "arguments": args}]))
        result = runner._verify_grounding(self._state())
        self.assertTrue(result["verification"]["grounded"])
        self.assertEqual(result["verification"]["citation_coverage"], 1.0)

    def test_grounding_llm_failure_reports_unverified_not_grounded(self):
        runner = self._runner(_ToolCallLLM(exc=RuntimeError("grounding model down")))
        result = runner._verify_grounding(self._state())
        verification = result["verification"]
        self.assertEqual(verification["grounded"], "unverified")
        self.assertEqual(verification["grounding_method"], "llm_fallback_unverified")
        # final_response still carries the answer body for the stream.
        self.assertEqual(result["final_response"], "料号 R-123 用量为 100。")

    def test_no_evidence_is_ungrounded_without_llm_call(self):
        runner = self._runner(_ToolCallLLM())
        state = self._state(answer="当前知识库中未找到可支撑回答的证据。")
        state["merged_evidence"] = []
        result = runner._verify_grounding(state)
        self.assertFalse(result["verification"]["grounded"])
        self.assertEqual(result["verification"]["grounding_method"], "no_evidence")


class ChatWithToolsTests(unittest.TestCase):
    def _client(self):
        return LLMClient(
            config=LLMClientConfig(
                provider=settings.Provider.CUSTOM,
                base_url="https://example.test/v1",
                model="test-model",
                api_key="k",
            )
        )

    def _resp(self, *, status_code, text="", payload=None):
        resp = mock.MagicMock()
        resp.status_code = status_code
        resp.text = text
        if payload is not None:
            resp.json.return_value = payload
        if status_code >= 400:
            resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}", response=resp)
        return resp

    def test_provider_rejecting_tools_falls_back_to_plain_completion(self):
        from src.core import llm_client as mod

        bad = self._resp(status_code=400, text="unsupported: tools parameter not supported for this model")
        good = self._resp(
            status_code=200,
            payload={"choices": [{"message": {"content": "{\"a\": 1}"}}], "usage": {}},
        )
        with mock.patch.object(mod.requests, "post", side_effect=[bad, good]) as post:
            result = self._client().chat_with_tools(
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
                tool_choice="auto",
                usage_stage="t",
            )
        self.assertIsNone(result.tool_calls)
        self.assertFalse(result.tool_call_supported)
        self.assertEqual(result.content, '{"a": 1}')
        self.assertEqual(post.call_count, 2)

    def test_tool_calls_parsed_from_response(self):
        from src.core import llm_client as mod

        payload = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{\"x\": 2}"}}
                        ],
                    }
                }
            ],
            "usage": {},
        }
        good = self._resp(status_code=200, payload=payload)
        with mock.patch.object(mod.requests, "post", side_effect=[good]) as post:
            result = self._client().chat_with_tools(
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
                tool_choice={"type": "function", "function": {"name": "f"}},
                usage_stage="t",
            )
        self.assertIsNotNone(result.tool_calls)
        self.assertEqual(result.tool_calls[0]["name"], "f")
        self.assertEqual(result.tool_calls[0]["arguments"], {"x": 2})
        self.assertEqual(post.call_count, 1)


class _ExplodingRouteLLM:
    """Proves the routing LLM is not consulted when _skip_route_llm is set."""

    def chat(self, messages):
        raise AssertionError("route LLM must not be called with _skip_route_llm")


class _RouterSmallTalkLLM:
    def chat(self, messages):
        return '{"category": "small_talk", "needs_retrieval": false, "reason": "问候"}'


class RouteSkipTests(unittest.TestCase):
    def test_with_skip_flag_ignores_llm_and_uses_deterministic(self):
        state = {"user_query": "U1800 每个关键引脚连接到哪个网络？", "history": [], "trace": [], "_skip_route_llm": True}
        result = route_query(state, _ExplodingRouteLLM())
        self.assertTrue(result["route_decision"]["needs_retrieval"])

    def test_with_skip_flag_overrides_smalltalk_llm(self):
        state = {"user_query": "U1800 引脚", "history": [], "trace": [], "_skip_route_llm": True}
        result = route_query(state, _RouterSmallTalkLLM())
        # Deterministic hardware routing returns retrieve even though the LLM
        # would have said small_talk, proving the LLM was bypassed.
        self.assertTrue(result["route_decision"]["needs_retrieval"])

    def test_without_skip_flag_routes_via_llm(self):
        state = {"user_query": "U1800 引脚", "history": [], "trace": []}
        result = route_query(state, _RouterSmallTalkLLM())
        self.assertFalse(result["route_decision"]["needs_retrieval"])


class _FakeStreamResponse:
    encoding = "utf-8"

    def __init__(self, lines, status_code=200, text=""):
        self._lines = lines
        self.status_code = status_code
        self.text = text

    def iter_lines(self, chunk_size=1, decode_unicode=True):
        return iter(self._lines)

    def raise_for_status(self):
        pass


class StreamChatWithToolsTests(unittest.TestCase):
    def _client(self):
        return LLMClient(
            config=LLMClientConfig(
                provider=settings.Provider.CUSTOM,
                base_url="https://example.test/v1",
                model="test-model",
                api_key="k",
            )
        )

    def _sse(self, payload):
        return f"data: {json.dumps(payload, ensure_ascii=False)}"

    def test_streams_content_deltas_and_accumulates_tool_call_args(self):
        from src.core import llm_client as mod

        lines = [
            self._sse({"choices": [{"delta": {"content": "hi "}}]}), "",
            self._sse({"choices": [{"delta": {"content": "there"}}]}), "",
            self._sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "emit_plan", "arguments": "{\"calls\": [1"}}]}}]}), "",
            self._sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ", 2]}"}}]}}]}), "",
            "data: [DONE]", "",
        ]
        deltas = []
        with mock.patch.object(mod.requests, "post", return_value=_FakeStreamResponse(lines)) as post:
            result = self._client().stream_chat_with_tools(
                [{"role": "user", "content": "x"}],
                tools=[{"type": "function", "function": {"name": "emit_plan", "parameters": {}}}],
                tool_choice="auto",
                usage_stage="t",
                on_delta=deltas.append,
            )
        self.assertEqual(deltas, ["hi ", "there"])
        self.assertEqual(result.content, "hi there")
        self.assertIsNotNone(result.tool_calls)
        self.assertEqual(result.tool_calls[0]["name"], "emit_plan")
        self.assertEqual(result.tool_calls[0]["arguments"], {"calls": [1, 2]})
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
