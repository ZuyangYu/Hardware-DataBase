"""Parse assistant Markdown into a small, JSON-safe export document model.

The chat answer is persisted as Markdown because that is the interchange format
used by the UI.  Binary exporters must not treat that value as an opaque text
blob: tables, headings, lists and code fences have different semantics in
Word, PDF, spreadsheets and presentations.  This module is intentionally
renderer-independent and keeps only the bounded structure needed by those
writers.

``markdown-it-py`` is used as the parser so block/inline precedence and escaped
table cells follow the CommonMark/GFM rules instead of a collection of regular
expressions in each renderer.  The returned dictionaries contain no parser
objects and can therefore be persisted in a result snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from src.result_exports.answer import is_full_html_document


MAX_MARKDOWN_CHARS = 500_000
MAX_MARKDOWN_BLOCKS = 1_000
MAX_TABLE_ROWS = 10_000
MAX_TABLE_COLUMNS = 100
MAX_LIST_ITEMS = 10_000
MAX_INLINE_DEPTH = 32


def _attrs(token: Token) -> dict[str, Any]:
    attrs = token.attrs
    if isinstance(attrs, dict):
        return attrs
    if isinstance(attrs, (list, tuple)):
        return {str(key): value for key, value in attrs if isinstance(key, str)}
    return {}


def _inline_nodes(children: Sequence[Token] | None, *, depth: int = 0) -> list[dict[str, Any]]:
    """Convert markdown-it inline tokens to bounded JSON-safe nodes."""

    if not children or depth > MAX_INLINE_DEPTH:
        return []
    root: list[dict[str, Any]] = []
    stack: list[list[dict[str, Any]]] = [root]
    container_stack: list[dict[str, Any]] = []
    container_types = {
        "em_open": "emphasis",
        "strong_open": "strong",
        "s_open": "strikethrough",
        "link_open": "link",
    }
    closing_types = {
        "em_close",
        "strong_close",
        "s_close",
        "link_close",
    }
    for token in children:
        token_type = token.type
        if token_type in container_types:
            node: dict[str, Any] = {"type": container_types[token_type], "children": []}
            if token_type == "link_open":
                href = _attrs(token).get("href")
                if href:
                    node["href"] = str(href)[:2_000]
            stack[-1].append(node)
            container_stack.append(node)
            stack.append(node["children"])
            continue
        if token_type in closing_types:
            if len(stack) > 1:
                stack.pop()
            if container_stack:
                container_stack.pop()
            continue
        if token_type in {"softbreak", "hardbreak"}:
            stack[-1].append({"type": "break"})
            continue
        if token_type == "text":
            if token.content:
                stack[-1].append({"type": "text", "text": token.content[:MAX_MARKDOWN_CHARS]})
            continue
        if token_type == "code_inline":
            stack[-1].append({"type": "code", "text": token.content[:MAX_MARKDOWN_CHARS]})
            continue
        if token_type == "image":
            attrs = _attrs(token)
            stack[-1].append(
                {
                    "type": "image",
                    "text": token.content[:1_000],
                    "src": str(attrs.get("src") or "")[:2_000],
                }
            )
            continue
        if token_type in {"html_inline", "entity"}:
            # Raw HTML is deliberately rendered as text by all exporters.
            if token.content:
                stack[-1].append({"type": "text", "text": token.content[:MAX_MARKDOWN_CHARS]})
            continue
        # Unknown inline tokens (for example a plugin token) are kept as text
        # so an extension never makes an entire paragraph disappear.
        if token.content:
            stack[-1].append({"type": "text", "text": token.content[:MAX_MARKDOWN_CHARS]})
    return root


def inline_text(nodes: Sequence[dict[str, Any]] | None) -> str:
    """Flatten inline nodes for writers that do not support rich text."""

    if not nodes:
        return ""
    parts: list[str] = []
    for node in nodes:
        node_type = node.get("type")
        if node_type == "break":
            parts.append("\n")
        elif node_type == "image":
            parts.append(str(node.get("text") or ""))
        elif node_type == "code":
            parts.append(str(node.get("text") or ""))
        elif node_type in {"emphasis", "strong", "strikethrough", "link"}:
            parts.append(inline_text(node.get("children")))
        else:
            parts.append(str(node.get("text") or ""))
    return "".join(parts)


def _inline_block(token: Token) -> dict[str, Any]:
    nodes = _inline_nodes(token.children)
    return {"text": inline_text(nodes), "inlines": nodes}


def _first_inline(tokens: Sequence[Token], start: int, end: int) -> dict[str, Any]:
    for index in range(start, min(end, len(tokens))):
        token = tokens[index]
        if token.type == "inline":
            return _inline_block(token)
    return {"text": "", "inlines": []}


def _matching_close(tokens: Sequence[Token], start: int, open_type: str, close_type: str) -> int:
    depth = 0
    for index in range(start, len(tokens)):
        token_type = tokens[index].type
        if token_type == open_type:
            depth += 1
        elif token_type == close_type:
            depth -= 1
            if depth == 0:
                return index
    return len(tokens)


def _list_block(tokens: Sequence[Token], start: int) -> tuple[dict[str, Any], int]:
    opening = tokens[start]
    ordered = opening.type == "ordered_list_open"
    close_type = "ordered_list_close" if ordered else "bullet_list_close"
    end = _matching_close(tokens, start, opening.type, close_type)
    items: list[dict[str, Any]] = []
    item_depth = 0
    item_start: int | None = None
    for index in range(start + 1, min(end, len(tokens))):
        token = tokens[index]
        if token.type == "list_item_open":
            item_depth += 1
            if item_depth == 1:
                item_start = index + 1
        elif token.type == "list_item_close":
            if item_depth == 1 and item_start is not None:
                inline = _first_inline(tokens, item_start, index)
                if inline["text"] or inline["inlines"]:
                    items.append(inline)
                if len(items) >= MAX_LIST_ITEMS:
                    break
                item_start = None
            item_depth = max(0, item_depth - 1)
    attrs = _attrs(opening)
    block: dict[str, Any] = {
        "type": "list",
        "ordered": ordered,
        "items": [{"text": item["text"], "inlines": item["inlines"]} for item in items],
    }
    if ordered and attrs.get("start") is not None:
        try:
            block["start"] = max(1, int(attrs["start"]))
        except (TypeError, ValueError):
            pass
    return block, min(end + 1, len(tokens))


def _quote_block(tokens: Sequence[Token], start: int) -> tuple[dict[str, Any], int]:
    end = _matching_close(tokens, start, "blockquote_open", "blockquote_close")
    lines: list[dict[str, Any]] = []
    for token in tokens[start + 1 : end]:
        if token.type == "inline":
            lines.append(_inline_block(token))
    text = "\n".join(line["text"] for line in lines if line["text"])
    nodes: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if index:
            nodes.append({"type": "break"})
        nodes.extend(line["inlines"])
    return {"type": "quote", "text": text, "inlines": nodes}, min(end + 1, len(tokens))


def _table_block(tokens: Sequence[Token], start: int) -> tuple[dict[str, Any], int]:
    end = _matching_close(tokens, start, "table_open", "table_close")
    header: list[str] = []
    header_inlines: list[list[dict[str, Any]]] = []
    rows: list[list[str]] = []
    row_inlines: list[list[list[dict[str, Any]]]] = []
    alignments: list[str] = []
    current_cells: list[str] | None = None
    current_inline_cells: list[list[dict[str, Any]]] | None = None
    current_header = False
    current_cell: dict[str, Any] | None = None
    for token in tokens[start + 1 : end]:
        if token.type == "tr_open":
            current_cells = []
            current_inline_cells = []
            current_header = False
        elif token.type in {"th_open", "td_open"}:
            current_header = token.type == "th_open"
            current_cell = {"text": "", "inlines": []}
            if current_header and len(alignments) < MAX_TABLE_COLUMNS:
                style = str(_attrs(token).get("style") or "")
                if "center" in style:
                    alignments.append("center")
                elif "right" in style:
                    alignments.append("right")
                else:
                    alignments.append("left")
        elif token.type == "inline" and current_cell is not None:
            current_cell = _inline_block(token)
        elif token.type in {"th_close", "td_close"} and current_cells is not None and current_inline_cells is not None:
            current_cells.append(str((current_cell or {}).get("text") or "")[:MAX_MARKDOWN_CHARS])
            current_inline_cells.append(list((current_cell or {}).get("inlines") or []))
            current_cell = None
        elif token.type == "tr_close" and current_cells is not None and current_inline_cells is not None:
            current_cells = current_cells[:MAX_TABLE_COLUMNS]
            current_inline_cells = current_inline_cells[:MAX_TABLE_COLUMNS]
            if current_header and not header:
                header = current_cells
                header_inlines = current_inline_cells
            elif current_cells:
                rows.append(current_cells)
                row_inlines.append(current_inline_cells)
                if len(rows) >= MAX_TABLE_ROWS:
                    break
            current_cells = None
            current_inline_cells = None
            current_header = False
    widths = [len(header), *(len(row) for row in rows)]
    width = min(MAX_TABLE_COLUMNS, max(widths, default=0))
    if not header and rows:
        width = min(MAX_TABLE_COLUMNS, len(rows[0]))
        header = [f"列{index + 1}" for index in range(width)]
        header_inlines = [[{"type": "text", "text": value}] for value in header]
    header = (header + [""] * width)[:width]
    normalized_rows = [(row + [""] * width)[:width] for row in rows]
    normalized_row_inlines = [
        (inline_row + [[] for _ in range(width)])[:width] for inline_row in row_inlines
    ]
    block = {
        "type": "table",
        "columns": header,
        "rows": normalized_rows,
        "inlines": header_inlines[:width],
        "row_inlines": normalized_row_inlines,
        "alignments": (alignments + ["left"] * width)[:width],
    }
    return block, min(end + 1, len(tokens))


def _parse_tokens(tokens: Sequence[Token], start: int = 0, end: int | None = None) -> list[dict[str, Any]]:
    end = len(tokens) if end is None else min(end, len(tokens))
    blocks: list[dict[str, Any]] = []
    index = start
    while index < end and len(blocks) < MAX_MARKDOWN_BLOCKS:
        token = tokens[index]
        token_type = token.type
        if token_type == "heading_open":
            inline = _first_inline(tokens, index + 1, min(index + 3, end))
            try:
                level = max(1, min(6, int(token.tag[1:])))
            except (ValueError, IndexError):
                level = 1
            blocks.append({"type": "heading", "level": level, **inline})
            index += 3
        elif token_type == "paragraph_open":
            inline = _first_inline(tokens, index + 1, min(index + 3, end))
            if inline["text"] or inline["inlines"]:
                blocks.append({"type": "paragraph", **inline})
            index += 3
        elif token_type in {"bullet_list_open", "ordered_list_open"}:
            block, index = _list_block(tokens, index)
            if block["items"]:
                blocks.append(block)
        elif token_type == "blockquote_open":
            block, index = _quote_block(tokens, index)
            if block["text"]:
                blocks.append(block)
        elif token_type == "table_open":
            block, index = _table_block(tokens, index)
            if block["columns"]:
                blocks.append(block)
        elif token_type in {"fence", "code_block"}:
            info = str(token.info or "").strip().split(None, 1)[0] if token_type == "fence" else ""
            code_text = str(token.content or "")[:MAX_MARKDOWN_CHARS]
            if is_full_html_document(code_text):
                index += 1
                continue
            blocks.append(
                {
                    "type": "code",
                    "language": info[:80],
                    "text": code_text,
                }
            )
            index += 1
        elif token_type == "hr":
            blocks.append({"type": "rule"})
            index += 1
        elif token_type == "html_block":
            text = str(token.content or "").strip()
            if text and not is_full_html_document(text):
                blocks.append({"type": "paragraph", "text": text[:MAX_MARKDOWN_CHARS], "inlines": [{"type": "text", "text": text[:MAX_MARKDOWN_CHARS]}]})
            index += 1
        else:
            index += 1
    return blocks


def parse_markdown_blocks(value: str, *, max_chars: int = MAX_MARKDOWN_CHARS) -> list[dict[str, Any]]:
    """Return bounded block dictionaries for a Markdown answer.

    The parser is fail-soft: malformed or unsupported Markdown is represented
    as a paragraph rather than causing an export job to fail.  The input is
    capped before parsing so an unexpectedly verbose model answer cannot make
    every format renderer allocate unbounded state.
    """

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    try:
        limit = max(0, min(int(max_chars), MAX_MARKDOWN_CHARS))
    except (TypeError, ValueError):
        limit = MAX_MARKDOWN_CHARS
    text = text[:limit]
    if not text.strip():
        return []
    try:
        parser = MarkdownIt("commonmark", {"breaks": True, "html": False}).enable("table")
        return _parse_tokens(parser.parse(text))
    except Exception:
        # Export is a delivery path; preserve the answer even if an extension
        # or a future parser version emits an unexpected token shape.
        return [{"type": "paragraph", "text": text, "inlines": [{"type": "text", "text": text}]}]


__all__ = [
    "MAX_MARKDOWN_BLOCKS",
    "MAX_MARKDOWN_CHARS",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
    "inline_text",
    "is_full_html_document",
    "parse_markdown_blocks",
]
