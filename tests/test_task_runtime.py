"""Runtime glue: plan creation, server chapter summary and completion gate."""

from __future__ import annotations

from types import SimpleNamespace

import src.settings as settings
from src.agents.schemas import Evidence
from src.services import task_settings
from src.services.document_comparison import (
    canonicalize_parts,
    compare_documents,
    render_comparison_page,
)
from src.services.task_runtime import (
    create_compare_task,
    ensure_comparison_answer,
    finalize_task,
    completion_note,
    record_comparison_page,
    run_comparison_preflight,
)
from src.storage.document_comparison_store import DocumentComparisonStore


def _report(count=2):
    left = canonicalize_parts(
        [SimpleNamespace(
            part_id=f"l{index}", ordinal=index, part_type="text",
            text_content=f"第 {index} 章旧内容" + "A" * 300,
            locator={"heading_path": [f"{index} 章"], "paragraph": index},
            metadata={}, parser_version="v1",
        ) for index in range(1, count + 1)],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [SimpleNamespace(
            part_id=f"r{index}", ordinal=index, part_type="text",
            text_content=f"第 {index} 章新内容" + "B" * 300,
            locator={"heading_path": [f"{index} 章"], "paragraph": index},
            metadata={}, parser_version="v1",
        ) for index in range(1, count + 1)],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )
    return compare_documents(left, right)


def _rt(report, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))
    DocumentComparisonStore().save(report, owner_user_id=None)
    evidence = Evidence(
        id=f"comparison:{report.report_id}:x",
        content="comparison",
        source_name="a ↔ b",
        content_kind="comparison_modified",
        processor_kind="document_comparison",
        metadata={
            "source_type": "comparison",
            "comparison_report_id": report.report_id,
            "comparison_item_id": report.items[0].item_id,
        },
    )
    return SimpleNamespace(
        evidence=[evidence],
        attachment_user_id=None,
        attachment_session_id=None,
        chat_session_id="s1",
        task_plan=None,
        task_progress=None,
        turn_budget=None,
    )


def test_preflight_invokes_document_compare_once(monkeypatch):
    monkeypatch.setattr(task_settings, "TASK_COMPARISON_PREFLIGHT_ENABLED", True)
    calls = []

    class _Tool:
        name = "document_compare"

        def invoke(self, args):
            calls.append(args)
            return "预检结果"

    assert run_comparison_preflight([_Tool()], SimpleNamespace()) == "预检结果"
    assert calls == [{}]


def test_preflight_supports_plain_tool_functions_and_attachment_args(monkeypatch):
    monkeypatch.setattr(task_settings, "TASK_COMPARISON_PREFLIGHT_ENABLED", True)
    calls = []

    def document_compare(**kwargs):
        calls.append(kwargs)
        return "plain result"

    rt = SimpleNamespace(attachment_refs=[SimpleNamespace(attachment_id="att-1")])
    assert run_comparison_preflight([document_compare], rt) == "plain result"
    assert calls == [{"left": "attachment:att-1"}]


def test_preflight_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(task_settings, "TASK_COMPARISON_PREFLIGHT_ENABLED", False)

    class _Tool:
        name = "document_compare"

        def invoke(self, args):
            raise AssertionError("should not be invoked")

    assert run_comparison_preflight([_Tool()], SimpleNamespace()) == ""


def test_compare_task_chapter_summary_runs_to_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(task_settings, "TASK_PLANNING_ENABLED", True)
    monkeypatch.setattr(task_settings, "TASK_COMPLETION_GATE_ENABLED", True)
    report = _report()
    rt = _rt(report, tmp_path, monkeypatch)
    plan = create_compare_task(
        rt,
        query="按章节说差异",
        turn_id="t1",
        evidence_purpose="compare",
        source_scope_fingerprint="scope",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    assert plan is not None
    answer = ensure_comparison_answer(rt, "")
    assert "章节概览" in answer
    decision = finalize_task(rt, answer_text=answer, evidence=rt.evidence)
    assert decision is not None
    assert decision.status == "complete"
    assert completion_note(decision) == ""


def test_chapter_summary_covers_29_items_without_inline_bodies(monkeypatch, tmp_path):
    monkeypatch.setattr(task_settings, "TASK_PLANNING_ENABLED", True)
    monkeypatch.setattr(task_settings, "TASK_COMPLETION_GATE_ENABLED", True)
    report = _report(count=29)
    rt = _rt(report, tmp_path, monkeypatch)
    create_compare_task(
        rt,
        query="按章节说差异",
        turn_id="t1",
        evidence_purpose="compare",
        source_scope_fingerprint="scope",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    answer = ensure_comparison_answer(rt, "")
    assert answer.count("- num:") == 29
    decision = finalize_task(rt, answer_text=answer, evidence=rt.evidence)
    assert decision is not None
    assert decision.status in {"complete", "complete_with_warnings"}
    assert "compare.item_body" not in decision.missing_deliverables


def test_full_report_uses_artifact_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(task_settings, "TASK_PLANNING_ENABLED", True)
    monkeypatch.setattr(task_settings, "TASK_COMPLETION_GATE_ENABLED", True)
    report = _report()
    rt = _rt(report, tmp_path, monkeypatch)
    plan = create_compare_task(
        rt,
        query="所有章节逐条列出",
        turn_id="t1",
        evidence_purpose="compare",
        source_scope_fingerprint="scope",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    assert plan is not None
    answer = ensure_comparison_answer(rt, "索引见下")
    assert "完整比较报告" in answer
    decision = finalize_task(
        rt, answer_text=answer, evidence=rt.evidence,
        artifact_id=report.report_id,
    )
    assert decision is not None
    assert decision.status == "complete"


def test_chapter_detail_page_is_partial_until_scope_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(task_settings, "TASK_PLANNING_ENABLED", True)
    monkeypatch.setattr(task_settings, "TASK_COMPLETION_GATE_ENABLED", True)
    report = _report()
    rt = _rt(report, tmp_path, monkeypatch)
    plan = create_compare_task(
        rt,
        query="第 1 章具体差异",
        turn_id="t1",
        evidence_purpose="compare",
        source_scope_fingerprint="scope",
        has_attachments=True,
        has_kb=True,
        comparison_available=True,
    )
    assert plan is not None
    page = render_comparison_page(report, chapter_keys=("num:1",), limit=10)
    record_comparison_page(rt, report, page)
    decision = finalize_task(rt, answer_text="第 1 章差异", evidence=rt.evidence)
    assert decision is not None
    # The citation deliverable is intentionally unresolved until the answer
    # cites admitted evidence; the gate must not silently claim completion.
    assert decision.status in {"partial", "continue"}
