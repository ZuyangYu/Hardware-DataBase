"""Section/table alignment and exact diff for whole-document comparison."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.services.document_comparison import (
    canonicalize_parts,
    compare_documents,
    render_comparison_evidence,
)
from src.services.document_comparison_models import (
    ADDED,
    MOVED,
    TABLE_MODIFIED,
    UNCHANGED,
)


def _part(ordinal, text, *, path=("1 概述",), part_type="text", table_id=None, rows=None):
    locator = {"heading_path": list(path), "paragraph": ordinal}
    metadata = {}
    if table_id:
        locator["table_id"] = table_id
        metadata = {"table_id": table_id, "header_context": ["Pin", "Voltage"]}
        text = json.dumps(rows or [])
    return SimpleNamespace(
        part_id=f"part-{ordinal}",
        ordinal=ordinal,
        part_type=part_type,
        text_content=text,
        locator=locator,
        metadata=metadata,
        parser_version="test-parser",
    )


def _document(left: bool):
    parts = [
        _part(0, "系统概述。", path=("1 概述",)),
        _part(1, "管脚 2 的电压为 3.3V。" if left else "管脚 2 的电压为 5V。",
              path=("2 管脚定义",)),
        _part(2, "", path=("2 管脚定义",), part_type="table", table_id="table-0",
              rows=[["Pin", "Voltage"], ["2", "3.3" if left else "5"]]),
        _part(3, "安全机制说明。", path=("3 安全机制",)),
        _part(4, "仅左侧存在的段落。" if left else "仅右侧存在的段落。",
              path=("4 变更章节",)),
    ]
    if not left:
        parts.append(_part(5, "新增附录内容。", path=("5 新增章节",)))
    return canonicalize_parts(
        parts,
        source_type="attachment" if left else "knowledge_base",
        source_id="attachment:att-1" if left else "kb:5:record:9",
        source_name="附件 HSI" if left else "知识库 HSI",
        document_version="v1" if left else "v2",
        source_content_hash="a" * 64 if left else "b" * 64,
        parser_version="test-parser",
    )


def test_alignment_uses_section_numbers_and_keeps_changed_both_sides():
    report = compare_documents(_document(True), _document(False))
    assert report.status == "complete"
    changed = report.changed_items()
    assert changed
    modified = [item for item in changed if item.section_key == "num:2"]
    assert modified
    assert all(item.left_ref.startswith("attachment:att-1#") for item in modified)
    assert all(item.right_ref.startswith("kb:5:record:9#") for item in modified)
    assert any("5V" in item.right_text for item in modified)
    table_changes = [item for item in changed if item.kind == TABLE_MODIFIED]
    assert table_changes
    assert table_changes[0].left_locator["table_id"] == "table-0"


def test_unchanged_sections_are_retained_once_but_not_sent_to_model():
    report = compare_documents(_document(True), _document(False))
    unchanged = [item for item in report.items if item.kind == UNCHANGED]
    assert unchanged
    assert unchanged[0].left_ref and unchanged[0].right_ref
    evidence = render_comparison_evidence(report, max_bytes=100000)
    assert evidence
    assert all(item.metadata["evidence_role"] == "comparison" for item in evidence)
    assert all(item.metadata["comparison_sources"] == ["attachment", "knowledge_base"]
               for item in evidence)
    # The unchanged 1 概述 section is not injected unless the whole report is
    # identical.
    assert all("系统概述" not in item.content for item in evidence)


def test_heading_only_change_is_a_moved_or_modified_fact():
    left = _document(True)
    right_parts = [
        _part(0, "系统概述。", path=("1 概述",)),
        _part(1, "安全机制说明。", path=("3 安全机制（更新）",)),
    ]
    right = canonicalize_parts(
        right_parts, source_type="knowledge_base", source_id="kb:5:record:9",
        source_name="知识库 HSI", source_content_hash="c" * 64,
    )
    report = compare_documents(left, right)
    moved = [item for item in report.items if item.kind == MOVED]
    assert moved
    assert moved[0].left_text and moved[0].right_text


def test_new_section_is_added_with_one_side_reference():
    report = compare_documents(_document(True), _document(False))
    added = [item for item in report.items if item.kind == ADDED]
    assert added
    assert all(item.right_ref for item in added)


def test_identical_documents_return_one_summary_with_both_refs():
    left = canonicalize_parts(
        [_part(0, "相同内容。", path=("1 概述",))],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [_part(0, "相同内容。", path=("1 概述",))],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )
    report = compare_documents(left, right)
    assert report.changed_items() == []
    evidence = render_comparison_evidence(report)
    assert len(evidence) == 1
    assert evidence[0].metadata["left_ref"]
    assert evidence[0].metadata["right_ref"]
