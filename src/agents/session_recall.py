"""Session-scoped recall of prior-turn evidence pointers and unresolved gaps."""

from __future__ import annotations

import re
from typing import Any

from src.agents.schemas import Evidence

_MAX_PRIOR_GAPS = 5

_FOLLOW_UP_RE = re.compile(
    r"^(?:那|那么|这个|该|其|它|上述|刚才|前面|继续|再看|还有|另外)|(?:呢|吗)[？?]?$"
)
_ENTITY_RE = re.compile(r"\b[A-Z]{1,6}\d[A-Z0-9_-]*\b", re.IGNORECASE)
_CJK_SEGMENT_RE = re.compile(r"[\u4e00-\u9fff]+")
_STOP_BIGRAMS = frozenset({
    "什么", "怎么", "如何", "哪些", "多少", "是否", "这个", "那个", "可以",
    "需要", "问题", "资料", "证据", "知识", "没有", "给出", "出现", "无法",
    "确认", "上一", "一轮", "本轮", "以及", "进行", "一个", "我们", "现在",
    "具体", "相关", "内容", "还有", "另外", "继续",
})


def _cjk_bigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for segment in _CJK_SEGMENT_RE.findall(str(text or "")):
        for index in range(len(segment) - 1):
            gram = segment[index:index + 2]
            if gram not in _STOP_BIGRAMS:
                grams.add(gram)
    return grams


def prior_context_relevant(query: str, turns: list[Any]) -> bool:
    """Only carry prior evidence/gaps into an actually related next question."""

    current = str(query or "").strip()
    if not current or not turns:
        return False
    if len(current) <= 120 and _FOLLOW_UP_RE.search(current):
        return True
    prior_parts: list[str] = []
    for turn in turns[:3]:
        prior_parts.append(str(getattr(turn, "query", "") or ""))
        summary = turn.summary if isinstance(turn.summary, dict) else {}
        verification = summary.get("verification") or {}
        prior_parts.extend(
            str(gap or "")
            for gap in [
                *(verification.get("answer_gaps") or []),
                *(verification.get("gaps") or []),
            ]
        )
        for row in summary.get("evidence") or []:
            if isinstance(row, dict):
                prior_parts.append(str(row.get("source_name") or ""))
    prior_text = " ".join(prior_parts)
    if set(_ENTITY_RE.findall(current)) & set(_ENTITY_RE.findall(prior_text)):
        return True
    return len(_cjk_bigrams(current) & _cjk_bigrams(prior_text)) >= 2


def preload_session_context(
    rt: Any, session_id: int | None, user_id: int | None
) -> None:
    """Reuse recent evidence as candidates and surface last turn's gaps."""

    if not session_id or not user_id:
        return
    try:
        from src.core.conversation import ConversationService

        turns = ConversationService().list_recent_turns(
            int(user_id), int(session_id), limit=4
        )
    except Exception:
        return
    history = [
        turn
        for turn in turns
        if isinstance(getattr(turn, "summary", None), dict) and turn.summary
    ]
    if not prior_context_relevant(str(getattr(rt, "question", "") or ""), history):
        return

    gaps: list[str] = []
    for turn in history:
        summary = turn.summary if isinstance(turn.summary, dict) else {}
        verification = summary.get("verification") or {}
        for gap in [
            *(verification.get("answer_gaps") or []),
            *(verification.get("gaps") or []),
        ]:
            text = str(gap or "").strip()
            if text and text not in gaps:
                gaps.append(text[:240])
            if len(gaps) >= _MAX_PRIOR_GAPS:
                break
        if len(gaps) >= _MAX_PRIOR_GAPS:
            break

    items: list[Evidence] = []
    for turn in reversed(history):
        summary = turn.summary if isinstance(turn.summary, dict) else {}
        for row in summary.get("evidence") or []:
            if not isinstance(row, dict) or not row.get("id") or not row.get("content"):
                continue
            items.append(
                Evidence(
                    id=str(row.get("id")),
                    content=str(row.get("content")),
                    source_name=str(row.get("source_name") or ""),
                    content_kind=str(row.get("content_kind") or "document_text"),
                    processor_kind=str(row.get("processor_kind") or "ragflow"),
                    score=float(row.get("score") or 0.0),
                    locator=dict(row.get("locator") or {}),
                    metadata=dict(row.get("metadata") or {}),
                )
            )
    if items:
        rt.stash_candidates(items)
        rt.prior_evidence_ids.update(item.id for item in items)
    rt.prior_gaps = gaps


def gap_closure_prompt(rt: Any, gaps: list[str]) -> str:
    """Build the same-turn continuation prompt that closes declared gaps."""

    lines = [
        "【系统内部指令】本轮回答仍存在证据缺口。请在给出最终回答前先补齐，不要直接结束。",
        f"用户问题：{str(getattr(rt, 'question', '') or '').strip()[:500]}",
        "缺口：",
    ]
    lines.extend(f"- {gap}" for gap in gaps[:5])
    with rt._lock:
        keys = list(rt.candidate_order)[-12:]
        candidates = [
            rt.candidate_evidence[key]
            for key in keys
            if key in rt.candidate_evidence
        ]
    if candidates:
        lines.append("可直接 evidence_read 的未读候选：")
        for item in candidates:
            locator = ", ".join(
                f"{name}={value}" for name, value in (item.locator or {}).items()
            )
            lines.append(
                f"- id={item.id} | {item.source_name or '未知来源'}"
                f" | {locator[:120]}"
                f" | {' '.join(str(item.content or '').split())[:120]}"
            )
    if rt.prior_gaps:
        lines.append("上一轮遗留缺口：")
        lines.extend(f"- {gap}" for gap in rt.prior_gaps[:5])
    lines.append(
        "要求：最多再执行 3 次工具调用，优先补齐最影响结论的 1-2 个缺口"
        "（已做过且无结果的完全相同查询不要重复）；"
        "补齐后重新输出完整最终回答，只保留一个版本，不要提“上一版/补充说明”。"
        "确认知识库确实没有的，写清已查来源和关键词。"
        "（本指令是系统内部补齐步骤，不属于用户对话内容。）"
    )
    return "\n".join(lines)


def evidence_rows(evidence: list[Evidence]) -> list[dict]:
    """Serialize evidence for the persisted turn summary."""

    rows = []
    for item in evidence:
        rows.append(
            {
                "id": item.id,
                "source_name": item.source_name,
                "score": item.score,
                "locator": item.locator,
                "content_kind": item.content_kind,
                "processor_kind": item.processor_kind,
                "content": item.content,
                "metadata": item.metadata or {},
            }
        )
    return rows


__all__ = ["evidence_rows", "gap_closure_prompt", "preload_session_context"]
