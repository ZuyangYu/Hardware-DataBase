"""Honest PDF annotation candidates and structurally validated EDF edits."""

from __future__ import annotations

import io

import pytest

import src.settings
from src.attachments.edf_revision import EdfRevisionError, validate_edf_replacement
from src.attachments.pdf_revision import PdfRevisionError, annotate_text
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore

_EDIF = """(edif demo
  (edifVersion 2 0 0)
  (edifLevel 0)
  (keywordMap (keywordLevel 0))
  (comment "old")
  (library lib
    (edifLevel 0)
    (technology (numberDefinition))
    (cell AND2
      (cellType GENERIC)
      (view netlist
        (viewType NETLIST)
        (interface
          (port A (direction INPUT))
          (port B (direction INPUT))
          (port Y (direction OUTPUT))
        )
        (contents
          (instance U1
            (viewRef netlist (cellRef AND2 (libraryRef lib)))
          )
        )
      )
    )
  )
  (design demo
    (cellRef AND2 (libraryRef lib))
    (comment "design")
  )
)
"""


def _pdf_bytes() -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(300, 200))
    pdf.drawString(60, 150, "ADAS ICD")
    pdf.drawString(60, 120, "保留内容")
    pdf.save()
    return buffer.getvalue()


def _pdf_text(source: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(source))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _annotation_count(source: bytes) -> int:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(source))
    page = reader.pages[0]
    return len(page.get("/Annots") or [])


def _service(tmp_path, monkeypatch):
    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", True)
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_INDEX_DB_PATH", str(tmp_path / "att.db")
    )
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_STORAGE_DIR", str(tmp_path / "storage")
    )
    store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
    return AttachmentService(store=store), store


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_pdf_annotation_keeps_original_text_and_adds_one_note():
    source = _pdf_bytes()
    child, audit = annotate_text(
        source, before="ADAS ICD", after="ADAS Interface Control Document"
    )

    assert audit["operation"] == "annotate_text"
    assert audit["page"] == 1
    # The original glyphs are still present; the suggestion lives in a note.
    assert "ADAS ICD" in _pdf_text(child)
    assert _annotation_count(source) == 0
    assert _annotation_count(child) == 1


def test_pdf_annotation_rejects_missing_and_ambiguous_targets():
    source = _pdf_bytes()
    with pytest.raises(PdfRevisionError, match="未在 PDF 中找到"):
        annotate_text(source, before="不存在的文本", after="X")

    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(300, 200))
    pdf.drawString(60, 150, "ADAS ICD")
    pdf.drawString(60, 120, "ADAS ICD")
    pdf.save()
    duplicated = buffer.getvalue()
    with pytest.raises(PdfRevisionError, match="唯一"):
        annotate_text(duplicated, before="ADAS ICD", after="X")


def test_service_pdf_revision_audits_annotation(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7, user_id=11, filename="hsi.pdf", stream=io.BytesIO(_pdf_bytes())
    )

    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="annotate_text",
        before="ADAS ICD",
        after="ADAS Interface Control Document",
    )

    assert revision.operation == "annotate_text"
    assert revision.details["page"] == 1
    assert "批注" in revision.details["notice"]
    _record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    assert _annotation_count(child_bytes) == 1
    assert "ADAS ICD" in _pdf_text(child_bytes)

    with pytest.raises(ValueError, match="annotate_text"):
        service.create_attachment_revision(
            parent_attachment_id=parent.attachment_id,
            session_id=7,
            user_id=11,
            operation="replace_text",
            before="ADAS ICD",
            after="X",
        )


# ---------------------------------------------------------------------------
# PDF reflow (optional LibreOffice engine)
# ---------------------------------------------------------------------------


def test_reflow_extracts_the_text_layer_into_editable_paragraphs():
    from docx import Document

    from src.attachments.pdf_reflow import reflow_to_docx

    child, audit = reflow_to_docx(_pdf_bytes())

    assert child.startswith(b"PK")
    assert audit["operation"] == "reflow_to_docx"
    assert audit["child_extension"] == ".docx"
    assert audit["engine"] == "pypdf-text"
    assert "复核" in audit["fidelity_note"]
    texts = [paragraph.text for paragraph in Document(io.BytesIO(child)).paragraphs]
    assert "—— 第 1 页 ——" in texts
    assert "ADAS ICD" in texts


def test_reflow_rejects_a_pdf_without_a_text_layer():
    from reportlab.pdfgen import canvas

    from src.attachments.pdf_reflow import PdfReflowUnavailable, reflow_to_docx

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(200, 100))
    pdf.drawString(20, 50, "")  # blank page, no text layer
    pdf.save()

    with pytest.raises(PdfReflowUnavailable, match="文本层"):
        reflow_to_docx(buffer.getvalue())


def test_service_reflow_produces_a_docx_candidate(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7, user_id=11, filename="hsi.pdf", stream=io.BytesIO(_pdf_bytes())
    )

    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="reflow_to_docx",
    )

    assert revision.operation == "reflow_to_docx"
    assert revision.details["child_extension"] == ".docx"
    record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    assert record.filename.endswith(".docx")
    assert child_bytes.startswith(b"PK")
    from docx import Document

    texts = [paragraph.text for paragraph in Document(io.BytesIO(child_bytes)).paragraphs]
    assert "ADAS ICD" in texts

    # A textless PDF is refused with a clear reason.
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    blank = canvas.Canvas(buffer, pagesize=(200, 100))
    blank.drawString(20, 50, "")
    blank.save()
    blank_record = service.upload(
        session_id=7, user_id=11, filename="scan.pdf", stream=io.BytesIO(buffer.getvalue())
    )
    with pytest.raises(ValueError, match="文本层"):
        service.create_attachment_revision(
            parent_attachment_id=blank_record.attachment_id,
            session_id=7,
            user_id=11,
            operation="reflow_to_docx",
        )


# ---------------------------------------------------------------------------
# EDF
# ---------------------------------------------------------------------------


def test_edf_validator_accepts_cosmetic_and_rejects_corrupting_edits():
    parent = _EDIF.encode("utf-8")
    cosmetic = _EDIF.replace('(comment "old")', '(comment "new")').encode("utf-8")
    validate_edf_replacement(parent, cosmetic)

    corrupt = _EDIF.replace("(edif demo", "(edifx demo").encode("utf-8")
    with pytest.raises(EdfRevisionError):
        validate_edf_replacement(parent, corrupt)


def test_service_edf_edit_is_validated(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7, user_id=11, filename="board.edf", stream=io.BytesIO(_EDIF.encode("utf-8"))
    )

    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="replace_text",
        before="old",
        after="new",
    )
    _record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    assert b'"new"' in child_bytes

    with pytest.raises(ValueError, match="EDF 解析校验失败|EDF 结构校验未通过"):
        service.create_attachment_revision(
            parent_attachment_id=parent.attachment_id,
            session_id=7,
            user_id=11,
            operation="replace_text",
            before="(edif demo",
            after="(edifx demo",
        )
