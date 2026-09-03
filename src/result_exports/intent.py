"""Server-owned export intent recognition and validation.

The browser may observe a completed turn, but it must not be the source of
truth for an export request. This module intentionally recognizes only
explicit output language; ordinary mentions of PDF/Excel remain retrieval or
conversation requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from src.result_exports.models import is_export_format_enabled, normalize_export_format


_EXPORT_ACTION_PATTERN = re.compile(
    r"(导出|输出|生成|下载|保存为|转换为|整理成|export|output|generate|download|save\s+as|convert)",
    re.IGNORECASE,
)
_EXPORT_NEGATION_PATTERN = re.compile(
    r"(不要|无需|不需要|别|禁止).{0,6}(导出|输出|生成|下载)",
    re.IGNORECASE,
)
_FORMAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("md", re.compile(r"markdown|\bmd\b", re.IGNORECASE)),
    ("xlsx", re.compile(r"excel|xlsx|电子表格|表格", re.IGNORECASE)),
    ("docx", re.compile(r"word|woed|docx|文档", re.IGNORECASE)),
    ("pdf", re.compile(r"pdf", re.IGNORECASE)),
    ("pptx", re.compile(r"power\s*point|pptx?|演示文稿|幻灯片", re.IGNORECASE)),
)


@dataclass(frozen=True)
class ExportPlan:
    """Validated, serializable intent persisted on a ChatTurn."""

    formats: tuple[str, ...]
    content_shape: str = "report"
    title: str | None = None
    include_citations: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "formats": list(self.formats),
            "content_shape": self.content_shape,
            "title": self.title,
            "include_citations": self.include_citations,
            "options": dict(self.options),
        }


def infer_export_intent(query: str) -> ExportPlan | None:
    """Return a conservative export plan for an explicit user request.

    This is a compatibility fallback for models that do not emit a structured
    ``declare_export_request`` call. It is deliberately fail-closed: no
    action word or a simple negation means no export job.
    """

    text = str(query or "").strip()
    if not text or not _EXPORT_ACTION_PATTERN.search(text) or _EXPORT_NEGATION_PATTERN.search(text):
        return None
    formats = tuple(
        normalize_export_format(format_name)
        for format_name, pattern in _FORMAT_PATTERNS
        if pattern.search(text) and is_export_format_enabled(format_name)
    )
    if not formats:
        return None
    # An isolated spreadsheet request can be rendered as structured data;
    # mixed requests keep report semantics so the answer and evidence remain
    # available in every requested format.
    content_shape = "data" if formats == ("xlsx",) else "report"
    return ExportPlan(formats=formats, content_shape=content_shape)
