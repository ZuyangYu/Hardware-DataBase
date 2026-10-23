"""Exact chapter pagination for frozen comparison reports."""

from __future__ import annotations

from types import SimpleNamespace

from src.services.comparison_scope import (
    chapter_key_for_item,
    scope_chapter_item_ids,
    select_scope_items,
)
from src.services.document_comparison import (
    canonicalize_parts,
    compare_documents,
    render_comparison_page,
)
from src.services.task_planning import scope_from_query


def _part(ordinal, text, path):
    return SimpleNamespace(
        part_id=f"p{ordinal}", ordinal=ordinal, part_type="text",
        text_content=text, locator={"heading_path": [path], "paragraph": ordinal},
        metadata={}, parser_version="v1",
    )


def _report(items=12):
    left = canonicalize_parts(
        [_part(index, f"第 {index} 节旧内容。" + "A" * 200, f"{index} 节")
         for index in range(1, items + 1)],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [_part(index, f"第 {index} 节新内容。" + "B" * 200, f"{index} 节")
         for index in range(1, items + 1)],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )
    return compare_documents(left, right)


def test_page_has_stable_cursor_and_no_duplicates():
    report = _report()
    first = render_comparison_page(report, limit=3)
    assert first.total_count >= 3
    assert first.rendered_count <= 3
    assert first.next_cursor
    second = render_comparison_page(
        report, limit=3, cursor=first.next_cursor,
    )
    first_ids = {item.item_id for item in first.items}
    second_ids = {item.item_id for item in second.items}
    assert not (first_ids & second_ids)
    assert second.offset == 3


def test_chapter_scope_includes_subsection_items():
    report = _report()
    scope = scope_from_query("第 1 章具体差异", task_kind="compare")
    selected = select_scope_items(report, scope)
    assert selected
    assert all(chapter_key_for_item(item) == "num:1" for item in selected)


def test_chapter_summary_has_all_chapters_and_item_ids():
    report = _report()
    scope = scope_from_query("按章节说差异", task_kind="compare")
    chapters = scope_chapter_item_ids(report, scope)
    assert "num:1" in chapters
    assert "num:12" in chapters
    assert all(isinstance(value, tuple) for value in chapters.values())


def test_page_reports_remaining_count_and_truncation():
    report = _report()
    page = render_comparison_page(report, limit=2)
    assert page.rendered_count <= 2
    assert page.remaining_count == max(0, page.total_count - page.rendered_count)
    assert page.truncated is (page.remaining_count > 0)
