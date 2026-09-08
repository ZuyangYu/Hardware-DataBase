"""Canonical, server-side intent planning for chat turns.

The chat UI may use a lightweight copy of these rules for interaction hints,
but routing and durable export decisions must be made by the server.  The
planner is deliberately conservative: comparison wins over generation, an
export requires an explicit output format, and a recommendation follow-up
only means generation when a live template context exists.
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
    "template_generation",
    "document_authoring",
    "export",
]
IntentAction = Literal["answer", "compare", "generate", "export", "clarify"]
IntentTarget = Literal[
    "conversation",
    "knowledge_base",
    "attachment",
    "template",
    "document",
    "report",
    "unknown",
]


_COMPARISON_PATTERN = re.compile(
    r"对比|比较|区别|差异|差别|异同|对照|compare|comparison|difference|diff",
    re.IGNORECASE,
)
_GENERATION_ACTION_PATTERN = re.compile(
    r"生成|创建|填写|填充|回填|出具|制作|撰写|起草|"
    r"generate|create|draft|produce|fill|write",
    re.IGNORECASE,
)
_EXPORT_ACTION_PATTERN = re.compile(
    r"导出|输出|整理成|转换为|下载|保存为|另存为|"
    r"export|output|download|convert|save\s+as",
    re.IGNORECASE,
)
_DOCUMENT_ACTION_PATTERN = re.compile(
    rf"(?:{_GENERATION_ACTION_PATTERN.pattern}|{_EXPORT_ACTION_PATTERN.pattern})",
    re.IGNORECASE,
)
_DOCUMENT_TARGET_PATTERN = re.compile(
    r"模板|模版|文档|文件|报告|工单|ICD|表格|表单|template|document|report|sheet|form|work\s*order",
    re.IGNORECASE,
)
_TEMPLATE_MARKER_PATTERN = re.compile(r"模板|模版|template", re.IGNORECASE)
_RECOMMENDED_PATTERN = re.compile(
    r"按(?:照)?推荐|使用推荐值|直接执行|无需澄清|"
    r"use\s+(?:the\s+)?recommended\s+defaults?",
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
_AMBIGUOUS_DOCUMENT_PATTERN = re.compile(
    r"(?:整理|汇总|组织|compile|整理一下).{0,12}(?:成|为|into|as)?\s*"
    r"(?:文档|文件|document|file)\s*$",
    re.IGNORECASE,
)

# Only explicit format names are output formats.  The generic Chinese word
# “文档” is intentionally not treated as DOCX; it is commonly the subject of
# a request such as “将对比结果整理成 PDF”.
_FORMAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("md", re.compile(r"markdown|(?<![A-Za-z0-9])md(?![A-Za-z0-9])", re.IGNORECASE)),
    ("xlsx", re.compile(r"excel|xlsx|电子表格", re.IGNORECASE)),
    ("docx", re.compile(r"(?<![A-Za-z0-9])(?:word|woed|docx)(?![A-Za-z0-9])", re.IGNORECASE)),
    ("pdf", re.compile(r"(?<![A-Za-z0-9])pdf(?![A-Za-z0-9])", re.IGNORECASE)),
    ("pptx", re.compile(r"power\s*point|(?<![A-Za-z0-9])pptx?(?![A-Za-z0-9])|演示文稿|幻灯片", re.IGNORECASE)),
)


@dataclass(frozen=True)
class IntentPlan:
    """A small, serializable plan shared by routing and export decisions."""

    intent: IntentName
    action: IntentAction
    target: IntentTarget
    template_required: bool = False
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


def _action_before_target(text: str) -> bool:
    return bool(
        re.search(
            rf"(?:{_DOCUMENT_ACTION_PATTERN.pattern})[\s\S]{{0,80}}"
            rf"(?:{_DOCUMENT_TARGET_PATTERN.pattern})",
            text,
            re.IGNORECASE,
        )
    )


def _generation_action_before_target(text: str) -> bool:
    return bool(
        re.search(
            rf"(?:{_GENERATION_ACTION_PATTERN.pattern})[\s\S]{{0,80}}"
            rf"(?:{_DOCUMENT_TARGET_PATTERN.pattern})",
            text,
            re.IGNORECASE,
        )
    )


def _generation_target_before_action(text: str) -> bool:
    return bool(
        re.search(
            rf"(?:{_DOCUMENT_TARGET_PATTERN.pattern})[\s\S]{{0,80}}"
            rf"(?:{_GENERATION_ACTION_PATTERN.pattern})",
            text,
            re.IGNORECASE,
        )
    )


def _target_before_action(text: str) -> bool:
    return bool(
        re.search(
            rf"(?:{_DOCUMENT_TARGET_PATTERN.pattern})[\s\S]{{0,80}}"
            rf"(?:{_DOCUMENT_ACTION_PATTERN.pattern})",
            text,
            re.IGNORECASE,
        )
    )


def _target_for(text: str, *, has_attachments: bool, has_kb: bool) -> IntentTarget:
    if _TEMPLATE_MARKER_PATTERN.search(text):
        return "template"
    if re.search(r"工单|work\s*order", text, re.IGNORECASE):
        return "document"
    if re.search(r"ICD|报告|report", text, re.IGNORECASE):
        return "report"
    if re.search(r"文档|文件|document", text, re.IGNORECASE):
        return "document"
    if has_attachments:
        return "attachment"
    if has_kb:
        return "knowledge_base"
    return "conversation"


def classify_intent(
    query: str,
    *,
    has_template_context: bool = False,
    has_attachments: bool = False,
    has_kb: bool = True,
) -> IntentPlan:
    """Classify one chat query using precedence-safe deterministic rules.

    Precedence is important here: comparison is checked before template
    generation so phrases such as “参考模板比较差异” cannot overwrite or
    fill a template.  A generic export is only recognized when an explicit
    format is present; mentioning a PDF while asking a question is retrieval.
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

    # Safety/semantic precedence: a comparison remains evidence retrieval even
    # when it contains “模板”, “生成” or an explicit PDF output request.
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

    document_command = bool(_action_before_target(text) or _target_before_action(text))
    generation_command = bool(
        _generation_action_before_target(text) or _generation_target_before_action(text)
    )
    recommended_followup = bool(has_template_context and _RECOMMENDED_PATTERN.search(text))
    explicit_template_command = bool(
        _TEMPLATE_MARKER_PATTERN.search(text)
        and document_command
    )
    fill_command = bool(
        re.search(r"填充|填写|回填|fill", text, re.IGNORECASE)
        and document_command
    )
    explicit_export = bool(
        formats
        and _EXPORT_ACTION_PATTERN.search(text)
        and not _EXPORT_NEGATION_PATTERN.search(text)
    )
    # “整理成文档” leaves both the source material and output contract
    # underspecified.  Keep it in the governed authoring route, but expose a
    # clarification action so no worker/job can be started by keyword alone.
    ambiguous_document = bool(
        _AMBIGUOUS_DOCUMENT_PATTERN.search(text)
        and not formats
        and not _TEMPLATE_MARKER_PATTERN.search(text)
    )
    if ambiguous_document:
        return IntentPlan(
            intent="document_authoring",
            action="clarify",
            target=target,
            confidence=0.78,
            reason_codes=("ambiguous_document_request",),
        )
    # A generic “输出 Excel 表格” is an export even when a template happens to
    # be mounted in the session.  Generation verbs, template markers and fill
    # verbs retain the document-flow meaning.
    template_command = bool(
        recommended_followup
        or explicit_template_command
        or fill_command
        or (has_template_context and document_command and not explicit_export)
    )
    if template_command:
        reasons = (
            ("recommended_followup",)
            if recommended_followup and not document_command
            else ("template_targeted_command",)
        )
        return IntentPlan(
            intent="template_generation",
            action="generate",
            target=target,
            template_required=True,
            requested_formats=formats,
            confidence=0.98,
            reason_codes=reasons,
        )

    if generation_command:
        return IntentPlan(
            intent="document_authoring",
            action="generate",
            target=target,
            template_required=False,
            requested_formats=formats,
            confidence=0.94,
            reason_codes=("document_authoring_command",),
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


def is_template_generation_query(
    query: str,
    *,
    has_template_context: bool = False,
) -> bool:
    """Compatibility helper for callers that only need the route decision."""

    return classify_intent(
        query,
        has_template_context=has_template_context,
    ).intent == "template_generation"
