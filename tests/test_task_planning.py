"""Deterministic task contracts and delivery-scope parsing."""

from __future__ import annotations

from src.services.task_planning import (
    TaskPlanCompiler,
    build_deliverables,
    scope_from_query,
)


def test_compare_defaults_to_chapter_summary_not_full_inline():
    scope = scope_from_query("对比这两份 HSI 文件", task_kind="compare")
    assert scope.scope_kind == "chapter_summary"
    assert scope.delivery_preference == "inline"


def test_explicit_chapter_is_chapter_detail():
    scope = scope_from_query("第 4 章具体有哪些差异？", task_kind="compare")
    assert scope.scope_kind == "chapter_detail"
    assert scope.chapter_keys == ("num:4",)


def test_explicit_section_is_exact_section():
    scope = scope_from_query("4.2 节有什么差异？", task_kind="compare")
    assert scope.scope_kind == "chapter_detail"
    assert scope.section_keys == ("num:4.2",)


def test_all_chapters_detail_prefers_artifact():
    scope = scope_from_query("所有章节逐条列出全部差异", task_kind="compare")
    assert scope.scope_kind == "full_report"
    assert scope.delivery_preference == "artifact"


def test_table_scope():
    scope = scope_from_query("对比表 10 的单元格差异", task_kind="compare")
    assert scope.scope_kind == "item_detail"
    assert scope.table_keys == ("table-10",)


def test_compiler_is_stable_and_scope_aware():
    compiler = TaskPlanCompiler()
    first = compiler.compile(
        query="按章节说差异",
        turn_id="t1",
        evidence_purpose="compare",
        source_scope_fingerprint="scope-a",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    second = compiler.compile(
        query="按章节说差异",
        turn_id="t1",
        evidence_purpose="compare",
        source_scope_fingerprint="scope-a",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    assert first.plan_hash == second.plan_hash
    assert first.delivery_scope.scope_kind == "chapter_summary"
    assert {item.deliverable_id for item in first.deliverables} >= {
        "compare.chapter_summary",
        "compare.index",
        "compare.answer",
    }


def test_compiler_marks_missing_capability():
    plan = TaskPlanCompiler().compile(
        query="对比这两份文件",
        evidence_purpose="compare",
        has_attachments=True,
        has_kb=True,
        comparison_available=False,
    )
    assert "comparison_capability_unavailable" in plan.compile_errors


def test_chapter_detail_deliverables_require_body_and_diff():
    plan = TaskPlanCompiler().compile(
        query="第 4 章具体差异",
        evidence_purpose="compare",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    kinds = {item.kind for item in plan.deliverables if item.required}
    assert "item_body" in kinds
    assert "inline_diff" in kinds


def test_build_deliverables_for_summary_does_not_require_body():
    scope = scope_from_query("按章节说差异", task_kind="compare")
    required = {item.kind for item in build_deliverables("compare", scope) if item.required}
    assert "chapter_summary" in required
    assert "item_body" not in required
    assert "inline_diff" not in required
