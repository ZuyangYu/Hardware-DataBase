"""Canonical server-side intent planning regressions."""

from __future__ import annotations

import pytest

from src.core.intent import IntentPlan, classify_intent


def test_comparison_has_priority_over_template_generation():
    plan = classify_intent(
        "参考模板，比较知识库和附件中的 HSI 文档差异并整理成 PDF",
        has_template_context=True,
        has_attachments=True,
    )

    assert isinstance(plan, IntentPlan)
    assert plan.intent == "compare"
    assert plan.action == "compare"
    assert plan.template_required is False
    assert plan.requested_formats == ("pdf",)
    assert "comparison_requested" in plan.reason_codes


def test_template_generation_requires_document_action_and_target():
    plan = classify_intent(
        "参考模板，根据知识库生成新的 ICD 文档",
        has_template_context=True,
    )

    assert plan.intent == "template_generation"
    assert plan.action == "generate"
    assert plan.target == "template"
    assert plan.template_required is True


def test_recommended_followup_uses_existing_template_context():
    plan = classify_intent("按照推荐来进行", has_template_context=True)

    assert plan.intent == "template_generation"
    assert plan.reason_codes == ("recommended_followup",)


def test_recommendation_without_context_is_not_a_template_command():
    plan = classify_intent("按照推荐来进行")

    assert plan.intent != "template_generation"
    assert plan.template_required is False


def test_attachment_question_is_not_export_or_template_generation():
    plan = classify_intent("这个 PDF 里有哪些接口？", has_attachments=True)

    assert plan.intent == "attachment_qa"
    assert plan.action == "answer"
    assert plan.requested_formats == ()


def test_generic_table_export_is_not_template_generation_when_context_is_mounted():
    plan = classify_intent(
        "请把检索结果输出成 Excel 表格",
        has_template_context=True,
    )

    assert plan.intent == "export"
    assert plan.requested_formats == ("xlsx",)


@pytest.mark.parametrize(
    "query,kwargs",
    [
        ("基于知识库创建 ICD 报告", {"has_kb": True}),
        ("create a report from the attached files", {"has_attachments": True, "has_kb": False}),
    ],
)
def test_generic_document_authoring_does_not_require_a_template(query, kwargs):
    plan = classify_intent(query, **kwargs)

    assert plan.intent == "document_authoring"
    assert plan.action == "generate"
    assert plan.template_required is False


def test_current_result_pdf_is_conversational_export():
    plan = classify_intent("请把当前结果另存为 PDF", has_kb=True)

    assert plan.intent == "export"
    assert plan.action == "export"
    assert plan.export_requested is True
    assert plan.requested_formats == ("pdf",)
    assert "result_delivery_export" in plan.reason_codes


def test_ambiguous_document_request_requires_clarification():
    plan = classify_intent("整理成文档", has_kb=True)

    assert plan.intent == "document_authoring"
    assert plan.action == "clarify"
    assert plan.reason_codes == ("ambiguous_document_request",)
