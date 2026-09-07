"""Normalize model answers before they become durable export content.

The export pipeline owns file creation.  A model may still emit an old
browser-print workaround (or a stale answer may be re-exported), so this
module removes only the recognizable full-HTML fallback and replaces its
contradictory status text with the server-owned asynchronous export notice.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_FORMAT_LABELS = {
    "md": "Markdown",
    "markdown": "Markdown",
    "xlsx": "Excel",
    "excel": "Excel",
    "docx": "Word",
    "word": "Word",
    "pdf": "PDF",
    "pptx": "PowerPoint",
    "powerpoint": "PowerPoint",
}

_HTML_DOCUMENT_RE = re.compile(
    r"(?is)(?:<!doctype\s+html\b|<html\b).*?</html\s*>"
)
_HTML_FENCE_RE = re.compile(
    r"(?ims)^[ \t]*```[^\n]*\n(?P<body>.*?)^[ \t]*```[ \t]*(?:\n|$)"
)
_EXPORT_FAILURE_RE = re.compile(
    r"(?i)(?:无法|不能|不可)(?:直接)?(?:输出|生成|创建|导出)[^。\n]{0,80}"
    r"(?:pdf|word|excel|power\s*point|文件)"
)
_HTML_FALLBACK_RE = re.compile(r"(?i)(?:自包含\s*html|html\s*报告|另存为\s*pdf)")
_EXPORT_INSTRUCTION_RE = re.compile(
    r"(?i)(?:将下方代码|保存为[^\n]{0,80}\.html|用浏览器[^\n]{0,80}"
    r"(?:打开|打印)|按\s*ctrl\s*\+\s*p|打印目标[^\n]{0,80}pdf)"
)
_EXPORT_META_HEADING_RE = re.compile(
    r"(?i)(?:pdf|word|excel|power\s*point|导出|转换).{0,20}(?:报告)?(?:生成|导出|转换)"
)
_ASYNC_STATUS_RE = re.compile(r"(?i)导出任务.*(?:提交|排队|可下载|生成完成)")


def is_full_html_document(value: Any) -> bool:
    """Return whether *value* looks like a complete browser HTML document."""

    text = str(value or "")
    lowered = text.casefold()
    has_root = "<!doctype html" in lowered or re.search(r"<html\b", lowered) is not None
    has_shell = re.search(r"<(?:head|body)\b", lowered) is not None
    has_close = re.search(r"</html\s*>", lowered) is not None
    return bool(has_root and has_shell and has_close)


def _plan_formats(export_plan: Any) -> list[str]:
    if export_plan is None:
        return []
    values: Any
    if isinstance(export_plan, Mapping):
        values = export_plan.get("formats")
    else:
        values = getattr(export_plan, "formats", None)
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        return []
    formats: list[str] = []
    for value in values:
        normalized = str(value or "").strip().lower().lstrip(".")
        if normalized in _FORMAT_LABELS and normalized not in formats:
            formats.append(normalized)
    return formats


def _remove_html_documents(text: str) -> tuple[str, bool]:
    removed = False

    def replace_fence(match: re.Match[str]) -> str:
        nonlocal removed
        body = match.group("body")
        if is_full_html_document(body):
            removed = True
            return "\n"
        return match.group(0)

    text = _HTML_FENCE_RE.sub(replace_fence, text)

    def replace_document(match: re.Match[str]) -> str:
        nonlocal removed
        document = match.group(0)
        if not is_full_html_document(document):
            return document
        removed = True
        return "\n"

    text = _HTML_DOCUMENT_RE.sub(replace_document, text)
    return text, removed


def _status_line(formats: list[str]) -> str:
    labels = [_FORMAT_LABELS[value] for value in formats]
    return f"已提交 {'、'.join(labels)} 导出任务，生成完成后可在下方下载。"


def _clean_fallback_text(text: str, *, had_failure: bool) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if _EXPORT_FAILURE_RE.search(line) or _HTML_FALLBACK_RE.search(line):
            had_failure = True
            continue
        if _EXPORT_INSTRUCTION_RE.search(line):
            had_failure = True
            continue
        if had_failure and _EXPORT_META_HEADING_RE.search(stripped):
            continue
        # A malformed/partial HTML response should not leak document shell
        # markers even when the closing tag was truncated by a model limit.
        if re.match(r"(?i)^\s*(?:<!doctype\s+html|</?html\b|</?(?:head|body)\b)", line):
            had_failure = True
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def strip_export_fallback_markup(answer: str) -> str:
    """Remove a complete HTML fallback without adding an export status line.

    Renderers use this for snapshots created before answer normalization.  It
    deliberately leaves ordinary HTML snippets untouched when they are not a
    browser-ready document.
    """

    text, removed_html = _remove_html_documents(str(answer or ""))
    if not removed_html:
        return str(answer or "")
    cleaned = _clean_fallback_text(text, had_failure=True)
    return f"{cleaned}\n" if cleaned else ""


def normalize_export_answer(answer: str, export_plan: Any = None) -> str:
    """Remove contradictory HTML fallbacks for a server-owned export plan.

    Non-export answers are returned byte-for-byte unchanged.  The operation is
    idempotent so it is safe to apply both at turn completion and when an old
    turn is manually exported.
    """

    text = str(answer or "")
    formats = _plan_formats(export_plan)
    if not formats:
        return text

    had_failure = bool(_EXPORT_FAILURE_RE.search(text) or _HTML_FALLBACK_RE.search(text))
    text, removed_html = _remove_html_documents(text)
    had_failure = had_failure or removed_html

    cleaned = _clean_fallback_text(text, had_failure=had_failure)
    status = _status_line(formats)
    if not _ASYNC_STATUS_RE.search(cleaned):
        cleaned = f"{cleaned}\n\n{status}" if cleaned else status
    return cleaned + "\n"


__all__ = [
    "is_full_html_document",
    "normalize_export_answer",
    "strip_export_fallback_markup",
]
