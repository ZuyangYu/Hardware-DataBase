"""Task purpose selects a bounded evidence strategy."""

from __future__ import annotations

from src.core.conversation_orchestrator import ConversationOrchestrator
from src.core.intent import classify_intent
from src.services.evidence_policy import (
    COMPARE,
    EXTRACT,
    QA,
    SUMMARIZE,
    VISUAL,
    policy_for_purpose,
    resolve_evidence_purpose,
)


def test_purpose_resolution_covers_explicit_verbs():
    # Comparison is a model-selected tool call, not a keyword-derived purpose.
    assert resolve_evidence_purpose("对比附件和知识库中的 HSI 文档") == QA
    assert resolve_evidence_purpose("总结这份附件的第 2 章") == SUMMARIZE
    assert resolve_evidence_purpose("提取表格中的所有管脚字段") == EXTRACT
    assert resolve_evidence_purpose("看一下第 3 页的图片") == VISUAL
    assert resolve_evidence_purpose("管脚 2 的电压是多少") == QA


def test_conversation_plan_records_comparison_purpose():
    plan = ConversationOrchestrator().plan(
        query="对比附件和知识库中的 HSI 文档",
        has_attachments=True,
        has_kb=True,
        source_scope="attachment_and_knowledge_base",
    )
    assert plan.evidence_purpose == QA
    assert plan.comparison_requested is True
    assert plan.to_dict()["evidence_purpose"] == QA


def test_compare_policy_requires_both_sources_and_never_formally_prefetches():
    policy = policy_for_purpose(
        COMPARE, source_scope="attachment_and_knowledge_base",
        has_attachments=True, has_kb=True,
    )
    assert policy.require_both_sources is True
    assert policy.formal_prefetch is False
    assert policy.comparison_required is True
    assert policy.prefetch_mode == "catalog_only"


def test_author_and_extract_policies_do_not_share_qa_preview_budget():
    assert policy_for_purpose("author").prefetch_mode == "catalog_only"
    assert policy_for_purpose("extract").prefetch_mode == "structured_preview"
    assert policy_for_purpose("summarize").prefetch_mode == "section_preview"
    assert policy_for_purpose("qa").prefetch_mode == "candidate_preview"


def test_intent_classifier_still_routes_comparison_before_generation():
    intent = classify_intent(
        "参考模板对比附件和知识库，并生成 PDF 报告",
        has_attachments=True,
        has_kb=True,
    )
    assert intent.intent == "compare"
    assert intent.comparison_requested is True
