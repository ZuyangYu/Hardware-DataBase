"""Deterministic evidence strategy for one conversation turn.

This module no longer decides that a turn *is* a comparison.  Comparison is a
model-selected tool call; the policy here only bounds prefetch/admission for a
purpose explicitly supplied by the caller.  It never widens the server-resolved
source scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

QA = "qa"
SUMMARIZE = "summarize"
EXTRACT = "extract"
COMPARE = "compare"
AUTHOR = "author"
VISUAL = "visual"

PURPOSES = (QA, SUMMARIZE, EXTRACT, COMPARE, AUTHOR, VISUAL)

_SUMMARIZE_PATTERN = re.compile(
    r"总结|摘要|概括|归纳|summari[sz]e|overview|摘要一下",
    re.IGNORECASE,
)
_EXTRACT_PATTERN = re.compile(
    r"提取|抽取|列出|清单|字段|表格数据|所有\d*个|extract|list all|field",
    re.IGNORECASE,
)
_VISUAL_PATTERN = re.compile(
    r"图片|截图|页面|图纸|视觉|chart|figure|image|page\s*\d+",
    re.IGNORECASE,
)
_AUTHOR_PATTERN = re.compile(
    r"生成|编写|撰写|起草|完善|修订|改写|输出.*(?:文档|报告|pdf|docx)|generate|draft|write",
    re.IGNORECASE,
)


def resolve_evidence_purpose(
    query: str,
    *,
    conversation_plan: Any | None = None,
    intent_plan: Any | None = None,
) -> str:
    """Return a bounded evidence purpose for an explicitly routed turn.

    Comparison is intentionally absent: the model decides whether to call the
    comparison tool, so a keyword must not pre-classify the turn.  The
    remaining purposes are derived from explicit verbs only; an uncertain
    request stays on the low-cost ``qa`` path.
    """

    text = str(query or "")
    action = str(getattr(conversation_plan, "action", "") or getattr(intent_plan, "action", ""))
    if action in {"revise_attachment"}:
        return AUTHOR
    if _VISUAL_PATTERN.search(text):
        return VISUAL
    if _SUMMARIZE_PATTERN.search(text):
        return SUMMARIZE
    if _EXTRACT_PATTERN.search(text):
        return EXTRACT
    if _AUTHOR_PATTERN.search(text):
        return AUTHOR
    return QA


@dataclass(frozen=True)
class EvidencePolicy:
    purpose: str
    prefetch_mode: str
    formal_prefetch: bool
    require_both_sources: bool
    max_preview_items: int = 4
    comparison_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "prefetch_mode": self.prefetch_mode,
            "formal_prefetch": self.formal_prefetch,
            "require_both_sources": self.require_both_sources,
            "max_preview_items": self.max_preview_items,
            "comparison_required": self.comparison_required,
        }


def policy_for_purpose(
    purpose: str,
    *,
    source_scope: str = "auto",
    has_attachments: bool = False,
    has_kb: bool = False,
) -> EvidencePolicy:
    normalized = str(purpose or QA)
    if normalized not in PURPOSES:
        normalized = QA
    dual = source_scope == "attachment_and_knowledge_base" or (
        has_attachments and has_kb
    )
    if normalized == COMPARE:
        return EvidencePolicy(
            purpose=normalized,
            prefetch_mode="catalog_only",
            formal_prefetch=False,
            require_both_sources=dual,
            max_preview_items=2,
            comparison_required=True,
        )
    if normalized == SUMMARIZE:
        return EvidencePolicy(
            purpose=normalized,
            prefetch_mode="section_preview",
            formal_prefetch=False,
            require_both_sources=False,
            max_preview_items=3,
        )
    if normalized == AUTHOR:
        # Document generation keeps its own frozen source contract; ordinary
        # chat evidence budget must not be reused as a generation budget.
        return EvidencePolicy(
            purpose=normalized,
            prefetch_mode="catalog_only",
            formal_prefetch=False,
            require_both_sources=False,
            max_preview_items=2,
        )
    if normalized == VISUAL:
        return EvidencePolicy(
            purpose=normalized,
            prefetch_mode="page_preview",
            formal_prefetch=False,
            require_both_sources=False,
            max_preview_items=2,
        )
    if normalized == EXTRACT:
        return EvidencePolicy(
            purpose=normalized,
            prefetch_mode="structured_preview",
            formal_prefetch=False,
            require_both_sources=False,
            max_preview_items=3,
        )
    return EvidencePolicy(
        purpose=QA,
        prefetch_mode="candidate_preview",
        formal_prefetch=False,
        require_both_sources=False,
        max_preview_items=4,
    )


__all__ = [
    "AUTHOR",
    "COMPARE",
    "EXTRACT",
    "PURPOSES",
    "QA",
    "SUMMARIZE",
    "VISUAL",
    "EvidencePolicy",
    "policy_for_purpose",
    "resolve_evidence_purpose",
]
