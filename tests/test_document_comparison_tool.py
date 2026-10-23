"""The comparison tool freezes both attachments and admits only diff facts."""

from __future__ import annotations

from types import SimpleNamespace

import src.settings as settings
from src.agents.tools import attachment_tools
from src.agents.tools.comparison_tools import make_document_comparison_tool
from src.agents.tools.runtime import ToolRuntime
from src.attachments.models import AttachmentRef


def _ref(attachment_id: str, asset_id: str, filename: str) -> AttachmentRef:
    return AttachmentRef(
        attachment_id=attachment_id, asset_id=asset_id, session_id=1,
        filename=filename, media_type="text/plain", extension=".txt",
        size_bytes=10, sha256=("a" if attachment_id == "att-1" else "b") * 64,
    )


class _Store:
    def __init__(self):
        self.parts = {
            "asset-1": [SimpleNamespace(
                part_id="p1", ordinal=0, part_type="text", text_content="管脚 2 电压 3.3V",
                locator={"heading_path": ["1 概述"], "paragraph": 0},
                metadata={}, parser_version="p1",
            )],
            "asset-2": [SimpleNamespace(
                part_id="p2", ordinal=0, part_type="text", text_content="管脚 2 电压 5V",
                locator={"heading_path": ["1 概述"], "paragraph": 0},
                metadata={}, parser_version="p1",
            )],
        }

    def list_parts(self, asset_id):
        return self.parts.get(asset_id, [])


def test_document_compare_tool_registers_changed_evidence(monkeypatch, tmp_path):
    refs = [_ref("att-1", "asset-1", "a.md"), _ref("att-2", "asset-2", "b.md")]
    monkeypatch.setattr(attachment_tools, "_authorized_refs", lambda rt: list(refs))
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))
    runtime = ToolRuntime(
        kb_name="", ctx=None, question="比较两个附件",
        source_scope="attachment_only", attachment_refs=refs,
        attachment_user_id=7, attachment_session_id=1,
        attachment_service=SimpleNamespace(store=_Store()),
        evidence_budget=10,
    )
    tool = make_document_comparison_tool(runtime)

    rendered = tool("attachment:att-1", "attachment:att-2")

    assert "comparison_modified" in rendered or "变化" in rendered
    assert "report_id=cmp-" in rendered
    assert runtime.evidence
    evidence = runtime.evidence[0]
    assert evidence.metadata["evidence_role"] == "comparison"
    assert evidence.metadata["comparison_sources"] == ["attachment", "attachment"]
    assert evidence.metadata["left_ref"].startswith("attachment:att-1#")
    assert evidence.metadata["right_ref"].startswith("attachment:att-2#")
    report_id = evidence.metadata["comparison_report_id"]
    assert (tmp_path / f"{report_id}.json").exists()


def test_scope_only_focus_does_not_filter_out_all_items(monkeypatch, tmp_path):
    refs = [_ref("att-1", "asset-1", "a.md"), _ref("att-2", "asset-2", "b.md")]
    monkeypatch.setattr(attachment_tools, "_authorized_refs", lambda rt: list(refs))
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))
    runtime = ToolRuntime(
        kb_name="", ctx=None, question="按章节说差异",
        source_scope="attachment_only", attachment_refs=refs,
        attachment_user_id=7, attachment_session_id=1,
        attachment_service=SimpleNamespace(store=_Store()),
        evidence_budget=10,
    )
    tool = make_document_comparison_tool(runtime)
    rendered = tool("attachment:att-1", "attachment:att-2", focus="按章节说差异")
    assert "comparison_modified" in rendered
    assert runtime.evidence


def test_focused_expand_reuses_frozen_report_after_initial_compare(monkeypatch, tmp_path):
    """A chapter follow-up must read the frozen report instead of rerunning it."""

    refs = [_ref("att-1", "asset-1", "a.md"), _ref("att-2", "asset-2", "b.md")]
    monkeypatch.setattr(attachment_tools, "_authorized_refs", lambda rt: list(refs))
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))
    runtime = ToolRuntime(
        kb_name="", ctx=None, question="比较两个附件",
        source_scope="attachment_only", attachment_refs=refs,
        attachment_user_id=7, attachment_session_id=1,
        attachment_service=SimpleNamespace(store=_Store()),
        evidence_budget=10,
    )
    tool = make_document_comparison_tool(runtime)
    tool("attachment:att-1", "attachment:att-2")

    from src.storage import document_comparison_store

    def conflict(*args, **kwargs):
        from src.services.document_comparison import ComparisonError

        raise ComparisonError("comparison_report_conflict", "stale report id")

    monkeypatch.setattr(document_comparison_store.DocumentComparisonStore, "save", conflict)
    focused = tool("attachment:att-1", "attachment:att-2", focus="概述")

    assert "逐字差异" in focused
    assert "comparison_report_conflict" not in focused


def test_focused_expand_recovers_matching_frozen_report_across_turns(monkeypatch, tmp_path):
    """A new turn can recover a stored report when a rerun hits a conflict."""

    refs = [_ref("att-1", "asset-1", "a.md"), _ref("att-2", "asset-2", "b.md")]
    monkeypatch.setattr(attachment_tools, "_authorized_refs", lambda rt: list(refs))
    monkeypatch.setattr(settings, "DOCUMENT_COMPARISON_STORAGE_DIR", str(tmp_path))

    def runtime():
        return ToolRuntime(
            kb_name="", ctx=None, question="比较两个附件",
            source_scope="attachment_only", attachment_refs=refs,
            attachment_user_id=7, attachment_session_id=1,
            attachment_service=SimpleNamespace(store=_Store()),
            evidence_budget=10,
        )

    make_document_comparison_tool(runtime())("attachment:att-1", "attachment:att-2")
    from src.storage import document_comparison_store

    def conflict(*args, **kwargs):
        from src.services.document_comparison import ComparisonError

        raise ComparisonError("comparison_report_conflict", "stale report id")

    monkeypatch.setattr(document_comparison_store.DocumentComparisonStore, "save", conflict)
    focused = make_document_comparison_tool(runtime())(
        "attachment:att-1", "attachment:att-2", focus="概述"
    )

    assert "逐字差异" in focused
    assert "comparison_report_conflict" not in focused


def test_kb_source_can_be_resolved_by_name_without_record_id(monkeypatch):
    from src.agents.tools import comparison_tools

    class _FakeStore:
        def __init__(self):
            self.record = SimpleNamespace(
                id=9, kb_id=5, document_name="600605919_EPS_HSI.docx",
                original_file_name="600605919_EPS_HSI.docx", department_id="47",
            )

        def list_documents(self, kb_name, department_id):
            return [self.record]

        def get_document_by_id_scoped(self, record_id, department_id):
            return self.record if record_id == 9 and department_id == "47" else None

    monkeypatch.setattr("src.pipelines.document_store.PipelineDocumentStore", _FakeStore)
    runtime = ToolRuntime(
        kb_name="KB5", ctx=None, question="对比附件和知识库 HSI 文档",
        source_scope="attachment_and_knowledge_base",
        knowledge_scope=SimpleNamespace(sources=[
            SimpleNamespace(kb_id=5, kb_name="KB5", department_id="47"),
        ]),
        document_store=_FakeStore(),
    )
    record = comparison_tools._resolve_kb_record(runtime, "", hint="知识库中的 HSI 文档")
    assert record.id == 9
    assert comparison_tools._resolve_kb_record(runtime, "kb:600605919_EPS_HSI.docx").id == 9


def test_identical_report_note_is_completed_not_no_evidence():
    from src.agents.tools.comparison_tools import _report_completion_note
    from src.services.document_comparison import canonicalize_parts, compare_documents

    left = canonicalize_parts(
        [SimpleNamespace(
            part_id="p1", ordinal=0, part_type="text", text_content="same",
            locator={"paragraph": 0}, metadata={}, parser_version="v1",
        )],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [SimpleNamespace(
            part_id="p2", ordinal=0, part_type="text", text_content="same",
            locator={"paragraph": 0}, metadata={}, parser_version="v1",
        )],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="a" * 64,
    )
    note = _report_completion_note(compare_documents(left, right))
    assert "内容哈希相同" in note
    assert "不得改写成“未完成”" in note


def test_document_compare_tool_rejects_same_source(monkeypatch):
    ref = _ref("att-1", "asset-1", "a.md")
    monkeypatch.setattr(attachment_tools, "_authorized_refs", lambda rt: [ref])
    runtime = ToolRuntime(
        kb_name="", ctx=None, question="比较同一个附件",
        source_scope="attachment_only", attachment_refs=[ref],
        attachment_user_id=7, attachment_session_id=1,
        attachment_service=SimpleNamespace(store=_Store()),
    )
    tool = make_document_comparison_tool(runtime)
    result = tool("attachment:att-1", "attachment:att-1")
    assert "same_source" in result
    assert runtime.evidence == []
