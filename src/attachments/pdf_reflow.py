"""PDF -> editable DOCX text reflow.

PDF glyphs cannot be edited in place, so the honest alternative is a
deterministic **text reflow** into a clean DOCX that our own editing tools can
actually modify.  Layout, images and tables are intentionally not preserved
and the audit record says so; reviewers must compare before/after text.

A LibreOffice headless conversion was evaluated and rejected: its Writer PDF
import duplicates every text line into twin text boxes, which makes
unique-match edits ambiguous, so this module extracts the text layer itself.
"""

from __future__ import annotations

import io
from typing import Any


class PdfReflowUnavailable(ValueError):
    """Raised when a PDF has no text layer to reflow (for example a scan)."""


def _page_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def reflow_to_docx(source: bytes) -> tuple[bytes, dict[str, Any]]:
    """Extract the PDF text layer into a clean, editable DOCX.

    Each page contributes a ``—— 第 N 页 ——`` marker followed by one paragraph
    per non-empty line.  Paragraph text is exact, so later
    ``replace_text``/``replace_section`` edits and before/after diffs stay
    trustworthy.
    """

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(source))
    except Exception as exc:  # noqa: BLE001 - corrupt input
        raise PdfReflowUnavailable(f"PDF 无法解析：{exc}") from exc
    if reader.is_encrypted:
        raise PdfReflowUnavailable("加密 PDF 不支持重排")

    from docx import Document

    document = Document()
    page_count = 0
    paragraph_count = 0
    empty_pages: list[int] = []
    for index, page in enumerate(reader.pages, start=1):
        page_count += 1
        try:
            lines = _page_lines(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - extraction failure per page
            lines = []
        if not lines:
            empty_pages.append(index)
            continue
        document.add_paragraph(f"—— 第 {index} 页 ——")
        paragraph_count += 1
        for line in lines:
            document.add_paragraph(line)
            paragraph_count += 1

    if paragraph_count == 0:
        raise PdfReflowUnavailable(
            "PDF 没有可提取的文本层（可能是扫描件）；"
            "请使用 annotate_text 生成批注候选，或先对 PDF 做 OCR。"
        )

    buffer = io.BytesIO()
    document.save(buffer)
    audit = {
        "operation": "reflow_to_docx",
        "format": "pdf",
        "child_extension": ".docx",
        "engine": "pypdf-text",
        "pages": page_count,
        "paragraph_count": paragraph_count,
        "empty_pages": empty_pages,
        "fidelity_note": (
            "仅重排文本：版式、图片与表格不保留，必须人工复核文本 diff 后再使用。"
        ),
    }
    return buffer.getvalue(), audit


__all__ = ["PdfReflowUnavailable", "reflow_to_docx"]
