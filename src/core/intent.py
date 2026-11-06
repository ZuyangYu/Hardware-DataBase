"""Canonical, server-side intent planning for chat turns.

The chat UI may use a lightweight copy of these rules for interaction hints,
but routing and durable export decisions must be made by the server.  The
planner is deliberately conservative: comparison wins over export, and an
export requires an explicit output format.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Literal


IntentName = Literal[
    "general_qa",
    "knowledge_base_qa",
    "attachment_qa",
    "compare",
    "export",
]
IntentAction = Literal["answer", "compare", "export", "clarify"]
IntentTarget = Literal[
    "conversation",
    "knowledge_base",
    "attachment",
    "document",
    "report",
    "unknown",
]


_COMPARISON_PATTERN = re.compile(
    r"对比|比较|区别|差异|差别|异同|对照|compare|comparison|difference|diff",
    re.IGNORECASE,
)
_EXPORT_ACTION_PATTERN = re.compile(
    r"导出|输出|整理成|转换为|下载|保存为|另存为|生成|制作|"
    r"export|output|download|convert|save\s+as|generate|produce",
    re.IGNORECASE,
)
_DOCUMENT_TARGET_PATTERN = re.compile(
    r"模板|模版|文档|文件|报告|说明书|设计说明|规范|手册|工单|ICD|表格|表单|"
    r"template|document|report|specification|manual|guide|sheet|form|work\s*order",
    re.IGNORECASE,
)
_EXPORT_NEGATION_PATTERN = re.compile(
    r"(?:不要|无需|不需要|别|禁止).{0,8}(?:导出|输出|生成|下载|保存|转换)",
    re.IGNORECASE,
)
_CURRENT_RESULT_PATTERN = re.compile(
    r"(?:当前|刚才|上述|本次|这个|this|current|previous|above)"
    r".{0,24}(?:结果|回答|答案|result|answer|response)",
    re.IGNORECASE,
)
_RESULT_EXPORT_SOURCE_PATTERN = re.compile(
    r"检索结果|查询结果|搜索结果|当前结果|刚才结果|本次结果|"
    r"结果|答案|回答|result|answer|response",
    re.IGNORECASE,
)

# Only explicit format names are output formats.  The generic Chinese word
# “文档” is intentionally not treated as DOCX; it is commonly the subject of
# a request such as “将对比结果整理成 PDF”.
_FORMAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("md", re.compile(r"markdown|(?<![A-Za-z0-9])md(?![A-Za-z0-9])", re.IGNORECASE)),
    ("xlsm", re.compile(r"(?<![A-Za-z0-9])xlsm(?![A-Za-z0-9])", re.IGNORECASE)),
    ("xlsx", re.compile(r"excel|xlsx|电子表格", re.IGNORECASE)),
    ("docx", re.compile(r"(?<![A-Za-z0-9])(?:word|woed|docx)(?![A-Za-z0-9])", re.IGNORECASE)),
    ("pdf", re.compile(r"(?<![A-Za-z0-9])pdf(?![A-Za-z0-9])", re.IGNORECASE)),
    ("pptx", re.compile(r"power\s*point|(?<![A-Za-z0-9])pptx?(?![A-Za-z0-9])|演示文稿|幻灯片", re.IGNORECASE)),
)
_UNSUPPORTED_OUTPUT_FORMAT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:txt|csv|odt|rtf|html?|json|xml)(?![A-Za-z0-9])|"
    r"纯文本|网页文件|文本文件",
    re.IGNORECASE,
)
# draw.io files are produced by the MCP tool, not by the document-export
# pipeline.  A request such as “生成 .drawio XML” must reach the agent instead
# of being classified as an unsupported document output format.
_DRAWIO_FILE_PATTERN = re.compile(r"\.drawio\b|draw\.io|\bdrawio\b", re.IGNORECASE)


@dataclass(frozen=True)
class IntentPlan:
    """A small, serializable plan shared by routing and export decisions."""

    intent: IntentName
    action: IntentAction
    target: IntentTarget
    comparison_requested: bool = False
    export_requested: bool = False
    requested_formats: tuple[str, ...] = ()
    confidence: float = 0.0
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["requested_formats"] = list(self.requested_formats)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def is_document_comparison_query(query: str) -> bool:
    """Return whether the query explicitly asks for a comparison/diff."""

    return bool(_COMPARISON_PATTERN.search(str(query or "").strip()))


def extract_requested_formats(query: str) -> tuple[str, ...]:
    """Extract explicit output format names in stable UI order."""

    text = str(query or "").strip()
    if not text:
        return ()
    return tuple(
        format_name
        for format_name, pattern in _FORMAT_PATTERNS
        if pattern.search(text)
    )


def _target_for(text: str, *, has_attachments: bool, has_kb: bool) -> IntentTarget:
    if re.search(r"工单|work\s*order", text, re.IGNORECASE):
        return "document"
    if re.search(r"ICD|报告|report", text, re.IGNORECASE):
        return "report"
    if _DOCUMENT_TARGET_PATTERN.search(text):
        return "document"
    if has_attachments:
        return "attachment"
    if has_kb:
        return "knowledge_base"
    return "conversation"


def classify_intent(
    query: str,
    *,
    has_attachments: bool = False,
    has_kb: bool = True,
) -> IntentPlan:
    """Classify one chat query using precedence-safe deterministic rules.

    Comparison is checked before export so phrases such as “对比后导出 PDF”
    remain an evidence-comparison request with an optional delivery format.
    A generic export is only recognized when an explicit format is present;
    mentioning a PDF while asking a question is retrieval.
    """

    text = str(query or "").strip()
    if not text:
        return IntentPlan(
            intent="general_qa",
            action="answer",
            target="conversation",
            confidence=1.0,
            reason_codes=("empty_query",),
        )

    formats = extract_requested_formats(text)
    target = _target_for(text, has_attachments=has_attachments, has_kb=has_kb)
    # draw.io is produced by the MCP tool, never by the document-export
    # pipeline.  Incidental format-shaped text (for example a reference path
    # such as references/02-block-diagram.md) must not turn the request into
    # an unsupported-export clarification.
    if _DRAWIO_FILE_PATTERN.search(text):
        return IntentPlan(
            intent="knowledge_base_qa" if has_kb else "general_qa",
            action="answer",
            target=target,
            confidence=0.82,
            reason_codes=("drawio_file_request",),
        )

    if is_document_comparison_query(text):
        return IntentPlan(
            intent="compare",
            action="compare",
            target=target,
            comparison_requested=True,
            export_requested=bool(formats),
            requested_formats=formats,
            confidence=0.99,
            reason_codes=("comparison_requested",),
        )

    explicit_export = bool(
        formats
        and _EXPORT_ACTION_PATTERN.search(text)
        and not _EXPORT_NEGATION_PATTERN.search(text)
    )
    # “整理成” is also a valid delivery verb for an already-produced result.
    # If the user explicitly points at a result/answer, the requested artifact
    # is a generic result export.
    result_export_command = bool(
        explicit_export
        and _RESULT_EXPORT_SOURCE_PATTERN.search(text)
        and not re.search(r"模板|模版|template", text, re.IGNORECASE)
    )
    if result_export_command:
        if len(formats) > 1:
            return IntentPlan(
                intent="export",
                action="clarify",
                target="conversation",
                export_requested=True,
                requested_formats=formats,
                confidence=0.9,
                reason_codes=("multiple_output_formats",),
            )
        return IntentPlan(
            intent="export",
            action="export",
            target="conversation",
            export_requested=True,
            requested_formats=formats,
            confidence=0.96,
            reason_codes=("result_delivery_export",),
        )
    ambiguous_output_format = bool(
        explicit_export
        and (
            len(formats) > 1
            or _UNSUPPORTED_OUTPUT_FORMAT_PATTERN.search(text)
        )
    )
    if ambiguous_output_format:
        return IntentPlan(
            intent="export",
            action="clarify",
            target="conversation",
            export_requested=True,
            requested_formats=formats,
            confidence=0.9,
            reason_codes=(
                "multiple_output_formats"
                if len(formats) > 1
                else "unsupported_output_format",
            ),
        )
    if (
        _EXPORT_ACTION_PATTERN.search(text)
        and _UNSUPPORTED_OUTPUT_FORMAT_PATTERN.search(text)
        and not _EXPORT_NEGATION_PATTERN.search(text)
    ):
        return IntentPlan(
            intent="export",
            action="clarify",
            target="conversation",
            export_requested=True,
            requested_formats=formats,
            confidence=0.9,
            reason_codes=("unsupported_output_format",),
        )

    if explicit_export:
        return IntentPlan(
            intent="export",
            action="export",
            target="conversation",
            export_requested=True,
            requested_formats=formats,
            confidence=0.96,
            reason_codes=(
                "result_delivery_export"
                if _CURRENT_RESULT_PATTERN.search(text)
                else "explicit_output_format",
            ),
        )

    if has_attachments:
        return IntentPlan(
            intent="attachment_qa",
            action="answer",
            target="attachment",
            confidence=0.78,
            reason_codes=("attachment_context",),
        )
    if has_kb:
        return IntentPlan(
            intent="knowledge_base_qa",
            action="answer",
            target="knowledge_base",
            confidence=0.72,
            reason_codes=("knowledge_base_context",),
        )
    return IntentPlan(
        intent="general_qa",
        action="answer",
        target="conversation",
        confidence=0.65,
        reason_codes=("no_source_context",),
    )


__all__ = [
    "IntentAction",
    "IntentName",
    "IntentPlan",
    "IntentTarget",
    "classify_intent",
    "extract_requested_formats",
    "is_document_comparison_query",
]
