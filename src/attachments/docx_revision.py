"""Deterministic, auditable DOCX edits for chat attachment revisions.

Only two contract-level operations are supported, and both are exact by
construction so the server never has to guess what the user meant:

- ``replace_text``: a unique text occurrence (inside one paragraph or table
  cell) is replaced.  Surrounding runs keep their original formatting; the
  replacement adopts the formatting of the run where the match starts.
- ``replace_section``: the body under one heading is replaced.  The heading
  paragraph itself, every other section and all non-body content (styles,
  headers/footers, embedded objects outside the section) stay untouched.  New
  paragraphs inherit the original body style and its first run's direct
  character formatting; tables and embedded objects (pictures, OLE objects)
  inside the section are moved to the end of the new body instead of being
  dropped unless the caller explicitly opts into ``preserve_embedded=False``.

The parent document is never mutated: callers receive new bytes plus a JSON
audit record describing the target, the removed range and the inserted
paragraphs.  Ambiguous or missing targets raise ``DocxRevisionError`` so the
candidate is never created from a guess.
"""

from __future__ import annotations

import io
import re
from copy import deepcopy
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


_EMBEDDED_TAGS = ("w:drawing", "w:object", "w:pict", "w:oleObject")


def _embedded_tag_names() -> set[str]:
    from docx.oxml.ns import qn

    return {qn(tag) for tag in _EMBEDDED_TAGS}


def _element_embedded_count(element: Any) -> int:
    tags = _embedded_tag_names()
    return sum(1 for node in element.iter() if node.tag in tags)


def _paragraph_has_embedded(paragraph) -> bool:
    return _element_embedded_count(paragraph._p) > 0


def _embedded_run_elements(paragraph) -> list[Any]:
    """Return the ``w:r`` elements carrying drawings/OLE objects."""

    from docx.oxml.ns import qn

    tags = _embedded_tag_names()
    return [
        run_element
        for run_element in paragraph._p.iter(qn("w:r"))
        if any(node.tag in tags for node in run_element.iter())
    ]


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


_PLACEHOLDER_RE = re.compile(
    r"^\s*\[\[\s*(?:embed|嵌入(?:对象)?)\s*(?::|：)?\s*(\d*)\s*\]\]\s*$",
    re.IGNORECASE,
)
_PPR_EXCLUDED_TAGS = ("w:sectPr", "w:pageBreakBefore")


def _sanitized_paragraph_properties(pPr: Any) -> Any:
    """Copy direct paragraph formatting without section/page-break controls."""

    from docx.oxml.ns import qn

    if pPr is None:
        return None
    copy = deepcopy(pPr)
    for tag in _PPR_EXCLUDED_TAGS:
        for node in copy.findall(qn(tag)):
            copy.remove(node)
    return copy


def _new_paragraph_after(
    cursor_element,
    parent,
    line: str,
    style: Any = None,
    run_properties: Any = None,
    paragraph_properties: Any = None,
):
    """Create one ``w:p`` after ``cursor_element`` and return the element."""

    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    element = OxmlElement("w:p")
    if paragraph_properties is not None:
        element.append(deepcopy(paragraph_properties))
    cursor_element.addnext(element)
    new_paragraph = Paragraph(element, parent)
    if style is not None:
        try:
            new_paragraph.style = style
        except Exception:  # noqa: BLE001 - style may be unavailable
            pass
    if line:
        run = new_paragraph.add_run(line)
        if run_properties is not None:
            # ``w:rPr`` must be the first child of ``w:r``: the new run
            # only carries ``w:t`` at this point.
            run._r.insert(0, deepcopy(run_properties))
    return element


def _paragraphs_after(
    paragraph,
    lines: list[str],
    style: Any = None,
    run_properties: Any = None,
    paragraph_properties: Any = None,
):
    """Insert one paragraph per line after ``paragraph``.

    Returns the last inserted ``w:p`` element (``None`` for empty input) so
    callers can continue appending content after the new body.
    """

    cursor = paragraph._p
    last_element: Any = None
    for line in lines:
        last_element = _new_paragraph_after(
            cursor,
            paragraph._parent,
            line,
            style=style if style is not None else paragraph.style,
            run_properties=run_properties,
            paragraph_properties=paragraph_properties,
        )
        cursor = last_element
    return last_element


def _replace_block_with_lines(paragraph, lines: list[str]) -> None:
    """Replace a whole paragraph, expanding ``after`` lines into paragraphs."""

    from docx.oxml.ns import qn

    if _paragraph_has_embedded(paragraph):
        raise DocxRevisionError(
            "the target paragraph contains an embedded object; "
            "whole-paragraph replacement would drop it"
        )
    first, rest = lines[0], lines[1:]
    runs = paragraph.runs
    run_properties = None
    if runs:
        runs[0].text = first
        run_properties = runs[0]._r.find(qn("w:rPr"))
        for run in runs[1:]:
            run.text = ""
    elif first:
        paragraph.add_run(first)
    _paragraphs_after(
        paragraph,
        rest,
        style=paragraph.style,
        run_properties=deepcopy(run_properties) if run_properties is not None else None,
        paragraph_properties=_sanitized_paragraph_properties(paragraph._p.pPr),
    )


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
    if whole_block and _paragraph_has_embedded(paragraph):
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
    preserve_embedded: bool = True,
) -> tuple[bytes, dict[str, Any]]:
    """Replace one heading's body and return ``(child_bytes, audit)``.

    The heading paragraph keeps its identity and formatting.  Everything from
    the element after the heading up to (excluding) the next heading of the
    same or a higher level is removed, then ``after`` is inserted as one
    paragraph per line.

    New paragraphs copy the first removed body paragraph's style, its direct
    paragraph formatting (indentation, spacing, alignment, ...) and its first
    run's direct character formatting (font name, size, bold, colour, ...), so
    the replacement text matches the original section formatting.  With
    ``preserve_embedded=True`` (the default) tables and runs carrying embedded
    objects (pictures, OLE objects) are preserved.  A placeholder line
    (``[[embed]]`` or ``[[embed:N]]``, N = 1-based original order) puts the next
    or the numbered object at that position; everything not placed stays, in
    original order, at the end of the new body.  The audit reports both the
    removal and the preservation counts.
    """

    from docx.oxml.ns import qn

    document = _load(source)
    heading, level, full_path = _find_section_heading(document, heading_path)

    body = document.element.body
    siblings = list(body.iterchildren())
    start_index = siblings.index(heading._p)
    section_elements: list[Any] = []
    for element in siblings[start_index + 1 :]:
        if element.tag == qn("w:p"):
            from docx.text.paragraph import Paragraph

            candidate = Paragraph(element, heading._parent)
            if _is_heading(candidate) and _heading_level(candidate) <= level:
                break
        if element.tag == qn("w:sectPr"):
            break
        section_elements.append(element)

    # Format samples must be read before anything is detached: the first body
    # paragraph with a style and the first run with direct character
    # formatting represent "the original format" of this section.
    body_style: Any = None
    run_properties: Any = None
    paragraph_properties: Any = None
    for element in section_elements:
        if element.tag != qn("w:p"):
            continue
        from docx.text.paragraph import Paragraph

        candidate = Paragraph(element, heading._parent)
        if body_style is None and candidate.style is not None:
            body_style = candidate.style
        if paragraph_properties is None:
            paragraph_properties = _sanitized_paragraph_properties(element.pPr)
        if run_properties is None:
            for run_element in element.iter(qn("w:r")):
                found = run_element.find(qn("w:rPr"))
                if found is not None:
                    run_properties = deepcopy(found)
                    break
        if (
            body_style is not None
            and run_properties is not None
            and paragraph_properties is not None
        ):
            break
    if body_style is None:
        # An empty section must not inherit the heading style for its new
        # body; fall back to the document default.
        try:
            body_style = document.styles["Normal"]
        except KeyError:
            body_style = None

    removed: list[Any] = []
    preserved: list[tuple[str, Any, list[Any]]] = []
    removed_paragraphs = 0
    preserved_paragraphs = 0
    preserved_tables = 0
    preserved_drawings = 0
    for element in section_elements:
        from docx.text.paragraph import Paragraph

        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, heading._parent)
            embedded_runs = (
                _embedded_run_elements(paragraph) if preserve_embedded else []
            )
            if embedded_runs:
                preserved.append(("runs", paragraph, embedded_runs))
                preserved_paragraphs += 1
                preserved_drawings += sum(
                    _element_embedded_count(run_element)
                    for run_element in embedded_runs
                )
                continue
            removed.append(element)
            removed_paragraphs += 1
            continue
        if element.tag == qn("w:tbl") and preserve_embedded:
            preserved.append(("table", element, []))
            preserved_tables += 1
            continue
        removed.append(element)

    # The ``before`` audit field always describes the full original body.
    removed_text = _elements_text(section_elements)
    removed_tables = sum(1 for element in removed if element.tag == qn("w:tbl"))
    removed_drawings = sum(_element_embedded_count(element) for element in removed)

    for element in section_elements:
        body.remove(element)

    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    def _place_item(cursor_element, item):
        kind, element, embedded_runs = item
        if kind == "table":
            cursor_element.addnext(element)
            return element
        holder = OxmlElement("w:p")
        holder_paragraph = Paragraph(holder, heading._parent)
        try:
            holder_paragraph.style = element.style
        except Exception:  # noqa: BLE001 - style may be unavailable
            pass
        for run_element in embedded_runs:
            holder.append(run_element)
        cursor_element.addnext(holder)
        return holder

    lines = after.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    placed_flags = [False] * len(preserved)
    placed_objects = 0
    inserted_paragraphs = 0
    cursor = heading._p
    for line in lines:
        match = _PLACEHOLDER_RE.match(line)
        if match is None:
            if "[[" in line and "]]" in line:
                raise DocxRevisionError(
                    "an object placeholder must occupy its own line "
                    "（每个 [[embed]] 占位符必须单独一行）"
                )
            cursor = _new_paragraph_after(
                cursor,
                heading._parent,
                line,
                style=body_style,
                run_properties=run_properties,
                paragraph_properties=paragraph_properties,
            )
            inserted_paragraphs += 1
            continue
        raw_index = str(match.group(1) or "").strip()
        if raw_index:
            index = int(raw_index)
            if index < 1 or index > len(preserved):
                raise DocxRevisionError(
                    f"object placeholder index out of range: {index} "
                    f"(原节共有 {len(preserved)} 个可保留对象)"
                )
            chosen = index - 1
            if placed_flags[chosen]:
                raise DocxRevisionError(
                    f"object placeholder {index} was used more than once "
                    "（同一对象不能重复放置）"
                )
        else:
            remaining = [i for i, placed in enumerate(placed_flags) if not placed]
            if not remaining:
                raise DocxRevisionError(
                    "there is no preserved object left for this placeholder "
                    "（占位符数量超过原节可保留对象数量）"
                )
            chosen = remaining[0]
        placed_flags[chosen] = True
        placed_objects += 1
        cursor = _place_item(cursor, preserved[chosen])

    for index, item in enumerate(preserved):
        if placed_flags[index]:
            continue
        cursor = _place_item(cursor, item)

    audit = {
        "operation": "replace_section",
        "heading_path": full_path,
        "heading_level": level,
        "embedded_policy": "preserve" if preserve_embedded else "drop",
        "removed_paragraphs": removed_paragraphs,
        "removed_tables": removed_tables,
        "removed_drawings": removed_drawings,
        "preserved_paragraphs": preserved_paragraphs,
        "preserved_tables": preserved_tables,
        "preserved_drawings": preserved_drawings,
        "placed_objects": placed_objects,
        "inserted_paragraphs": inserted_paragraphs,
        # Consumed by the service for the revision's ``before`` audit field;
        # never persisted twice.
        "removed_text": removed_text,
    }
    return _save(document), audit


def append_paragraphs_in_docx(
    source: bytes,
    heading_path: list[str] | None,
    after: str,
) -> tuple[bytes, dict[str, Any]]:
    """Append paragraphs at the end of a section or of the document.

    With ``heading_path`` the paragraphs are inserted after the last element
    of that section (before the next heading of the same or a higher level).
    Without it they go to the end of the document body, before the final
    ``w:sectPr``.  The inserted paragraphs copy the preceding paragraph's
    style, direct paragraph formatting and first run formatting, so appended
    text matches the surrounding body.  Nothing else in the document changes.
    """

    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    document = _load(source)
    body = document.element.body
    full_path: list[str] = []
    level = 0
    heading = None
    if [item for item in (heading_path or []) if str(item).strip()]:
        heading, level, full_path = _find_section_heading(document, heading_path)

    siblings = list(body.iterchildren())
    if heading is not None:
        cursor = heading._p
        for element in siblings[siblings.index(heading._p) + 1 :]:
            if element.tag == qn("w:p"):
                candidate = Paragraph(element, heading._parent)
                if _is_heading(candidate) and _heading_level(candidate) <= level:
                    break
            if element.tag == qn("w:sectPr"):
                break
            cursor = element
        anchor = "section_end"
    else:
        cursor = None
        for element in siblings:
            if element.tag == qn("w:sectPr"):
                break
            cursor = element
        anchor = "document_end"

    sample = None
    if cursor is not None and cursor.tag == qn("w:p"):
        sample = Paragraph(cursor, heading._parent if heading is not None else document)
    elif cursor is not None:
        previous = cursor.getprevious()
        while previous is not None:
            if previous.tag == qn("w:p"):
                sample = Paragraph(
                    previous,
                    heading._parent if heading is not None else document,
                )
                break
            previous = previous.getprevious()

    style: Any = None
    run_properties: Any = None
    paragraph_properties: Any = None
    if sample is not None:
        style = sample.style
        paragraph_properties = _sanitized_paragraph_properties(sample._p.pPr)
        for run_element in sample._p.iter(qn("w:r")):
            found = run_element.find(qn("w:rPr"))
            if found is not None:
                run_properties = deepcopy(found)
                break
    if style is None:
        try:
            style = document.styles["Normal"]
        except KeyError:
            style = None

    lines = after.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if cursor is None:
        sectPr = body.find(qn("w:sectPr"))
        for line in lines:
            element = OxmlElement("w:p")
            if sectPr is not None:
                sectPr.addprevious(element)
            else:
                body.append(element)
            paragraph = Paragraph(element, document)
            if style is not None:
                try:
                    paragraph.style = style
                except Exception:  # noqa: BLE001 - style may be unavailable
                    pass
            if line:
                run = paragraph.add_run(line)
                if run_properties is not None:
                    run._r.insert(0, deepcopy(run_properties))
    else:
        for line in lines:
            cursor = _new_paragraph_after(
                cursor,
                heading._parent if heading is not None else document,
                line,
                style=style,
                run_properties=run_properties,
                paragraph_properties=paragraph_properties,
            )

    audit = {
        "operation": "append_paragraphs",
        "heading_path": full_path,
        "heading_level": level,
        "anchor": anchor,
        "inserted_paragraphs": len(lines),
    }
    return _save(document), audit


__all__ = [
    "DocxRevisionError",
    "append_paragraphs_in_docx",
    "replace_section_in_docx",
    "replace_text_in_docx",
]
