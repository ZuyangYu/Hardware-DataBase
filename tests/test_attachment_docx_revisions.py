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
    append_paragraphs_in_docx,
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


def _add_embedded_run(paragraph) -> None:
    """Attach an OLE-object run (the HSI embedded-workbook shape)."""

    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    paragraph._p.append(
        parse_xml(f'<w:r {nsdecls("w")}><w:object><w:embed/></w:object></w:r>')
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


def test_replace_text_multiline_inherits_first_run_formatting():
    from docx import Document
    from docx.shared import Pt

    def build(document):
        paragraph = document.add_paragraph()
        run = paragraph.add_run("旧段落")
        run.font.size = Pt(14)

    source = _build_docx(build)
    child, _audit = replace_text_in_docx(source, "旧段落", "第一行\n第二行")

    document = Document(io.BytesIO(child))
    assert [paragraph.text for paragraph in document.paragraphs] == ["第一行", "第二行"]
    assert document.paragraphs[0].runs[0].font.size == Pt(14)
    assert document.paragraphs[1].runs[0].font.size == Pt(14)


def test_replace_text_refuses_whole_block_with_ole_object():
    def build(document):
        paragraph = document.add_paragraph("旧对象段")
        _add_embedded_run(paragraph)

    source = _build_docx(build)
    with pytest.raises(DocxRevisionError, match="embedded object"):
        replace_text_in_docx(source, "旧对象段", "新\n内容")


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


def _embedded_section_docx() -> bytes:
    def build(document):
        document.add_heading("1 概述", level=1)
        document.add_paragraph("概述正文")
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "原表"
        holder = document.add_paragraph("对象说明")
        _add_embedded_run(holder)
        document.add_heading("2 其他", level=1)
        document.add_paragraph("其他内容")

    return _build_docx(build)


def test_replace_section_preserves_tables_and_embedded_objects():
    from docx import Document
    from docx.oxml.ns import qn

    source = _embedded_section_docx()
    child, audit = replace_section_in_docx(source, ["1 概述"], "新概述一\n新概述二")

    document = Document(io.BytesIO(child))
    texts = [paragraph.text for paragraph in document.paragraphs]
    assert texts == ["1 概述", "新概述一", "新概述二", "", "2 其他", "其他内容"]
    assert "概述正文" not in texts
    assert "对象说明" not in texts
    assert document.tables[0].cell(0, 0).text == "原表"
    holder = document.paragraphs[3]
    assert holder._p.find(f".//{qn('w:object')}") is not None
    assert audit["embedded_policy"] == "preserve"
    assert audit["preserved_tables"] == 1
    assert audit["preserved_drawings"] == 1
    assert audit["removed_tables"] == 0
    assert audit["removed_drawings"] == 0
    assert audit["removed_paragraphs"] == 1
    assert "概述正文" in audit["removed_text"]


def test_replace_section_can_drop_embedded_objects_when_explicit():
    from docx import Document
    from docx.oxml.ns import qn

    source = _embedded_section_docx()
    child, audit = replace_section_in_docx(
        source, ["1 概述"], "新概述", preserve_embedded=False
    )

    document = Document(io.BytesIO(child))
    assert document.tables == []
    assert all(
        paragraph._p.find(f".//{qn('w:object')}") is None
        for paragraph in document.paragraphs
    )
    assert audit["embedded_policy"] == "drop"
    assert audit["removed_tables"] == 1
    assert audit["removed_drawings"] == 1
    assert audit["preserved_tables"] == 0
    assert audit["preserved_drawings"] == 0


def test_replace_section_inherits_body_style_and_run_formatting():
    from docx import Document
    from docx.shared import Pt

    def build(document):
        document.add_heading("1 概述", level=1)
        paragraph = document.add_paragraph()
        run = paragraph.add_run("旧正文")
        run.font.size = Pt(16)
        run.font.name = "宋体"
        run.bold = True
        document.add_heading("2 其他", level=1)
        document.add_paragraph("其他内容")

    source = _build_docx(build)
    child, _audit = replace_section_in_docx(source, ["1 概述"], "新正文一\n新正文二")

    document = Document(io.BytesIO(child))
    assert document.paragraphs[0].text == "1 概述"
    new_paragraphs = document.paragraphs[1:3]
    assert [paragraph.text for paragraph in new_paragraphs] == ["新正文一", "新正文二"]
    for paragraph in new_paragraphs:
        run = paragraph.runs[0]
        assert run.font.size == Pt(16)
        assert run.font.name == "宋体"
        assert run.bold is True
    assert new_paragraphs[0].style == new_paragraphs[1].style


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


def test_service_section_revision_summary_reports_preserved_objects(
    tmp_path, monkeypatch
):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_embedded_section_docx()),
    )

    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="replace_section",
        heading_path=["1 概述"],
        after="新概述",
        client_request_id="section-preserve-1",
    )

    summary = revision_change_summary(revision)
    assert "保留 1 个表格、1 个嵌入对象（移至节末）" in summary
    assert revision.details["embedded_policy"] == "preserve"
    assert revision.details["preserved_drawings"] == 1


def test_attachment_edit_blocks_unconfirmed_embedded_removal(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_embedded_section_docx()),
    )
    runtime = _runtime(service, parent)
    runtime.question = "完善第1章的内容并替换掉原来的第1章"

    payload = json.loads(
        make_attachment_edit(runtime)(
            operation="replace_section",
            heading_path=["1 概述"],
            after="新概述",
            preserve_embedded=False,
        )
    )

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "attachment_embedded_removal_unconfirmed"


def test_attachment_edit_drops_embedded_when_user_asked(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_embedded_section_docx()),
    )
    runtime = _runtime(service, parent)
    runtime.question = "把第1章的表格和插图都删除，重写这章内容"

    payload = json.loads(
        make_attachment_edit(runtime)(
            operation="replace_section",
            heading_path=["1 概述"],
            after="新概述",
            preserve_embedded=False,
        )
    )

    assert payload["status"] == "completed"
    assert payload["details"]["embedded_policy"] == "drop"
    assert payload["details"]["removed_tables"] == 1
    assert payload["details"]["removed_drawings"] == 1


def test_replace_section_places_objects_with_placeholders():
    from docx import Document
    from docx.oxml.ns import qn

    source = _embedded_section_docx()
    child, audit = replace_section_in_docx(
        source,
        ["1 概述"],
        "开头\n[[embed:2]]\n中间\n[[embed:1]]\n结尾",
    )

    document = Document(io.BytesIO(child))
    tags: list[str] = []
    for element in document.element.body.iterchildren():
        if element.tag == qn("w:p"):
            tags.append("p:" + "".join(node.text or "" for node in element.iter(qn("w:t"))))
        elif element.tag == qn("w:tbl"):
            tags.append("tbl")
    assert tags == [
        "p:1 概述",
        "p:开头",
        "p:",
        "p:中间",
        "tbl",
        "p:结尾",
        "p:2 其他",
        "p:其他内容",
    ]
    assert document.tables[0].cell(0, 0).text == "原表"
    assert document.paragraphs[2]._p.find(f".//{qn('w:object')}") is not None
    assert audit["placed_objects"] == 2
    assert audit["preserved_tables"] == 1
    assert audit["preserved_drawings"] == 1


def test_replace_section_placeholder_errors():
    source = _embedded_section_docx()
    with pytest.raises(DocxRevisionError, match="out of range"):
        replace_section_in_docx(source, ["1 概述"], "[[embed:3]]")
    with pytest.raises(DocxRevisionError, match="more than once"):
        replace_section_in_docx(source, ["1 概述"], "[[embed:1]]\n[[embed:1]]")
    with pytest.raises(DocxRevisionError, match="own line"):
        replace_section_in_docx(source, ["1 概述"], "如图 [[embed:1]] 所示")


def test_replace_section_inherits_direct_paragraph_formatting():
    from docx import Document
    from docx.shared import Pt

    def build(document):
        document.add_heading("1 概述", level=1)
        paragraph = document.add_paragraph("旧正文")
        paragraph.paragraph_format.left_indent = Pt(24)
        paragraph.paragraph_format.space_after = Pt(12)
        paragraph.paragraph_format.page_break_before = True
        document.add_heading("2 其他", level=1)
        document.add_paragraph("其他内容")

    source = _build_docx(build)
    child, _audit = replace_section_in_docx(source, ["1 概述"], "新一\n新二")

    document = Document(io.BytesIO(child))
    for paragraph in document.paragraphs[1:3]:
        assert paragraph.paragraph_format.left_indent == Pt(24)
        assert paragraph.paragraph_format.space_after == Pt(12)
        assert not paragraph.paragraph_format.page_break_before


def test_replace_text_multiline_inherits_direct_paragraph_formatting():
    from docx import Document
    from docx.shared import Pt

    def build(document):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Pt(18)
        paragraph.add_run("旧段落")

    source = _build_docx(build)
    child, _audit = replace_text_in_docx(source, "旧段落", "第一行\n第二行")

    document = Document(io.BytesIO(child))
    assert [paragraph.text for paragraph in document.paragraphs] == ["第一行", "第二行"]
    assert document.paragraphs[1].paragraph_format.left_indent == Pt(18)


def test_append_paragraphs_at_section_end_inherits_formatting():
    from docx import Document
    from docx.shared import Pt

    def build(document):
        document.add_heading("1 概述", level=1)
        paragraph = document.add_paragraph("原有正文")
        paragraph.paragraph_format.left_indent = Pt(18)
        document.add_heading("2 其他", level=1)
        document.add_paragraph("其他内容")

    source = _build_docx(build)
    child, audit = append_paragraphs_in_docx(source, ["1 概述"], "追加一\n追加二")

    document = Document(io.BytesIO(child))
    assert [paragraph.text for paragraph in document.paragraphs] == [
        "1 概述",
        "原有正文",
        "追加一",
        "追加二",
        "2 其他",
        "其他内容",
    ]
    for paragraph in document.paragraphs[2:4]:
        assert paragraph.paragraph_format.left_indent == Pt(18)
    assert audit["operation"] == "append_paragraphs"
    assert audit["anchor"] == "section_end"
    assert audit["inserted_paragraphs"] == 2


def test_append_paragraphs_at_document_end():
    from docx import Document

    source = _sectioned_docx()
    child, audit = append_paragraphs_in_docx(source, None, "文末一\n文末二")

    document = Document(io.BytesIO(child))
    assert [paragraph.text for paragraph in document.paragraphs][-2:] == [
        "文末一",
        "文末二",
    ]
    assert audit["anchor"] == "document_end"
    assert audit["heading_path"] == []


def test_service_append_revision_creates_candidate(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
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
        operation="append_paragraphs",
        heading_path=["2 硬件架构"],
        after="补充一\n补充二",
        client_request_id="append-1",
    )

    assert revision.operation == "append_paragraphs"
    assert revision.details["anchor"] == "section_end"
    summary = revision_change_summary(revision)
    assert "章节「2 硬件架构」末尾追加 2 段" in summary

    _record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    texts = _paragraph_texts(child_bytes)
    assert texts.index("补充一") < texts.index("3 其他")


def test_attachment_edit_tool_exposes_append_operation(tmp_path, monkeypatch):
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
            operation="append_paragraphs",
            heading_path=["2 硬件架构"],
            after="工具追加段落",
        )
    )

    assert payload["status"] == "completed"
    assert payload["details"]["anchor"] == "section_end"
    _record, child_bytes = service.read_source_bytes(
        attachment_id=payload["child_attachment_id"], session_id=7, user_id=11
    )
    texts = _paragraph_texts(child_bytes)
    assert texts.index("工具追加段落") < texts.index("3 其他")


def test_attachment_edit_tool_blocks_invalid_placeholder(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="hsi.docx",
        stream=io.BytesIO(_embedded_section_docx()),
    )
    runtime = _runtime(service, parent)

    payload = json.loads(
        make_attachment_edit(runtime)(
            operation="replace_section",
            heading_path=["1 概述"],
            after="[[embed:9]]",
        )
    )

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "attachment_revision_rejected"
    assert "out of range" in payload["message"]
