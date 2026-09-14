"""Deterministic, auditable DOCX edits for chat attachment revisions.

Only two contract-level operations are supported, and both are exact by
construction so the server never has to guess what the user meant:

- ``replace_text``: a unique text occurrence (inside one paragraph or table
  cell) is replaced.  Surrounding runs keep their original formatting; the
  replacement adopts the formatting of the run where the match starts.
- ``replace_section``: the body under one heading is replaced.  The heading
  paragraph itself, every other section and all non-body content (styles,
  headers/footers, embedded objects outside the section) stay untouched.

The parent document is never mutated: callers receive new bytes plus a JSON
audit record describing the target, the removed range and the inserted
paragraphs.  Ambiguous or missing targets raise ``DocxRevisionError`` so the
candidate is never created from a guess.
"""

from __future__ import annotations

import io
import re
from typing import Any, Iterator


class DocxRevisionError(ValueError):
    """Raised when a DOCX edit target is missing, ambiguous or unsupported."""


_HEADING_NAME_RE = re.compile(
    r"(?:heading|标题|標題)\s*([0-9]+)", re.IGNORECASE
)


def _outline_level_from_pPr(pPr: Any) -> int | None:
    """Read a ``w:outlineLvl`` child (0-based) as a 1-based heading level."""

    from docx.oxml.ns import qn

    if pPr is None:
        return None
    node = pPr.find(qn("w:outlineLvl"))
    if node is None:
        return None
    try:
        return max(1, int(node.get(qn("w:val"))) + 1)
    except (TypeError, ValueError):
        return None


def _outline_level_from_style(style_element: Any) -> int | None:
    from docx.oxml.ns import qn

    if style_element is None:
        return None
    return _outline_level_from_pPr(style_element.find(qn("w:pPr")))


def heading_level(paragraph: Any) -> int | None:
    """Return the 1-based outline level when the paragraph is a heading.

    Corporate documents rarely name their styles ``Heading N``: localized or
    custom styles (for example ``样式1—标题1``) are usually based on a built-in
    heading style.  Detection therefore checks, in order: a direct paragraph
    outline level, the style name, and the inherited style chain with its
    outline levels.  Returns ``None`` for ordinary body paragraphs.
    """

    pPr = getattr(getattr(paragraph, "_p", None), "pPr", None)
    level = _outline_level_from_pPr(pPr)
    if level is not None:
        return level

    style = getattr(paragraph, "style", None)
    seen: set[str] = set()
    while style is not None:
        style_id = str(getattr(style, "style_id", "") or "")
        if style_id and style_id in seen:
            break
        if style_id:
            seen.add(style_id)
        name = str(getattr(style, "name", "") or "")
        match = _HEADING_NAME_RE.search(name)
        if match:
            return max(1, int(match.group(1)))
        level = _outline_level_from_style(getattr(style, "element", None))
        if level is not None:
            return level
        try:
            style = style.base_style
        except Exception:  # noqa: BLE001 - broken style inheritance
            break
    return None


def _load(source: bytes):
    from docx import Document  # python-docx

    return Document(io.BytesIO(source))


def _save(document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paragraph_text(paragraph) -> str:
    return "".join(run.text or "" for run in paragraph.runs)


def _is_heading(paragraph) -> bool:
    return heading_level(paragraph) is not None


def _heading_level(paragraph) -> int:
    return heading_level(paragraph) or 1


def _heading_outline(document) -> Iterator[tuple[Any, int, list[str]]]:
    """Yield (paragraph, level, full_heading_path) for every body heading."""

    stack: list[tuple[int, str]] = []
    for paragraph in document.paragraphs:
        if not _is_heading(paragraph):
            continue
        text = _paragraph_text(paragraph).strip()
        if not text:
            continue
        level = _heading_level(paragraph)
        stack = stack[: max(0, level - 1)]
        full_path = [item[1] for item in stack] + [text]
        yield paragraph, level, full_path
        stack.append((level, text))


def _iter_paragraphs(document) -> Iterator[Any]:
    """Iterate body paragraphs plus table-cell paragraphs, de-duplicated."""

    seen: set[int] = set()

    def _emit(paragraph):
        key = id(paragraph._p)
        if key in seen:
            return None
        seen.add(key)
        return paragraph

    for paragraph in document.paragraphs:
        candidate = _emit(paragraph)
        if candidate is not None:
            yield candidate
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    candidate = _emit(paragraph)
                    if candidate is not None:
                        yield candidate


def _has_drawing(paragraph) -> bool:
    from docx.oxml.ns import qn

    return paragraph._p.find(f".//{qn('w:drawing')}") is not None


def _replace_span(paragraph, start: int, end: int, after: str) -> None:
    """Replace ``[start, end)`` of the paragraph's run text with ``after``."""

    inserted = False
    position = 0
    for run in paragraph.runs:
        text = run.text or ""
        run_start = position
        run_end = position + len(text)
        position = run_end
        if run_end <= start or run_start >= end:
            continue
        local_start = max(start, run_start) - run_start
        local_end = min(end, run_end) - run_start
        if not inserted:
            run.text = f"{text[:local_start]}{after}{text[local_end:]}"
            inserted = True
        else:
            run.text = f"{text[:local_start]}{text[local_end:]}"
    if not inserted:
        raise DocxRevisionError("replacement target runs could not be located")


def _paragraphs_after(paragraph, lines: list[str], style: Any = None):
    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    cursor = paragraph._p
    for line in lines:
        new_element = OxmlElement("w:p")
        cursor.addnext(new_element)
        cursor = new_element
        new_paragraph = Paragraph(new_element, paragraph._parent)
        target_style = style if style is not None else paragraph.style
        if target_style is not None:
            try:
                new_paragraph.style = target_style
            except Exception:  # noqa: BLE001 - style may be unavailable
                pass
        if line:
            new_paragraph.add_run(line)


def _replace_block_with_lines(paragraph, lines: list[str]) -> None:
    """Replace a whole paragraph, expanding ``after`` lines into paragraphs."""

    if _has_drawing(paragraph):
        raise DocxRevisionError(
            "the target paragraph contains an embedded object; "
            "whole-paragraph replacement would drop it"
        )
    first, rest = lines[0], lines[1:]
    runs = paragraph.runs
    if runs:
        runs[0].text = first
        for run in runs[1:]:
            run.text = ""
    elif first:
        paragraph.add_run(first)
    _paragraphs_after(paragraph, rest, style=paragraph.style)


def replace_text_in_docx(source: bytes, before: str, after: str) -> tuple[bytes, dict[str, Any]]:
    """Replace one unique text occurrence and return ``(child_bytes, audit)``."""

    document = _load(source)
    matches: list[tuple[Any, int, str, bool]] = []
    for paragraph in _iter_paragraphs(document):
        text = _paragraph_text(paragraph)
        start = text.find(before)
        while start != -1:
            whole_block = text.strip() == before.strip()
            matches.append((paragraph, start, text, whole_block))
            start = text.find(before, start + 1)
    if not matches:
        raise DocxRevisionError("replacement target was not found in the document")
    if len(matches) > 1:
        raise DocxRevisionError(
            f"replacement target must occur exactly once (found {len(matches)})"
        )

    paragraph, start, text, whole_block = matches[0]
    multi_line = "\n" in after or "\r" in after
    if multi_line and not whole_block:
        raise DocxRevisionError(
            "multi-line replacement requires the target to be a whole paragraph"
        )
    if whole_block and _has_drawing(paragraph):
        raise DocxRevisionError(
            "the target paragraph contains an embedded object; "
            "replacing its text would drop it"
        )
    if multi_line:
        lines = after.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        _replace_block_with_lines(paragraph, lines)
        mode = "block"
    else:
        _replace_span(paragraph, start, start + len(before), after)
        mode = "span"
    audit = {
        "operation": "replace_text",
        "mode": mode,
        "before_length": len(before),
        "after_length": len(after),
        "occurrences": 1,
    }
    return _save(document), audit


def _elements_text(elements: list[Any]) -> str:
    from docx.oxml.ns import qn

    lines: list[str] = []
    for element in elements:
        if element.tag == qn("w:p"):
            text = "".join(node.text or "" for node in element.iter(qn("w:t"))).strip()
            if text:
                lines.append(text)
        elif element.tag == qn("w:tbl"):
            for row in element.iter(qn("w:tr")):
                cells = [
                    "".join(node.text or "" for node in cell.iter(qn("w:t"))).strip()
                    for cell in row.iter(qn("w:tc"))
                ]
                lines.append(" | ".join(cell for cell in cells if cell))
    return "\n".join(lines)


def _count_drawings(elements: list[Any]) -> int:
    from docx.oxml.ns import qn

    return sum(1 for element in elements for _ in element.iter(qn("w:drawing")))


def _find_section_heading(document, heading_path: list[str]):
    path = [str(item).strip() for item in (heading_path or []) if str(item).strip()]
    if not path:
        raise DocxRevisionError("a section edit requires at least one heading")
    target = path[-1]
    prefix = path[:-1]

    matches: list[tuple[Any, int, list[str]]] = []
    for paragraph, level, full_path in _heading_outline(document):
        if full_path[-1] != target and target not in full_path[-1]:
            continue
        if prefix and full_path[: len(prefix)] != prefix:
            continue
        matches.append((paragraph, level, full_path))
    if not matches:
        raise DocxRevisionError(f"section heading was not found: {target}")
    exact = [match for match in matches if match[2][-1] == target]
    candidates = exact or matches
    if len(candidates) > 1:
        raise DocxRevisionError(
            f"section heading is ambiguous (found {len(candidates)}): {target}"
        )
    return candidates[0]


def replace_section_in_docx(
    source: bytes,
    heading_path: list[str],
    after: str,
) -> tuple[bytes, dict[str, Any]]:
    """Replace one heading's body and return ``(child_bytes, audit)``.

    The heading paragraph keeps its identity and formatting.  Everything from
    the element after the heading up to (excluding) the next heading of the
    same or a higher level is removed, then ``after`` is inserted as one
    paragraph per line, copying the first removed paragraph's style.
    """

    from docx.oxml.ns import qn

    document = _load(source)
    heading, level, full_path = _find_section_heading(document, heading_path)

    body = document.element.body
    siblings = list(body.iterchildren())
    start_index = siblings.index(heading._p)
    removed: list[Any] = []
    for element in siblings[start_index + 1 :]:
        if element.tag == qn("w:p"):
            from docx.text.paragraph import Paragraph

            candidate = Paragraph(element, heading._parent)
            if _is_heading(candidate) and _heading_level(candidate) <= level:
                break
        if element.tag == qn("w:sectPr"):
            break
        removed.append(element)

    removed_text = _elements_text(removed)
    removed_drawings = _count_drawings(removed)
    removed_paragraphs = sum(1 for element in removed if element.tag == qn("w:p"))
    removed_tables = sum(1 for element in removed if element.tag == qn("w:tbl"))
    body_style: Any = None
    for element in removed:
        if element.tag != qn("w:p"):
            continue
        from docx.text.paragraph import Paragraph

        candidate = Paragraph(element, heading._parent)
        if candidate.style is not None:
            body_style = candidate.style
            break
    if body_style is None:
        # An empty section must not inherit the heading style for its new
        # body; fall back to the document default.
        try:
            body_style = document.styles["Normal"]
        except KeyError:
            body_style = None

    for element in removed:
        body.remove(element)

    lines = after.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    _paragraphs_after(heading, lines, style=body_style)

    audit = {
        "operation": "replace_section",
        "heading_path": full_path,
        "heading_level": level,
        "removed_paragraphs": removed_paragraphs,
        "removed_tables": removed_tables,
        "removed_drawings": removed_drawings,
        "inserted_paragraphs": len(lines),
        # Consumed by the service for the revision's ``before`` audit field;
        # never persisted twice.
        "removed_text": removed_text,
    }
    return _save(document), audit


__all__ = [
    "DocxRevisionError",
    "replace_section_in_docx",
    "replace_text_in_docx",
]
