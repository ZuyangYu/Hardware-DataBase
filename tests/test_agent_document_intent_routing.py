"""聊天入口的文档生成流程确定性路由测试。

有模板上下文且明确要求生成文档的轮次必须进入文档流程（只装配文档工具 +
专用流程提示词），而不是交给通用问答 Agent 自由选工具。
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from src.agents.runner import MultiSourceAgentRunner, resolve_document_flow_route
from src.document_authoring.chat_context import build_document_context

DOCUMENT_TOOL_NAMES = {
    "get_document_template_analysis",
    "start_document_generation_session",
    "answer_clarification",
    "confirm_generation_session",
    "create_document_work_order",
    "get_document_generation_status",
}

RETRIEVAL_TOOL_NAMES = {
    "document_search",
    "memory_search",
    "conversation_search",
}


class _FakeRAGBackend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


class _FakeCtx:
    user_id = "user-1"
    tenant_id = "tenant-1"

    def has_kb_permission(self, kb_name, permission):
        return True


def _context():
    return build_document_context(
        {
            "analysis_id": "analysis-1",
            "template_version_id": "template-1",
            "knowledge_base_name": "kb_hw",
        },
        ctx=_FakeCtx(),
    )


def _expired_context():
    return build_document_context(
        {
            "analysis_id": "analysis-1",
            "template_version_id": "template-1",
            "knowledge_base_name": "kb_hw",
        },
        ctx=_FakeCtx(),
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )


def _install_fake_agent(monkeypatch):
    from src.agents import runner as runner_mod

    captured = {}
    fake_agent = type("F", (), {})()

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    def fake_stream(*args, **kwargs):
        captured["stream_config"] = kwargs.get("config") or {}
        return iter(((AIMessage(content="好的"), {"langgraph_node": "model"}),))

    fake_agent.stream = fake_stream

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    return captured


def _tool_name(tool) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))


@pytest.mark.parametrize(
    "query",
    [
        "请根据模板生成 ICD 文档",
        "帮我创建工单",
        "把模板填充成报告",
        "generate the report from the attached template",
    ],
)
def test_generation_intent_routes_to_document_flow(query):
    routed, _ = resolve_document_flow_route(
        document_context=_context(), query=query, has_document_tools=True
    )
    assert routed is True


@pytest.mark.parametrize(
    "query",
    [
        "这个模板里有哪些字段？",
        "知识库里有没有类似的 ICD 资料？",
        "show me the sources in this knowledge base",
    ],
)
def test_question_intent_stays_with_general_agent(query):
    routed, _ = resolve_document_flow_route(
        document_context=_context(), query=query, has_document_tools=True
    )
    assert routed is False


def test_route_requires_tools_fresh_context_and_query():
    routed, _ = resolve_document_flow_route(
        document_context=_context(), query="生成文档", has_document_tools=False
    )
    assert routed is False
    routed, _ = resolve_document_flow_route(
        document_context=None, query="生成文档", has_document_tools=True
    )
    assert routed is False
    routed, _ = resolve_document_flow_route(
        document_context=_expired_context(), query="生成文档", has_document_tools=True
    )
    assert routed is False


def test_explicit_true_routes_without_intent_keywords():
    routed, by = resolve_document_flow_route(
        document_context=_context(), query="继续", has_document_tools=True,
        explicit_flow=True,
    )
    assert routed is True and by == "explicit"


def test_explicit_false_blocks_regex_hit():
    routed, by = resolve_document_flow_route(
        document_context=_context(), query="帮我生成ICD", has_document_tools=True,
        explicit_flow=False,
    )
    assert routed is False and by == "explicit"


def test_none_falls_back_to_regex():
    routed, by = resolve_document_flow_route(
        document_context=_context(), query="帮我生成ICD", has_document_tools=True,
        explicit_flow=None,
    )
    assert routed is True and by == "regex"


def test_routed_turn_assembles_document_only_agent(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    events = []
    deltas = list(
        runner.stream(
            query="请根据模板生成 ICD 文档",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=_context(),
            event_callback=events.append,
        )
    )

    assert deltas == ["好的"]
    names = {_tool_name(tool) for tool in captured["tools"]}
    assert names and names <= DOCUMENT_TOOL_NAMES
    assert names & RETRIEVAL_TOOL_NAMES == set()
    prompt = captured["system_prompt"]
    assert "文档生成流程助手" in prompt
    assert "analysis-1" in prompt and "template-1" in prompt
    assert captured["stream_config"]["recursion_limit"] >= 24
    routed_events = [
        event
        for event in events
        if event.get("type") == "stage"
        and event.get("payload", {}).get("key") == "document_flow_routed"
    ]
    assert len(routed_events) == 1
    # document_flow=None falls back to the intent-keyword regex here.
    assert routed_events[0]["payload"]["routed_by"] == "regex"


def test_non_routed_turn_keeps_general_toolset(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    events = []
    list(
        runner.stream(
            query="这个模板里有哪些字段？",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=_context(),
            event_callback=events.append,
        )
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert DOCUMENT_TOOL_NAMES <= names
    assert names & RETRIEVAL_TOOL_NAMES != set()
    assert "文档生成流程助手" not in captured["system_prompt"]
    assert not events


def test_routed_turn_without_document_tools_falls_back_to_general_agent(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    list(
        runner.stream(
            query="请根据模板生成 ICD 文档",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
        )
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert names & RETRIEVAL_TOOL_NAMES != set()
    assert "文档生成流程助手" not in captured["system_prompt"]


def test_explicit_false_strips_document_tools_from_general_toolset(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    events = []
    list(
        runner.stream(
            query="帮我生成ICD",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=_context(),
            document_flow=False,
            event_callback=events.append,
        )
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert names & DOCUMENT_TOOL_NAMES == set()
    assert names & RETRIEVAL_TOOL_NAMES != set()
    assert "文档生成流程助手" not in captured["system_prompt"]
    assert not events


def test_explicit_true_with_expired_context_emits_unavailable_event(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    events = []
    list(
        runner.stream(
            query="这个模板里有哪些字段？",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=_expired_context(),
            document_flow=True,
            event_callback=events.append,
        )
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert names & DOCUMENT_TOOL_NAMES == set()
    assert "文档生成流程助手" not in captured["system_prompt"]
    unavailable = [
        event
        for event in events
        if event.get("type") == "stage"
        and event.get("payload", {}).get("key") == "document_flow_unavailable"
    ]
    assert len(unavailable) == 1
    payload = unavailable[0]["payload"]
    assert payload["status"] == "error"
    assert payload["detail"] == "document_context has expired"


def test_explicit_true_with_valid_context_routes_to_document_flow(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    events = []
    list(
        runner.stream(
            query="继续",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=_context(),
            document_flow=True,
            event_callback=events.append,
        )
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert names and names <= DOCUMENT_TOOL_NAMES
    assert "文档生成流程助手" in captured["system_prompt"]
    routed = [
        event
        for event in events
        if event.get("type") == "stage"
        and event.get("payload", {}).get("key") == "document_flow_routed"
    ]
    assert len(routed) == 1
    assert routed[0]["payload"]["routed_by"] == "explicit"
    assert not [
        event
        for event in events
        if event.get("payload", {}).get("key") == "document_flow_unavailable"
    ]


def test_explicit_true_without_document_tools_emits_unavailable_event(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    events = []
    list(
        runner.stream(
            query="请根据模板生成 ICD 文档",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=_context(),
            document_flow=True,
            event_callback=events.append,
        )
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert names & DOCUMENT_TOOL_NAMES == set()
    assert names & RETRIEVAL_TOOL_NAMES != set()
    assert "文档生成流程助手" not in captured["system_prompt"]
    unavailable = [
        event
        for event in events
        if event.get("type") == "stage"
        and event.get("payload", {}).get("key") == "document_flow_unavailable"
    ]
    assert len(unavailable) == 1
    payload = unavailable[0]["payload"]
    assert payload["status"] == "error"
    assert payload["detail"] == "document authoring tools are unavailable"


def test_runner_card_sink_reshapes_document_card_event_to_channel_payload(monkeypatch):
    from src.agents import runner as runner_mod

    _install_fake_agent(monkeypatch)
    captured: dict = {}

    def fake_factory(rt, **kwargs):
        captured["event_sink"] = kwargs.get("event_sink")
        return []

    monkeypatch.setattr(runner_mod, "make_document_authoring_tools", fake_factory)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    events = []
    list(
        runner.stream(
            query="这个模板里有哪些字段？",
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=_context(),
            event_callback=events.append,
        )
    )

    card = {
        "kind": "work_order_created",
        "work_order_id": "work-order-1",
        "status": "queued",
        "next_actions": ["get_document_generation_status"],
        "kb_name": "kb_hw",
    }
    assert captured["event_sink"] is not None
    captured["event_sink"]({"type": "document_card", "card": card})
    assert events[-1] == {"type": "document_card", "payload": {"card": card}}


def test_durable_event_callback_forwards_routed_by_from_stage_payload():
    from src.api.routes.query import _make_event_callback

    stages = []
    others = []
    callback = _make_event_callback(
        lambda key, label, status, detail="", **extra: stages.append((key, label, status, detail, extra)),
        lambda etype, payload: others.append((etype, payload)),
    )

    callback(
        {
            "type": "stage",
            "payload": {
                "key": "document_flow_routed",
                "label": "文档生成流程",
                "status": "running",
                "routed_by": "explicit",
            },
        }
    )
    callback({"type": "thought", "payload": {"text": "不应持久化"}})
    callback({"type": "degraded", "payload": {"stage": "agent_loop", "reason": "x"}})

    assert stages == [
        ("document_flow_routed", "文档生成流程", "running", "", {"routed_by": "explicit"}),
    ]
    assert others == [("degraded", {"stage": "agent_loop", "reason": "x"})]


def test_decode_document_flow_round_trip():
    from src.api.routes.query import _decode_document_flow

    assert _decode_document_flow({"document_flow": "true"}) is True
    assert _decode_document_flow({"document_flow": "false"}) is False
    assert _decode_document_flow({"document_flow": "  FALSE  "}) is False
    assert _decode_document_flow({}) is None
    assert _decode_document_flow({"document_flow": "yes"}) is None
    assert _decode_document_flow({"document_flow": ""}) is None
    assert _decode_document_flow(None) is None
