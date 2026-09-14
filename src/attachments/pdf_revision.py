"""Honest PDF "revision": an annotated candidate, never an in-place rewrite.

PDF text is not safely editable in general, so the server refuses to pretend.
``annotate_text`` locates one unique text occurrence and returns a child PDF
that keeps every original glyph while carrying a review note with the
suggested replacement.
"""

from __future__ import annotations

import io
from typing import Any


class PdfRevisionError(ValueError):
    """Raised when a PDF annotation target is missing, ambiguous or unsafe."""


def _mult(matrix_a, matrix_b) -> tuple[float, ...]:
    """Affine 2x3 matrix multiply (pypdf layout coordinates)."""

    a0, a1, a2, a3, a4, a5 = (float(value) for value in matrix_a)
    b0, b1, b2, b3, b4, b5 = (float(value) for value in matrix_b)
    return (
        a0 * b0 + a1 * b2,
        a0 * b1 + a1 * b3,
        a2 * b0 + a3 * b2,
        a2 * b1 + a3 * b3,
        a4 * b0 + a5 * b2 + b4,
        a4 * b1 + a5 * b3 + b5,
    )


def _fragments(page) -> list[tuple[str, float, float, float]]:
    """(text, x, y, font_size) for each text drawing operation on the page."""

    collected: list[tuple[str, float, float, float]] = []

    def visitor(text, cm, tm, _font_dict, font_size):
        if not text:
            return
        matrix = _mult(tm, cm)
        try:
            size = float(font_size)
        except (TypeError, ValueError):
            size = 12.0
        collected.append((str(text), float(matrix[4]), float(matrix[5]), size))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:  # noqa: BLE001 - extraction failure per page
        return []
    return collected


def _find_text_rects(page, before: str) -> list[tuple[float, float, float, float]]:
    """Approximate rectangles for every ``before`` occurrence on the page."""

    rects: list[tuple[float, float, float, float]] = []
    for text, x, y, size in _fragments(page):
        start = text.find(before)
        while start != -1:
            # Average Helvetica glyph width ~0.5em; good enough to anchor a note.
            width = max(6.0, len(before) * size * 0.5)
            x0 = x + start * size * 0.5
            rects.append((x0, y - size * 0.2, x0 + width, y + size))
            start = text.find(before, start + len(before))
    return rects


def annotate_text(source: bytes, *, before: str, after: str) -> tuple[bytes, dict[str, Any]]:
    """Add one note annotation next to one unique ``before`` occurrence."""

    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Text

    try:
        reader = PdfReader(io.BytesIO(source))
    except Exception as exc:  # noqa: BLE001 - corrupt input
        raise PdfRevisionError(f"PDF 无法解析: {exc}") from exc
    if reader.is_encrypted:
        raise PdfRevisionError("加密 PDF 不支持批注候选")

    matches: list[tuple[int, tuple[float, float, float, float]]] = []
    for index, page in enumerate(reader.pages):
        for rect in _find_text_rects(page, before):
            matches.append((index, rect))
    if not matches:
        raise PdfRevisionError(
            "未在 PDF 中找到可批注的目标文本（可能是扫描件、无文本层或文本被拆分成多段，"
            "请改用更短的连续短语）"
        )
    if len(matches) > 1:
        raise PdfRevisionError(
            f"批注目标必须唯一出现（找到 {len(matches)} 处），请提供更精确的文本"
        )

    page_index, rect = matches[0]
    writer = PdfWriter()
    writer.append(reader)
    note = Text(rect=rect, text=f"建议替换为：{after}")
    writer.add_annotation(page_number=page_index, annotation=note)
    output = io.BytesIO()
    writer.write(output)
    audit = {
        "operation": "annotate_text",
        "format": "pdf",
        "page": page_index + 1,
        "before": before,
        "after": after,
        "mode": "annotation",
        "notice": "PDF 原件内容未修改，候选通过批注标出建议替换。",
    }
    return output.getvalue(), audit


__all__ = ["PdfRevisionError", "annotate_text"]
