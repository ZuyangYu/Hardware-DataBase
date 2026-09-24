from __future__ import annotations

from types import SimpleNamespace

from src.core.conversation_orchestrator import (
    ConversationOrchestrator,
    ConversationPlan,
)


def _document_context():
    return SimpleNamespace(expired=False)


def test_backend_plan_is_the_final_document_authoring_route():
    plan = ConversationOrchestrator().plan(
        query="继续生成",
        document_context=_document_context(),
        has_document_tools=True,
        explicit_flow=True,
        has_attachments=False,
        has_kb=True,
    )

    assert isinstance(plan, ConversationPlan)
    assert plan.schema_version == "v1"
    assert plan.route == "document_authoring"
    assert plan.document_flow is True
    assert plan.authority == "backend"
    assert plan.routed_by == "explicit"


def test_comparison_precedes_template_generation_in_backend_plan():
    plan = ConversationOrchestrator().plan(
        query="参考模板比较附件和知识库的差异并整理成 PDF",
        document_context=_document_context(),
        has_document_tools=True,
        has_attachments=True,
        has_kb=True,
    )

    assert plan.route == "retrieval"
    assert plan.intent == "compare"
    assert plan.comparison_requested is True
    assert plan.document_flow is False


def test_missing_document_context_cannot_be_promoted_by_a_frontend_hint():
    plan = ConversationOrchestrator().plan(
        query="请生成 ICD 文档",
        document_context=None,
        has_document_tools=True,
        explicit_flow=True,
        has_attachments=True,
        has_kb=True,
    )

    assert plan.route == "retrieval"
    assert plan.document_flow is False
    assert "document_context_missing" in plan.reason_codes


def test_attachment_analysis_gets_a_dedicated_backend_route():
    plan = ConversationOrchestrator().plan(
        query="分析这个附件里的接口定义",
        document_context=None,
        has_document_tools=False,
        has_attachments=True,
        has_kb=True,
    )

    assert plan.route == "attachment_analysis"
    assert plan.intent == "attachment_qa"
    assert plan.authority == "backend"


def test_plan_is_json_safe_and_preserves_source_scope():
    plan = ConversationOrchestrator().plan(
        query="生成文档",
        document_context=_document_context(),
        has_document_tools=True,
        explicit_flow=True,
        source_scope="attachment_and_knowledge_base",
        has_attachments=True,
        has_kb=True,
    )

    payload = plan.to_dict()
    assert payload["route"] == "document_authoring"
    assert payload["source_scope"] == "attachment_and_knowledge_base"
    assert isinstance(payload["reason_codes"], list)


def test_v2_context_can_route_template_free_authoring_with_authorized_source():
    context = {
        "version": "v2",
        "knowledge_base_name": "kb_hw",
        "owner_user_id": "user-1",
        "tenant_id": "tenant-1",
        "expired": False,
        "output_spec_id": "spec-1",
        "output_spec_version": 1,
    }
    plan = ConversationOrchestrator().plan(
        query="基于知识库创建 ICD 报告",
        document_context=context,
        has_document_tools=True,
        has_kb=True,
    )

    assert plan.route == "document_authoring"
    assert plan.intent == "document_authoring"
    assert plan.template_required is False
    assert "document_authoring" in plan.allowed_tools


def test_ambiguous_authoring_route_is_clarification_only():
    plan = ConversationOrchestrator().plan(
        query="整理成文档",
        document_context=_document_context(),
        has_document_tools=True,
        has_kb=True,
    )

    assert plan.intent == "document_authoring"
    assert plan.action == "clarify"
    assert plan.reason_codes == ("ambiguous_document_request",)
    assert plan.route == "document_authoring"
