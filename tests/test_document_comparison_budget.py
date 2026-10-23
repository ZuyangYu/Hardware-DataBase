"""Only changed comparison facts reach the model, under a byte cap."""

from __future__ import annotations

from types import SimpleNamespace

from src.services.document_comparison import (
    canonicalize_parts,
    compare_documents,
    render_comparison_evidence,
)


def _part(ordinal, text, path):
    return SimpleNamespace(
        part_id=f"p{ordinal}", ordinal=ordinal, part_type="text",
        text_content=text, locator={"heading_path": [path], "paragraph": ordinal},
        metadata={}, parser_version="v1",
    )


def _left():
    return canonicalize_parts(
        [_part(index, f"左侧第 {index} 节内容。" + "A" * 500, f"{index} 节")
         for index in range(1, 8)],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )


def _right():
    return canonicalize_parts(
        [_part(index, f"右侧第 {index} 节内容。" + "B" * 500, f"{index} 节")
         for index in range(1, 8)],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )


def test_render_respects_byte_and_item_limits():
    report = compare_documents(_left(), _right())
    assert len(report.changed_items()) >= 7
    evidence = render_comparison_evidence(report, max_bytes=2500, max_items=3)
    assert 1 <= len(evidence) <= 3
    assert sum(len(item.content.encode("utf-8")) for item in evidence) <= 3000
    assert all(item.metadata["comparison_report_id"] == report.report_id for item in evidence)


def test_modified_items_include_character_level_diff():
    report = compare_documents(_left(), _right())
    focused = render_comparison_evidence(report, max_bytes=100000, focus="第 3 节")
    assert focused
    assert any("逐字差异" in item.content for item in focused)


def test_focus_filters_changed_items_without_creating_a_new_report():
    report = compare_documents(_left(), _right())
    all_evidence = render_comparison_evidence(report, max_bytes=100000)
    focused = render_comparison_evidence(report, max_bytes=100000, focus="第 3 节")
    assert focused
    assert len(focused) < len(all_evidence)
    assert all("第 3 节" in item.content or "3 节" in item.content for item in focused)
    assert {item.metadata["comparison_report_id"] for item in focused} == {report.report_id}


def test_focus_matches_numbered_section_key():
    left = canonicalize_parts(
        [_part(3, "旧正文", "3 概述")],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [_part(3, "新正文", "3 概述")],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )
    report = compare_documents(left, right)
    focused = render_comparison_evidence(report, max_bytes=100000, focus="第 3 节")
    assert focused
    assert all("旧正文" in item.content and "新正文" in item.content for item in focused)


def test_every_changed_item_carries_both_side_references():
    report = compare_documents(_left(), _right())
    for item in report.changed_items():
        assert item.left_ref.startswith("attachment:att-1#")
        assert item.right_ref.startswith("kb:5:record:9#")
        assert item.evidence_refs
