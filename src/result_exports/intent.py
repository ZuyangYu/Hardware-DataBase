"""Server-owned export intent recognition and validation.

The browser may observe a completed turn, but it must not be the source of
truth for an export request. This module intentionally recognizes only
explicit output language; ordinary mentions of PDF/Excel remain retrieval or
conversation requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.intent import classify_intent
from src.result_exports.models import is_export_format_enabled, normalize_export_format


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


def is_template_generation_intent(query: str) -> bool:
    """Whether a request targets a filled template artifact.

    Such a request must be handled by Document Flow.  Returning a generic
    ``ExportPlan`` for it would enqueue a second job that merely serializes
    the chat transcript, which is the failure mode this guard prevents.
    """

    return classify_intent(query).intent == "template_generation"


def infer_export_intent(query: str) -> ExportPlan | None:
    """Return a conservative export plan for an explicit user request.

    This is a compatibility fallback for models that do not emit a structured
    ``declare_export_request`` call. It is deliberately fail-closed: no
    action word or a simple negation means no export job.
    """

    text = str(query or "").strip()
    plan = classify_intent(text)
    if not text or not plan.export_requested:
        return None
    formats = tuple(
        normalize_export_format(format_name)
        for format_name in plan.requested_formats
        if is_export_format_enabled(format_name)
    )
    if not formats:
        return None
    # An isolated spreadsheet request can be rendered as structured data;
    # mixed requests keep report semantics so the answer and evidence remain
    # available in every requested format.
    content_shape = "data" if formats == ("xlsx",) else "report"
    return ExportPlan(formats=formats, content_shape=content_shape)
