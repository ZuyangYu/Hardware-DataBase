from __future__ import annotations

import json

from src.core.conversation_orchestrator import ConversationOrchestrator


def _plan(**kwargs):
    return ConversationOrchestrator().plan(**kwargs)


def test_chat_route_without_sources():
    plan = _plan(query="你好", has_kb=False, has_attachments=False)
    assert plan.route == "chat"
    assert plan.allowed_tools == ()


def test_retrieval_route_with_knowledge_base():
    plan = _plan(query="这个接口的引脚定义是什么？", has_kb=True)
    assert plan.route == "retrieval"
    assert "retrieval" in plan.allowed_tools


def test_attachment_analysis_gets_a_dedicated_backend_route():
    plan = _plan(query="分析这个附件的接口定义", has_kb=False, has_attachments=True)
    assert plan.route == "attachment_analysis"
    assert plan.allowed_tools == ("attachment",)


def test_comparison_is_retrieval():
    plan = _plan(query="对比这两份规范", has_kb=True, has_attachments=True)
    assert plan.route == "retrieval"
    assert plan.comparison_requested is True
    assert "attachment" in plan.allowed_tools


def test_export_is_retrieval_and_preserves_formats():
    plan = _plan(query="把刚才的结果导出成 PDF", has_kb=True)
    assert plan.route == "retrieval"
    assert plan.export_requested is True
    assert plan.requested_formats == ("pdf",)


def test_invalid_source_scope_falls_back_to_auto():
    plan = _plan(query="查询", has_kb=True, source_scope="not-a-scope")
    assert plan.source_scope == "auto"
    assert "invalid_source_scope_fallback" in plan.reason_codes


def test_plan_is_json_safe():
    plan = _plan(query="整理成文档", has_kb=True, has_attachments=True)
    payload = plan.to_dict()
    assert json.loads(json.dumps(payload))["route"] == plan.route
    assert isinstance(payload["allowed_tools"], list)
    assert isinstance(payload["source_scope"], str)
