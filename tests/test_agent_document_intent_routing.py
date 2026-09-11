"""聊天入口的文档生成流程确定性路由测试。

有模板上下文且明确要求生成文档的轮次必须进入文档流程（只装配文档工具 +
专用流程提示词），而不是交给通用问答 Agent 自由选工具。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from src.agents.runner import (
    MultiSourceAgentRunner,
    _direct_generation_answer,
    is_explicit_document_plan_confirmation_query,
    resolve_document_flow_route,
)
from src.attachments.models import AttachmentRef
from src.document_authoring.chat_context import build_document_context

DOCUMENT_TOOL_NAMES = {
    "generate_document_from_template",
    "get_document_template_analysis",
    "start_document_generation_session",
    "answer_clarification",
    "propose_document_plan",
    "confirm_document_plan",
    "confirm_generation_session",
    "create_document_work_order",
    "create_document_revision",
    "get_document_generation_status",
    "get_document_task_status",
    "resolve_icd_scope_exception",
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


def _template_free_context():
    return build_document_context(
        {
            "version": "v2",
            "knowledge_base_name": "kb_hw",
            "output_spec_id": "spec-1",
            "output_spec_version": 1,
        },
        ctx=_FakeCtx(),
    )


def _template_free_task_context():
    return build_document_context(
        {
            "version": "v2",
            "knowledge_base_name": "kb_hw",
            "task_id": "document-task-a",
        },
        ctx=_FakeCtx(),
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


def test_comparison_intent_never_routes_to_document_flow_even_with_template_context():
    routed, _ = resolve_document_flow_route(
        document_context=_context(),
        query="参考模板，比较知识库和附件中的 HSI 文档差异并整理成 PDF",
        has_document_tools=True,
    )

    assert routed is False


def test_route_requires_tools_fresh_context_and_query():
    routed, _ = resolve_document_flow_route(
        document_context=_context(), query="生成文档", has_document_tools=False
    )
    assert routed is False


def test_template_free_authoring_routes_with_v2_context():
    routed, by = resolve_document_flow_route(
        document_context=_template_free_context(),
        query="基于知识库创建 ICD 报告",
        has_document_tools=True,
    )
    assert routed is True
    assert by == "regex"
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
    assert routed_events[0]["payload"]["intent"] == "template_generation"
    assert routed_events[0]["payload"]["action"] == "generate"
    assert routed_events[0]["payload"]["target"] == "template"
    assert "template_targeted_command" in routed_events[0]["payload"]["reason_codes"]


def test_routed_turn_persists_backend_conversation_plan(monkeypatch):
    _install_fake_agent(monkeypatch)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    events = []
    list(
        runner.stream(
            query="请根据模板生成 ICD 文档",
            kb_name="kb_hw",
            history=[],
            thread_id="t-plan",
            document_context=_context(),
            event_callback=events.append,
        )
    )

    routed = [
        event["payload"]
        for event in events
        if event.get("type") == "stage"
        and event.get("payload", {}).get("key") == "document_flow_routed"
    ]
    assert len(routed) == 1
    assert routed[0]["route_authority"] == "backend"
    assert routed[0]["conversation_plan"]["route"] == "document_authoring"
    assert routed[0]["conversation_plan"]["schema_version"] == "v1"
    assert runner.get_last_retrieval_summary()["conversation_plan"]["route"] == "document_authoring"


def test_direct_generation_intent_submits_artifact_without_calling_model(monkeypatch):
    from src.agents import runner as runner_mod

    class Pipeline:
        def create_document_generation_session(self, *_args, **_kwargs):
            return object()

        def prepare_knowledge_base_document_generation(self, *_args, **_kwargs):
            return {"stage": "ready", "work_order_id": "wo-direct"}

    class DirectTool:
        name = "generate_document_from_template"

        def invoke(self, _args):
            return json.dumps({
                "status": "succeeded",
                "message": "模板填充任务已提交，完成后可下载最终文档",
                "work_order_id": "wo-direct",
                "job_id": "job-direct",
                "data": {"status": "queued", "stage": "ready", "target_format": "xlsx"},
            }, ensure_ascii=False)

    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "make_document_authoring_tools", lambda *_args, **_kwargs: [DirectTool()])
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("direct document generation must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )
    events = []
    answer = "".join(runner.stream(
        query="参考模板，根据知识库生成新的 ICD 文档",
        kb_name="kb_hw",
        history=[],
        thread_id="t1",
        document_context=_context(),
        event_callback=events.append,
    ))

    assert "真实的文档填充任务" in answer
    assert "wo-direct" in answer
    assert runner.get_last_retrieval_summary()["retriever_type"] == "document_authoring"
    assert any(
        event.get("payload", {}).get("key") == "document_generation_submission"
        for event in events
    )


def test_explicit_plan_confirmation_is_narrow():
    assert is_explicit_document_plan_confirmation_query("确认")
    assert is_explicit_document_plan_confirmation_query("确认生成。")
    assert is_explicit_document_plan_confirmation_query("同意按此方案生成")
    assert not is_explicit_document_plan_confirmation_query("请确认这个字段是什么意思")
    assert not is_explicit_document_plan_confirmation_query("不要确认生成")


@pytest.mark.parametrize(
    "text",
    ["可以", "好的", "没问题", "开始吧", "就这么做", "按这个方案执行", "继续生成"],
)
def test_plan_confirmation_accepts_natural_standalone_affirmations(text):
    assert is_explicit_document_plan_confirmation_query(text)


@pytest.mark.parametrize(
    "text",
    ["可以吗？", "如果没问题就生成", "好的，但先修改格式", "暂不生成", "为什么要确认"],
)
def test_plan_confirmation_rejects_questions_conditions_negation_and_modification(text):
    assert not is_explicit_document_plan_confirmation_query(text)


def test_explicit_confirmation_uses_latest_pending_plan_without_calling_model(monkeypatch):
    from src.agents import runner as runner_mod

    invoked = {}

    class Pipeline:
        def get_current_chat_document_task_projection(self, _ctx, **kwargs):
            assert kwargs == {
                "knowledge_base_name": "kb_hw",
                "conversation_id": "85",
            }
            return {
                "task_id": "task-pending",
                "status": "awaiting_plan_confirmation",
                "generation_session_id": "generation-session-pending",
                "planning_state": {
                    "output_spec_hash": "sha256:spec",
                    "plan_hash": "sha256:plan",
                },
            }

        def list_document_task_projections(self, *_args, **_kwargs):
            pytest.fail("confirmation must use the durable current-task pointer")

    class ConfirmTool:
        name = "confirm_document_plan"

        def invoke(self, args):
            invoked.update(args)
            return json.dumps({
                "status": "succeeded",
                "message": "document plan confirmed",
                "task_id": "task-pending",
                "work_order_id": None,
                "job_id": None,
                "data": {"status": "pending"},
            })

    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "make_document_authoring_tools", lambda *_args, **_kwargs: [ConfirmTool()])
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("an explicit plan confirmation must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )

    answer = "".join(runner.stream(
        query="确认",
        kb_name="kb_hw",
        history=[],
        ctx=_FakeCtx(),
        thread_id="85",
        document_context=_context(),
        document_flow=True,
    ))

    assert invoked["session_id"] == "generation-session-pending"
    assert invoked["expected_output_spec_hash"] == "sha256:spec"
    assert invoked["expected_plan_hash"] == "sha256:plan"
    assert invoked["client_request_id"].startswith("chat-plan-confirm:85:")
    assert "计划已确认" in answer
    assert "Markdown" not in answer


def test_explicit_confirmation_does_not_confirm_an_older_plan_after_newer_task_advanced(monkeypatch):
    from src.agents import runner as runner_mod

    class Pipeline:
        def get_current_chat_document_task_projection(self, _ctx, **_kwargs):
            return {
                "task_id": "task-newer",
                "status": "needs_review",
                "work_order_id": "wo-newer",
                "next_actions": ["review_document"],
            }

        def list_document_task_projections(self, *_args, **_kwargs):
            pytest.fail("an older task list must not influence current-task confirmation")

    class ConfirmTool:
        name = "confirm_document_plan"

        def invoke(self, _args):
            pytest.fail("an older pending plan must not be confirmed over the latest task")

    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "make_document_authoring_tools", lambda *_args, **_kwargs: [ConfirmTool()])
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("an already-advanced confirmation must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )

    answer = "".join(runner.stream(
        query="确认生成",
        kb_name="kb_hw",
        history=[],
        ctx=_FakeCtx(),
        thread_id="85",
        document_context=_context(),
        document_flow=True,
    ))

    assert "已经确认" in answer
    assert "待审核" in answer
    assert "wo-newer" in answer


def test_direct_generation_answer_shows_pending_intake_question():
    answer = _direct_generation_answer({
        "status": "waiting_human",
        "message": "文档生成前需要补充需求",
        "generation_session_id": "generation-session-a",
        "data": {
            "status": "needs_clarification",
            "question_id": "deliverables",
            "content": "希望交付什么格式？",
            "options": ["Markdown", "Word", "Excel"],
        },
    })

    assert "希望交付什么格式？" in answer
    assert "Markdown、Word、Excel" in answer
    assert "文档工作台" not in answer


def test_direct_generation_answer_rejection_names_the_real_reason():
    answer = _direct_generation_answer({
        "status": "rejected",
        "message": "generation session is not awaiting a plan proposal",
        "error_code": "plan_proposal_rejected",
    })

    assert "generation session is not awaiting a plan proposal" in answer
    assert "plan_proposal_rejected" in answer
    assert "修正模板或权限" not in answer


def test_direct_generation_answer_keeps_plan_confirmation_in_conversation():
    answer = _direct_generation_answer({
        "status": "waiting_human",
        "message": "已生成文档计划提案；请确认将要生成的内容后再执行生成。",
        "data": {"status": "awaiting_plan_confirmation"},
    })

    assert "确认生成" in answer
    assert "文档工作台" not in answer


def test_confirmed_task_answer_keeps_missing_data_in_conversation():
    from src.agents.runner import _confirmed_document_task_answer

    answer = _confirmed_document_task_answer({
        "status": "needs_clarification",
        "work_order_id": "wo-blocked",
        "error_code": "plan_release_blocked",
        "error_message": "21 个字段尚未完成，无法发布",
        "clarification_state": {
            "pending_question": {
                "content": "知识库中暂未找到以下必填字段的可靠资料：pin-11。请选择处理方式：",
                "options": ["标记为未提供，继续生成", "补充说明", "暂停等待资料"],
            },
        },
    })

    assert "请直接在对话中回答" in answer
    assert "标记为未提供，继续生成" in answer
    assert "文档工作台" not in answer


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
    assert DOCUMENT_TOOL_NAMES - {"create_document_work_order"} <= names
    assert "create_document_work_order" not in names
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


def test_document_flow_mounts_structured_attachment_tools(monkeypatch):
    captured = _install_fake_agent(monkeypatch)
    monkeypatch.setattr("src.attachments.service.AttachmentService", lambda: Mock())
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    ref = AttachmentRef(
        attachment_id="att-1",
        asset_id="asset-1",
        session_id=42,
        filename="board.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extension=".xlsx",
        size_bytes=100,
        sha256="hash",
        parse_status="ready",
    )

    list(
        runner.stream(
            query="请根据模板生成报告",
            kb_name="kb_hw",
            history=[],
            thread_id="42",
            document_context=_context(),
            document_flow=True,
            attachments=[ref],
            source_scope="attachment_only",
        )
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert {"attachment_table_query", "attachment_circuit_search"} <= names


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


# ---------------------------------------------------------------------------
# Phase 1 Task 11: v2 tool mounting and generic document-flow prompt
# ---------------------------------------------------------------------------

_V2_INTAKE_TOOL_NAMES = {
    "start_document_generation_session",
    "answer_clarification",
    "propose_document_plan",
    "confirm_document_plan",
}


def _stream_once(monkeypatch, *, query, context, v2=True):

    from src.agents import runner as runner_mod

    captured = _install_fake_agent(monkeypatch)
    if v2:
        monkeypatch.setattr(runner_mod.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    else:
        monkeypatch.setattr(runner_mod.settings, "DOCUMENT_PLANNING_V2_ENABLED", False)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
        document_job_store=Mock(),
    )
    deltas = list(
        runner.stream(
            query=query,
            kb_name="kb_hw",
            history=[],
            thread_id="t1",
            document_context=context,
        )
    )
    assert deltas
    return captured


def test_template_free_v2_context_mounts_intake_tools(monkeypatch):
    captured = _stream_once(
        monkeypatch,
        query="基于知识库生成一份评审报告",
        context=_template_free_context(),
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert _V2_INTAKE_TOOL_NAMES <= names
    assert "create_document_work_order" not in names
    assert "generate_document_from_template" not in names
    assert "get_document_template_analysis" not in names
    prompt = str(captured["system_prompt"])
    assert "confirm_document_plan" in prompt
    assert "propose_document_plan" in prompt


def test_template_free_v2_context_mounts_scope_resolution_when_task_exists(monkeypatch):
    captured = _stream_once(
        monkeypatch,
        query="继续生成",
        context=_template_free_task_context(),
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert "resolve_icd_scope_exception" in names


def test_template_free_v2_context_without_task_omits_scope_resolution(monkeypatch):
    captured = _stream_once(
        monkeypatch,
        query="继续生成",
        context=_template_free_context(),
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert "resolve_icd_scope_exception" not in names


def test_v2_flag_excludes_work_order_tool_for_template_contexts(monkeypatch):
    captured = _stream_once(
        monkeypatch,
        query="请根据模板生成 ICD 文档",
        context=_context(),
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert "create_document_work_order" not in names
    assert _V2_INTAKE_TOOL_NAMES <= names


def test_v2_flag_off_keeps_legacy_tool_surface(monkeypatch):
    captured = _stream_once(
        monkeypatch,
        query="请根据模板生成 ICD 文档",
        context=_context(),
        v2=False,
    )

    names = {_tool_name(tool) for tool in captured["tools"]}
    assert "create_document_work_order" in names
    assert "propose_document_plan" not in names
    assert "confirm_document_plan" not in names


def test_pending_clarification_matcher_accepts_canonical_options_and_delegation():
    from src.agents.runner import match_pending_clarification_answer

    recommendations = {
        "question_id": "recommendations",
        "options": ["采用推荐方案", "逐项设置"],
    }
    assert match_pending_clarification_answer("采用推荐方案", recommendations) == "采用推荐方案"
    assert match_pending_clarification_answer("采用推荐方案吧", recommendations) == "采用推荐方案"
    assert match_pending_clarification_answer("采用推荐方案是什么意思？", recommendations) is None
    assert match_pending_clarification_answer("不要采用推荐方案", recommendations) is None

    target_identity = {"question_id": "target_identity", "options": []}
    assert match_pending_clarification_answer("按照知识库中的模块和连接来确定", target_identity) == "按照知识库中的模块和连接来确定"
    assert match_pending_clarification_answer("X1900", target_identity) == "X1900"
    assert match_pending_clarification_answer("知识库里有哪些模块？", target_identity) is None
    assert match_pending_clarification_answer("帮我重新生成一份 ICD", target_identity) is None


def test_pending_clarification_answer_is_submitted_without_document_context(monkeypatch):
    """The session pointer is server-owned; a lost browser context must not
    turn the answer into a retrieval loop that never reaches the intake."""
    from types import SimpleNamespace

    from src.agents import runner as runner_mod

    answered: dict = {}

    class Pipeline:
        def get_current_chat_document_task_projection(self, _ctx, **_kwargs):
            pending = (
                {
                    "question_id": "recommendations",
                    "content": "是否采用推荐的生成与审核策略？",
                    "options": ["采用推荐方案", "逐项设置"],
                    "reason": "可一次确认安全默认值。",
                }
                if answered
                else {
                    "question_id": "target_identity",
                    "content": "这份 ICD 对应哪个硬件/总成？",
                    "options": [],
                    "reason": "示例模板可能包含旧产品数据。",
                }
            )
            return {
                "task_id": "task-1",
                "status": "needs_clarification",
                "clarification_state": {
                    "session_id": "generation-session-pending",
                    "status": "needs_clarification",
                    "pending_question": pending,
                },
            }

        def answer_document_generation_session(self, _ctx, session_id, **kwargs):
            answered.update({"session_id": session_id, **kwargs})
            return SimpleNamespace(session_id=session_id, status="needs_clarification")

    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("a pending clarification answer must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

    answer = "".join(runner.stream(
        query="按照知识库中的模块和连接来确定",
        kb_name="kb_hw",
        history=[],
        ctx=Ctx(),
        thread_id="85",
        document_context=None,
    ))

    assert answered["session_id"] == "generation-session-pending"
    assert answered["question_id"] == "target_identity"
    assert answered["answer"] == "按照知识库中的模块和连接来确定"
    assert "已记录" in answer
    assert "采用推荐方案" in answer


def test_clarification_answer_reports_compiled_plan_proposal():
    """The final clarification turn must surface the confirmation next step
    instead of promising that the system will continue silently."""
    from src.agents.runner import _direct_clarification_answer

    text = _direct_clarification_answer(
        {
            "status": "succeeded",
            "next_actions": ["confirm_document_plan"],
            "data": {
                "pending_question": None,
                "proposal": {
                    "status": "succeeded",
                    "next_actions": ["confirm_document_plan"],
                    "data": {"executable": True, "outline_count": 4, "table_count": 1},
                },
            },
        },
        {"question_id": "template_mapping_confirmation"},
        "允许替换模板示例数据",
    )

    assert "已记录「template_mapping_confirmation」的回答：允许替换模板示例数据" in text
    assert "已生成文档计划提案" in text
    assert "确认生成" in text


def test_clarification_answer_reports_non_executable_plan_proposal():
    """A compiled but non-executable proposal must not claim it is ready."""
    from src.agents.runner import _direct_clarification_answer

    text = _direct_clarification_answer(
        {
            "status": "succeeded",
            "next_actions": ["answer_clarification"],
            "data": {
                "pending_question": None,
                "proposal": {
                    "status": "succeeded",
                    "next_actions": ["answer_clarification"],
                    "data": {"executable": False},
                },
            },
        },
        {"question_id": "template_mapping_confirmation"},
        "允许替换模板示例数据",
    )

    assert "计划提案已生成，但当前仍有未满足的执行条件" in text
    assert "确认生成" not in text


def test_tool_payload_coercion_rejects_non_object_json():
    """Non-object tool payloads must degrade to an empty dict, never raise."""
    from src.agents.runner import _coerce_tool_payload

    assert _coerce_tool_payload("[1, 2]") == {}
    assert _coerce_tool_payload("null") == {}
    assert _coerce_tool_payload(None) == {}
    assert _coerce_tool_payload({"status": "succeeded"}) == {"status": "succeeded"}


def test_final_clarification_answer_auto_proposes_plan(monkeypatch):
    """After the last clarification the server continues deterministically
    into the plan proposal instead of leaving the conversation idle."""
    from types import SimpleNamespace

    from src.agents import runner as runner_mod

    class Pipeline:
        def __init__(self):
            self.answered: dict = {}
            self.proposal_calls: list = []

        def get_current_chat_document_task_projection(self, _ctx, **_kwargs):
            pending = (
                {
                    "question_id": "template_mapping_confirmation",
                    "content": "是否允许替换模板示例数据？",
                    "options": ["允许替换模板示例数据", "保留模板示例数据"],
                    "reason": "template_mapping_consent",
                }
                if not self.answered
                else None
            )
            return {
                "task_id": "task-plan",
                "status": "draft" if self.answered else "needs_clarification",
                "generation_session_id": "generation-session-plan",
                "next_actions": (
                    ["answer_clarification", "propose_document_plan"]
                    if self.answered
                    else []
                ),
                "clarification_state": {
                    "session_id": "generation-session-plan",
                    "status": "awaiting_plan" if self.answered else "needs_clarification",
                    "pending_question": pending,
                },
            }

        def answer_document_generation_session(self, _ctx, session_id, **kwargs):
            self.answered.update({"session_id": session_id, **kwargs})
            return SimpleNamespace(session_id=session_id, status="awaiting_plan")

        def get_document_generation_session(self, _ctx, session_id):
            return SimpleNamespace(
                session_id=session_id,
                status="awaiting_plan",
                output_spec_version=5,
            )

        def create_document_plan_proposal(self, _ctx, session_id, **kwargs):
            self.proposal_calls.append({"session_id": session_id, **kwargs})
            return {
                "executable": True,
                "task_id": "task-plan",
                "plan_hash": "plan-hash",
                "output_spec_hash": "spec-hash",
                "outline_count": 4,
                "table_count": 1,
            }

    pipeline = Pipeline()
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("the final clarification answer must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=pipeline,
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

    events: list[dict] = []
    answer = "".join(runner.stream(
        query="允许替换模板示例数据",
        kb_name="kb_hw",
        history=[],
        ctx=Ctx(),
        thread_id="99",
        document_context=_context(),
        event_callback=events.append,
    ))

    assert pipeline.answered["session_id"] == "generation-session-plan"
    assert pipeline.answered["question_id"] == "template_mapping_confirmation"
    assert pipeline.answered["answer"] == "允许替换模板示例数据"
    assert len(pipeline.proposal_calls) == 1
    assert pipeline.proposal_calls[0]["session_id"] == "generation-session-plan"
    assert pipeline.proposal_calls[0]["expected_output_spec_version"] == 5
    assert "已生成文档计划提案" in answer
    assert "确认生成" in answer
    cards = [
        event["payload"]["card"]
        for event in events
        if event.get("type") == "document_card"
        and isinstance(event.get("payload"), dict)
        and isinstance(event["payload"].get("card"), dict)
    ]
    assert [card.get("kind") for card in cards] == ["output_spec_confirmation"]
    assert cards[0]["generation_session_id"] == "generation-session-plan"


def test_final_clarification_without_mounted_context_reports_plan_next_step(monkeypatch):
    """A lost browser context must still project the actionable next step."""
    from types import SimpleNamespace

    from src.agents import runner as runner_mod

    class Pipeline:
        def __init__(self):
            self.answered: dict = {}

        def get_current_chat_document_task_projection(self, _ctx, **_kwargs):
            pending = (
                {
                    "question_id": "template_mapping_confirmation",
                    "content": "是否允许替换模板示例数据？",
                    "options": ["允许替换模板示例数据", "保留模板示例数据"],
                    "reason": "template_mapping_consent",
                }
                if not self.answered
                else None
            )
            return {
                "task_id": "task-plan",
                "status": "draft" if self.answered else "needs_clarification",
                "generation_session_id": "generation-session-plan",
                "next_actions": (
                    ["answer_clarification", "propose_document_plan"]
                    if self.answered
                    else []
                ),
                "clarification_state": {
                    "session_id": "generation-session-plan",
                    "status": "awaiting_plan" if self.answered else "needs_clarification",
                    "pending_question": pending,
                },
            }

        def answer_document_generation_session(self, _ctx, session_id, **kwargs):
            self.answered.update({"session_id": session_id, **kwargs})
            return SimpleNamespace(session_id=session_id, status="awaiting_plan")

        def create_document_plan_proposal(self, *_args, **_kwargs):
            pytest.fail("no mounted context must not compile a plan proposal")

    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("a pending clarification answer must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

    answer = "".join(runner.stream(
        query="允许替换模板示例数据",
        kb_name="kb_hw",
        history=[],
        ctx=Ctx(),
        thread_id="99",
        document_context=None,
    ))

    assert "已记录" in answer
    assert "需求信息已齐备，可以生成文档计划提案。" in answer


def test_projection_failure_after_answer_keeps_answer_recorded(monkeypatch):
    """The durable answer must not be reported as rejected just because the
    optional post-answer projection read failed."""
    from types import SimpleNamespace

    from src.agents import runner as runner_mod

    class Pipeline:
        def __init__(self):
            self.answered: dict = {}

        def get_current_chat_document_task_projection(self, _ctx, **_kwargs):
            if self.answered:
                raise RuntimeError("projection store offline")
            return {
                "task_id": "task-plan",
                "status": "needs_clarification",
                "generation_session_id": "generation-session-plan",
                "next_actions": [],
                "clarification_state": {
                    "session_id": "generation-session-plan",
                    "status": "needs_clarification",
                    "pending_question": {
                        "question_id": "template_mapping_confirmation",
                        "content": "是否允许替换模板示例数据？",
                        "options": ["允许替换模板示例数据", "保留模板示例数据"],
                        "reason": "template_mapping_consent",
                    },
                },
            }

        def answer_document_generation_session(self, _ctx, session_id, **kwargs):
            self.answered.update({"session_id": session_id, **kwargs})
            return SimpleNamespace(session_id=session_id, status="awaiting_plan")

    pipeline = Pipeline()
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("a pending clarification answer must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=pipeline,
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

    answer = "".join(runner.stream(
        query="允许替换模板示例数据",
        kb_name="kb_hw",
        history=[],
        ctx=Ctx(),
        thread_id="99",
        document_context=None,
    ))

    assert pipeline.answered["session_id"] == "generation-session-plan"
    assert "已记录" in answer
    assert "澄清回答未能提交" not in answer


def test_confirmed_task_answer_explains_blocking_scope_exception():
    """A blocking ICD scope gate must be explained with its operator action."""
    from src.agents.runner import _confirmed_document_task_answer

    text = _confirmed_document_task_answer({
        "status": "needs_review",
        "work_order_id": "wo-89b3",
        "next_actions": ["submit_icd_scope_resolution", "open_document_workbench"],
        "pending_review": {
            "review_id": "review-a",
            "review_kind": "icd_scope",
            "status": "pending",
            "scope_review": {
                "status": "pending",
                "pending_count": 1,
                "blocking": True,
                "exceptions": [{
                    "kind": "connector_mapping_missing",
                    "refdes": "X302",
                    "recommended_action": "check_edf_mapping",
                    "user_instruction": (
                        "已确定接插件 X302，但当前冻结来源中未找到其 EDF 管脚映射。"
                        "请检查已上传 EDF 是否包含该位号并重新解析后再生成。"
                    ),
                }],
            },
        },
    })

    assert "ICD" in text
    assert "X302" in text
    assert "EDF" in text
    assert "请确认实际目标接插件" in text
    assert "重新发起生成" in text
    assert "不会写入候选文件" in text


def test_confirmed_task_answer_offers_chat_resolution_for_open_scope_exceptions():
    """A non-blocking scope exception can be resolved by a chat instruction."""
    from src.agents.runner import _confirmed_document_task_answer

    text = _confirmed_document_task_answer({
        "status": "needs_review",
        "work_order_id": "wo-1",
        "next_actions": ["submit_icd_scope_resolution", "open_document_workbench"],
        "pending_review": {
            "review_kind": "icd_scope",
            "status": "pending",
            "scope_review": {
                "status": "pending",
                "pending_count": 1,
                "blocking": False,
                "exceptions": [{
                    "kind": "connector_scope_ambiguous",
                    "refdes": "X301",
                    "user_instruction": "请确认使用 X301 还是 X302。",
                }],
            },
        },
    })

    assert "X301" in text
    assert "包含" in text and "排除" in text
    assert "请按实际情况确认" in text
    assert "待处理：X301" in text


def test_edf_scope_replacement_matcher_requires_an_explicit_edf_instruction():
    from src.agents.runner import _looks_like_edf_scope_replacement

    assert _looks_like_edf_scope_replacement("实际根据EDF内容对X302等位号进行替换")
    assert _looks_like_edf_scope_replacement("按EDF实际位号生成")
    assert _looks_like_edf_scope_replacement("用 EDF 实际位号替换模板示例")
    assert not _looks_like_edf_scope_replacement("EDF 是什么文件？")
    assert not _looks_like_edf_scope_replacement("继续生成")


def test_edf_scope_replacement_is_submitted_without_the_model(monkeypatch):
    """A blocking template-example refdes is replaced deterministically."""
    from types import SimpleNamespace

    from src.agents import runner as runner_mod

    class Pipeline:
        def __init__(self):
            self.rebuilt: dict = {}

        def get_current_chat_document_task_projection(self, _ctx, **_kwargs):
            return {
                "task_id": "task-edf",
                "status": "needs_review",
                "work_order_id": "wo-edf",
                "generation_session_id": "generation-session-edf",
                "next_actions": ["submit_icd_scope_resolution", "open_document_workbench"],
                "pending_review": {
                    "review_kind": "icd_scope",
                    "status": "pending",
                    "scope_review": {
                        "status": "pending",
                        "pending_count": 1,
                        "blocking": True,
                        "exceptions": [{
                            "kind": "connector_mapping_missing",
                            "refdes": "X302",
                            "suggested_refdes": ["X1900", "X1902"],
                            "user_instruction": "模板示例位号",
                        }],
                    },
                },
            }

        def get_icd_scope_review(self, _ctx, _order_id):
            return SimpleNamespace(
                status="pending",
                exceptions=[SimpleNamespace(
                    exception_id="exception-a",
                    kind="connector_mapping_missing",
                    refdes="X302",
                    user_instruction="模板示例位号",
                    suggested_refdes=["X1900", "X1902"],
                )],
            )

        def rebuild_icd_scope_from_edf(self, _ctx, order_id, *, comment):
            self.rebuilt.update({"order_id": order_id, "comment": comment})
            return {"status": "resumed", "run_id": "run-edf"}

    pipeline = Pipeline()
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("an explicit EDF replacement must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=pipeline,
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

    events: list[dict] = []
    answer = "".join(runner.stream(
        query="实际根据EDF内容对X302等位号进行替换",
        kb_name="kb_hw",
        history=[],
        ctx=Ctx(),
        thread_id="100",
        document_context=_context(),
        event_callback=events.append,
    ))

    assert pipeline.rebuilt["order_id"] == "wo-edf"
    assert pipeline.rebuilt["comment"] == "实际根据EDF内容对X302等位号进行替换"
    assert "已按冻结 EDF 的实际位号替换模板示例位号" in answer


def test_lost_document_context_is_recovered_from_the_pending_session(monkeypatch):
    """A browser that lost the mounted context must still get the intake tool."""
    from types import SimpleNamespace

    from src.document_authoring.chat_context import DocumentAuthoringContext

    class Pipeline:
        def find_reusable_document_generation_session(self, _ctx, **kwargs):
            assert kwargs["knowledge_base_name"] == "kb_hw"
            return SimpleNamespace(
                session_id="generation-session-pending",
                document_task_id="task-pending",
                knowledge_base_name="kb_hw",
                template_version_id="template-a",
            )

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

    recovered = runner._recover_pending_document_context(
        ctx=Ctx(),
        conversation_id="97",
        kb_name="kb_hw",
    )

    assert isinstance(recovered, DocumentAuthoringContext)
    assert recovered.generation_session_id == "generation-session-pending"
    assert recovered.task_id == "task-pending"
    assert recovered.template_version_id == "template-a"
    assert recovered.knowledge_base_name == "kb_hw"


def test_context_recovery_skips_foreign_knowledge_bases():
    from types import SimpleNamespace


    class Pipeline:
        def find_reusable_document_generation_session(self, _ctx, **_kwargs):
            return SimpleNamespace(
                session_id="generation-session-pending",
                document_task_id=None,
                knowledge_base_name="other-kb",
                template_version_id=None,
            )

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

        def has_kb_permission(self, kb_name, permission):
            return kb_name != "other-kb"

    recovered = runner._recover_pending_document_context(
        ctx=Ctx(),
        conversation_id="97",
        kb_name="kb_hw",
    )

    assert recovered is None


def test_fill_command_reports_confirmed_task_without_forking_a_new_plan(monkeypatch):
    """After plan confirmation a fill/Excel request reports the live task."""
    from src.agents import runner as runner_mod

    class Pipeline:
        def get_current_chat_document_task_projection(self, *_args, **_kwargs):
            return {
                "status": "running",
                "work_order_id": "wo-live",
                "pending_review": None,
                "clarification_state": None,
            }

        def create_document_generation_session(self, *_args, **_kwargs):
            raise AssertionError("must not fork a new intake session")

    class DirectTool:
        name = "generate_document_from_template"

        def invoke(self, _args):
            raise AssertionError("must not submit a second template request")

    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(
        runner_mod, "make_document_authoring_tools", lambda *_args, **_kwargs: [DirectTool()],
    )
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("a status answer must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=Pipeline(),
        document_job_store=Mock(),
    )
    answer = "".join(runner.stream(
        query="根据上述内容帮我填充上传的模板文件，生成本项目新的icd的excel文件",
        kb_name="kb_hw",
        history=[],
        ctx=_FakeCtx(),
        thread_id="t1",
        document_context=_context(),
        event_callback=[].append,
    ))

    assert "该文档计划已经确认" in answer
    assert "生成中" in answer
    assert "wo-live" in answer


def test_blocking_scope_gate_text_offers_plain_confirmation():
    from src.agents.runner import _confirmed_document_task_answer

    text = _confirmed_document_task_answer({
        "status": "blocked",
        "work_order_id": "wo-edf",
        "pending_review": {
            "review_kind": "icd_scope",
            "status": "pending",
            "scope_review": {
                "status": "pending",
                "pending_count": 1,
                "blocking": True,
                "exceptions": [{
                    "kind": "connector_mapping_missing",
                    "refdes": "X302",
                    "suggested_refdes": ["X1900", "X1902"],
                    "user_instruction": "模板示例位号",
                }],
            },
        },
    })

    assert "请直接回复“确认”" in text
    assert "按 EDF 实际位号生成" in text
    assert "直接要求填充/继续生成也可以" in text


def test_blocking_scope_gate_accepts_a_plain_confirmation(monkeypatch):
    """'确认' alone resolves a blocking EDF scope gate with suggested refdes."""
    from types import SimpleNamespace

    from src.agents import runner as runner_mod

    projection = {
        "task_id": "task-edf",
        "status": "blocked",
        "work_order_id": "wo-edf",
        "generation_session_id": "generation-session-edf",
        "next_actions": ["submit_icd_scope_resolution"],
        "pending_review": {
            "review_kind": "icd_scope",
            "status": "pending",
            "scope_review": {
                "status": "pending",
                "pending_count": 1,
                "blocking": True,
                "exceptions": [{
                    "kind": "connector_mapping_missing",
                    "refdes": "X302",
                    "suggested_refdes": ["X1900", "X1902"],
                    "user_instruction": "模板示例位号",
                }],
            },
        },
    }

    class Pipeline:
        def __init__(self):
            self.rebuilt: dict = {}

        def get_current_chat_document_task_projection(self, _ctx, **_kwargs):
            return dict(projection)

        def get_icd_scope_review(self, _ctx, _order_id):
            return SimpleNamespace(
                status="pending",
                exceptions=[SimpleNamespace(
                    exception_id="exception-a",
                    kind="connector_mapping_missing",
                    refdes="X302",
                    user_instruction="模板示例位号",
                    suggested_refdes=["X1900", "X1902"],
                )],
            )

        def rebuild_icd_scope_from_edf(self, _ctx, order_id, *, comment):
            self.rebuilt.update({"order_id": order_id, "comment": comment})
            return {"status": "resumed", "run_id": "run-edf"}

    pipeline = Pipeline()
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    monkeypatch.setattr(
        runner_mod,
        "create_chat_model",
        lambda: pytest.fail("a plain confirmation must not call the model"),
    )
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=pipeline,
        document_job_store=Mock(),
    )

    class Ctx(_FakeCtx):
        metadata: dict = {}

    answer = "".join(runner.stream(
        query="确认",
        kb_name="kb_hw",
        history=[],
        ctx=Ctx(),
        thread_id="101",
        document_context=_context(),
    ))

    assert pipeline.rebuilt["order_id"] == "wo-edf"
    assert "已按冻结 EDF 的实际位号替换模板示例位号" in answer
