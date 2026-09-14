"""Structured DOCX attachment revisions (replace_text / replace_section).

The contract is intentionally exact: a unique text target or one heading
section.  Everything outside the target must survive byte-for-byte in the
child candidate, and the audit record must name the edited range.
"""

from __future__ import annotations

import io
import json

import pytest

import src.settings
from src.agents.tools.attachment_tools import make_attachment_edit
from src.agents.tools.runtime import ToolRuntime
from src.attachments.docx_revision import (
    DocxRevisionError,
    heading_level,
    replace_section_in_docx,
    replace_text_in_docx,
)
from src.attachments.service import AttachmentService, revision_change_summary
from src.attachments.store import AttachmentStore


def _service(tmp_path, monkeypatch):
    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", True)
    monkeypatch.setattr(
        src.settings,
        "CHAT_ATTACHMENT_INDEX_DB_PATH",
        str(tmp_path / "attachments.db"),
    )
    monkeypatch.setattr(
        src.settings,
        "CHAT_ATTACHMENT_STORAGE_DIR",
        str(tmp_path / "storage"),
    )
    store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
    return AttachmentService(store=store), store


def _build_docx(build) -> bytes:
    from docx import Document

    document = Document()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paragraph_texts(source: bytes) -> list[str]:
    from docx import Document

    return [paragraph.text for paragraph in Document(io.BytesIO(source)).paragraphs]


def _runtime(service, record) -> ToolRuntime:
    return ToolRuntime(
        kb_name="",
        ctx=None,
        chat_session_id=str(record.session_id),
        source_scope="attachment_only",
        attachment_refs=service.build_refs([record]),
        attachment_service=service,
        attachment_user_id=record.user_id,
        attachment_session_id=record.session_id,
    )


# ---------------------------------------------------------------------------
# replace_text
# ---------------------------------------------------------------------------


def test_replace_text_preserves_other_paragraphs_and_run_formatting():
    def build(document):
        document.add_paragraph("标题")
        paragraph = document.add_paragraph()
        paragraph.add_run("前缀")
        highlighted = paragraph.add_run("ADAS ICD")
        highlighted.bold = True
        paragraph.add_run("后缀")
        document.add_paragraph("保留内容")

    source = _build_docx(build)
    child, audit = replace_text_in_docx(source, "ADAS ICD", "ADAS Interface Control Document")

    texts = _paragraph_texts(child)
    assert texts == [
        "标题",
        "前缀ADAS Interface Control Document后缀",
        "保留内容",
    ]
    assert audit["operation"] == "replace_text"
    assert audit["mode"] == "span"

    from docx import Document

    child_paragraph = Document(io.BytesIO(child)).paragraphs[1]
    assert child_paragraph.runs[0].text == "前缀"
    assert child_paragraph.runs[1].text == "ADAS Interface Control Document"
    assert child_paragraph.runs[1].bold is True
    assert child_paragraph.runs[2].text == "后缀"


def test_replace_text_can_target_a_table_cell():
    def build(document):
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "旧值"
        table.cell(0, 1).text = "保留值"

    source = _build_docx(build)
    child, audit = replace_text_in_docx(source, "旧值", "新值")

    from docx import Document

    table = Document(io.BytesIO(child)).tables[0]
    assert table.cell(0, 0).text == "新值"
    assert table.cell(0, 1).text == "保留值"
    assert audit["occurrences"] == 1


def test_replace_text_rejects_missing_and_ambiguous_targets():
    def build(document):
        document.add_paragraph("same")
        document.add_paragraph("same")

    source = _build_docx(build)
    with pytest.raises(DocxRevisionError, match="exactly once"):
        replace_text_in_docx(source, "same", "changed")
    with pytest.raises(DocxRevisionError, match="not found"):
        replace_text_in_docx(source, "missing", "changed")


def test_replace_text_multiline_expands_a_whole_paragraph():
    def build(document):
        document.add_paragraph("旧章节正文")

    source = _build_docx(build)
    child, audit = replace_text_in_docx(source, "旧章节正文", "第一行\n第二行")

    assert _paragraph_texts(child) == ["第一行", "第二行"]
    assert audit["mode"] == "block"


# ---------------------------------------------------------------------------
# replace_section
# ---------------------------------------------------------------------------


def _sectioned_docx() -> bytes:
    def build(document):
        document.add_heading("1 概述", level=1)
        document.add_paragraph("概述内容")
        document.add_heading("2 硬件架构", level=1)
        document.add_paragraph("旧架构内容一")
        document.add_paragraph("旧架构内容二")
        document.add_heading("2.1 子系统", level=2)
        document.add_paragraph("子系统旧内容")
        document.add_heading("3 其他", level=1)
        document.add_paragraph("其他内容")

    return _build_docx(build)


def test_replace_section_only_replaces_the_target_section_body():
    source = _sectioned_docx()
    child, audit = replace_section_in_docx(
        source, ["2 硬件架构"], "新架构内容一\n新架构内容二"
    )

    texts = _paragraph_texts(child)
    assert texts == [
        "1 概述",
        "概述内容",
        "2 硬件架构",
        "新架构内容一",
        "新架构内容二",
        "3 其他",
        "其他内容",
    ]
    assert audit["operation"] == "replace_section"
    assert audit["heading_path"][-1] == "2 硬件架构"
    # A level-1 chapter replacement also removes its level-2 subsections.
    assert audit["removed_paragraphs"] == 4
    assert audit["inserted_paragraphs"] == 2
    assert "旧架构内容一" in audit["removed_text"]
    assert "2.1 子系统" in audit["removed_text"]
    assert "2.1 子系统" not in texts


def test_custom_localized_heading_styles_are_recognized():
    """Corporate docs use custom/localized styles based on Heading N."""
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE

    document = Document()
    custom = document.styles.add_style("样式1—标题1", WD_STYLE_TYPE.PARAGRAPH)
    custom.base_style = document.styles["Heading 1"]
    document.add_paragraph("共享和独占的硬件资源", style=custom)
    document.add_paragraph("正文内容")
    localized = document.styles.add_style("标题2", WD_STYLE_TYPE.PARAGRAPH)
    document.add_paragraph("6.1 信号对应关系", style=localized)
    document.add_paragraph("小节内容")
    buffer = io.BytesIO()
    document.save(buffer)
    source = buffer.getvalue()

    assert heading_level(Document(io.BytesIO(source)).paragraphs[0]) == 1
    assert heading_level(Document(io.BytesIO(source)).paragraphs[2]) == 2

    child, audit = replace_section_in_docx(
        source, ["共享和独占的硬件资源"], "新的章节正文"
    )
    texts = _paragraph_texts(child)
    assert texts == ["共享和独占的硬件资源", "新的章节正文"]
    assert audit["heading_path"][-1] == "共享和独占的硬件资源"
    assert "6.1 信号对应关系" in audit["removed_text"]


def test_docx_parser_exposes_heading_paths_for_custom_styles(tmp_path):
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE

    from src.attachments.local_document_parser import LocalDocumentParser
    from src.attachments.models import AttachmentAsset

    document = Document()
    custom = document.styles.add_style("样式1—标题1", WD_STYLE_TYPE.PARAGRAPH)
    custom.base_style = document.styles["Heading 1"]
    document.add_paragraph("共享和独占的硬件资源", style=custom)
    document.add_paragraph("正文内容")
    path = tmp_path / "custom-styles.docx"
    document.save(str(path))

    asset = AttachmentAsset(
        asset_id="asset-custom",
        session_id=7,
        user_id=11,
        tenant_id="default",
        sha256="hash",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        extension=".docx",
        size_bytes=1,
        storage_key="",
        parse_status="ready",
    )
    outcome = LocalDocumentParser()._parse_docx(asset, str(path))
    by_text = {part.text_content: part for part in outcome.parts}

    assert by_text["共享和独占的硬件资源"].locator["heading_path"] == [
        "共享和独占的硬件资源"
    ]
    assert by_text["正文内容"].locator["heading_path"] == ["共享和独占的硬件资源"]


def test_replace_section_requires_an_unambiguous_heading():
    source = _sectioned_docx()
    with pytest.raises(DocxRevisionError, match="not found"):
        replace_section_in_docx(source, ["9 不存在"], "新内容")

    def build(document):
        document.add_heading("重复章节", level=1)
        document.add_paragraph("a")
        document.add_heading("重复章节", level=1)
        document.add_paragraph("b")

    duplicate = _build_docx(build)
    with pytest.raises(DocxRevisionError, match="ambiguous"):
        replace_section_in_docx(duplicate, ["重复章节"], "新内容")


# ---------------------------------------------------------------------------
# service / candidate / audit
# ---------------------------------------------------------------------------


def test_service_docx_section_revision_creates_audited_candidate(tmp_path, monkeypatch):
    service, store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_sectioned_docx()),
    )

    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="replace_section",
        heading_path=["2 硬件架构"],
        after="新架构内容一\n新架构内容二",
        client_request_id="section-edit-1",
    )

    assert revision.operation == "replace_section"
    assert revision.details["heading_path"][-1] == "2 硬件架构"
    assert "旧架构内容一" in revision.before
    assert revision.status == "review_candidate"
    assert revision.release_state == "not_requested"

    summary = revision_change_summary(revision)
    assert "替换章节「2 硬件架构」" in summary
    assert "新增 2 段" in summary
    assert "移除 4 段" in summary

    persisted = store.get_attachment_revision(
        revision.revision_id, session_id=7, user_id=11
    )
    assert persisted is not None
    assert persisted.details["removed_paragraphs"] == 4

    _parent_record, parent_bytes = service.read_source_bytes(
        attachment_id=parent.attachment_id, session_id=7, user_id=11
    )
    child_record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    assert _paragraph_texts(parent_bytes) == _paragraph_texts(_sectioned_docx())
    assert "新架构内容一" in _paragraph_texts(child_bytes)
    assert "旧架构内容一" not in _paragraph_texts(child_bytes)
    assert child_record.sha256 == revision.child_content_hash

    repeated = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="replace_section",
        heading_path=["2 硬件架构"],
        after="新架构内容一\n新架构内容二",
        client_request_id="section-edit-1",
    )
    assert repeated.revision_id == revision.revision_id


def test_service_docx_text_revision_supports_docx(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="notes.docx",
        stream=io.BytesIO(
            _build_docx(lambda document: document.add_paragraph("旧标题"))
        ),
    )

    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        before="旧标题",
        after="新标题",
    )

    assert revision.operation == "replace_text"
    _record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    assert _paragraph_texts(child_bytes) == ["新标题"]


def test_service_section_revision_requires_heading(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_sectioned_docx()),
    )

    with pytest.raises(ValueError, match="heading"):
        service.create_attachment_revision(
            parent_attachment_id=parent.attachment_id,
            session_id=7,
            user_id=11,
            operation="replace_section",
            after="新内容",
        )


# ---------------------------------------------------------------------------
# agent tool surface
# ---------------------------------------------------------------------------


def test_attachment_edit_tool_exposes_section_operation(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_sectioned_docx()),
    )
    runtime = _runtime(service, parent)

    result = make_attachment_edit(runtime)(
        operation="replace_section",
        heading_path=["2 硬件架构"],
        after="工具写入的新内容",
        attachment_id=parent.attachment_id,
    )
    payload = json.loads(result)

    assert payload["status"] == "completed"
    assert payload["operation"] == "replace_section"
    assert payload["details"]["heading_path"][-1] == "2 硬件架构"
    assert payload["download_url"].endswith(
        f"/conversations/7/attachments/{payload['child_attachment_id']}/download"
    )

    _record, child_bytes = service.read_source_bytes(
        attachment_id=payload["child_attachment_id"], session_id=7, user_id=11
    )
    assert "工具写入的新内容" in _paragraph_texts(child_bytes)
    assert "旧架构内容一" not in _paragraph_texts(child_bytes)


def test_attachment_edit_tool_emits_downloadable_document_card(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="notes.docx",
        stream=io.BytesIO(
            _build_docx(lambda document: document.add_paragraph("旧标题"))
        ),
    )
    events: list[dict] = []
    runtime = _runtime(service, parent)
    runtime.on_event = events.append

    payload = json.loads(
        make_attachment_edit(runtime)(before="旧标题", after="新标题")
    )

    cards = [event for event in events if event["type"] == "document_card"]
    assert len(cards) == 1
    card = cards[0]["payload"]["card"]
    assert card["kind"] == "attachment_revision"
    assert card["status"] == "review_candidate"
    assert card["candidate_state"] == "available"
    assert card["artifacts"][0]["download_url"] == payload["download_url"]
    assert card["artifacts"][0]["output_format"] == "docx"


def test_attachment_edit_tool_blocks_ambiguous_section(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_sectioned_docx()),
    )
    runtime = _runtime(service, parent)

    payload = json.loads(
        make_attachment_edit(runtime)(
            operation="replace_section",
            heading_path=["9 不存在"],
            after="新内容",
        )
    )

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "attachment_revision_rejected"
    assert "not found" in payload["message"]
