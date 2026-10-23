"""Exact chapter expansion uses the frozen report and a stable cursor."""

from __future__ import annotations

from types import SimpleNamespace

import src.settings as settings
from src.agents.tools.comparison_tools import make_comparison_expand_tool
from src.agents.tools.runtime import ToolRuntime
from src.services.document_comparison import canonicalize_parts, compare_documents
from src.storage.document_comparison_store import DocumentComparisonStore


def _part(ordinal, text, path):
    return SimpleNamespace(
        part_id=f"p{ordinal}", ordinal=ordinal, part_type="text",
        text_content=text, locator={"heading_path": [path], "paragraph": ordinal},
        metadata={}, parser_version="v1",
    )


def _report():
    left = canonicalize_parts(
        [_part(i, f"第 {i} 节旧内容。" + "A" * 200, f"{i} 节") for i in range(1, 5)],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [_part(i, f"第 {i} 节新内容。" + "B" * 200, f"{i} 节") for i in range(1, 5)],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )
    return compare_documents(left, right)


def test_expand_reads_exact_chapter_and_returns_page_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))
    report = _report()
    DocumentComparisonStore().save(report, owner_user_id=None)
    rt = ToolRuntime(
        kb_name="", ctx=None, question="第 1 章差异",
        source_scope="attachment_only", attachment_user_id=None,
    )
    tool = make_comparison_expand_tool(rt)
    rendered = tool(report_id=report.report_id, chapter_keys="num:1", limit=10)
    assert "第 1 节" in rendered
    assert "比较分页" in rendered
    assert "当前展示 1/1 项" in rendered
    assert rt.evidence
    assert rt.evidence[0].metadata["comparison_report_id"] == report.report_id


def test_expand_without_report_id_fails_typed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))
    rt = ToolRuntime(kb_name="", ctx=None, question="第 1 章差异")
    tool = make_comparison_expand_tool(rt)
    rendered = tool(chapter_keys="num:1")
    assert "comparison_report_required" in rendered


def test_expand_unknown_chapter_returns_available_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))
    report = _report()
    DocumentComparisonStore().save(report, owner_user_id=None)
    rt = ToolRuntime(kb_name="", ctx=None, question="第 9 章差异")
    tool = make_comparison_expand_tool(rt)
    rendered = tool(report_id=report.report_id, chapter_keys="num:9")
    assert "未匹配到指定章节" in rendered
    assert "num:1" in rendered

