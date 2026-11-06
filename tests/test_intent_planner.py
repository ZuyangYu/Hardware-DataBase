from __future__ import annotations

from src.core.intent import classify_intent, extract_requested_formats


def test_comparison_has_priority_over_export():
    plan = classify_intent("对比两份附件并导出 PDF", has_attachments=True)
    assert plan.intent == "compare"
    assert plan.action == "compare"
    assert plan.comparison_requested is True
    assert plan.requested_formats == ("pdf",)


def test_attachment_question_is_not_export():
    plan = classify_intent("这个 PDF 里有哪些接口？", has_attachments=True)
    assert plan.intent == "attachment_qa"
    assert plan.export_requested is False


def test_generic_table_export_is_export():
    plan = classify_intent("把当前结果整理成 Excel 表格")
    assert plan.intent == "export"
    assert plan.action == "export"
    assert plan.requested_formats == ("xlsx",)


def test_multiple_document_output_formats_require_clarification():
    plan = classify_intent("把检索结果导出为 PDF 和 Word")
    assert plan.intent == "export"
    assert plan.action == "clarify"
    assert set(plan.requested_formats) == {"pdf", "docx"}


def test_unsupported_document_output_format_requires_clarification():
    plan = classify_intent("把结果导出为 CSV")
    assert plan.intent == "export"
    assert plan.action == "clarify"


def test_xlsm_is_preserved_as_an_explicit_output_format():
    assert extract_requested_formats("导出 xlsm") == ("xlsm",)


def test_generation_without_explicit_format_is_knowledge_base_qa():
    plan = classify_intent("根据知识库生成一份 ICD 报告", has_kb=True)
    assert plan.intent == "knowledge_base_qa"
    assert plan.action == "answer"


def test_revision_question_remains_knowledge_base_qa():
    plan = classify_intent("更新协议的原因是什么？", has_kb=True)
    assert plan.intent == "knowledge_base_qa"


def test_current_result_pdf_is_conversational_export():
    plan = classify_intent("把刚才的结果导出成 PDF")
    assert plan.intent == "export"
    assert plan.action == "export"
    assert plan.reason_codes == ("result_delivery_export",)


def test_empty_query_is_general_qa():
    plan = classify_intent("   ")
    assert plan.intent == "general_qa"
    assert plan.confidence == 1.0


def test_drawio_request_with_markdown_reference_is_not_an_export():
    plan = classify_intent(
        "读取 references/02-block-diagram.md 后生成 aaa_tcn2_block.drawio",
        has_kb=True,
    )
    assert plan.intent == "knowledge_base_qa"
    assert plan.action == "answer"
    assert plan.export_requested is False
    assert plan.reason_codes == ("drawio_file_request",)
