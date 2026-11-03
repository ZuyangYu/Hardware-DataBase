"""Session-scoped recall of prior-turn evidence pointers and unresolved gaps.

The recall buffer is deliberately dormant: nothing is injected into the prompt
or candidate pool until the model calls the ``session_recall`` tool.  The model,
not a regex, decides whether the new question continues the previous topic.
"""

from __future__ import annotations

from typing import Any

from src.agents.schemas import Evidence
from src.agents.session_evidence import load_session_evidence

_MAX_PRIOR_ITEMS = 12


def _session_state_block(
    history: list[Any], items: list[Evidence], gaps: list[str]
) -> str:
    """Build a compact, always-visible continuity block for the next turn."""

    if not history:
        return ""
    last = history[0]
    query = " ".join(str(getattr(last, "query", "") or "").split())[:300]
    answer = " ".join(str(getattr(last, "answer", "") or "").split())[:800]
    lines = [
        "<session_state>",
        "以下是本会话上一轮的上下文；仅在与当前问题相关时使用，不要重复无关内容。",
    ]
    if query:
        lines.append(f"上一轮问题：{query}")
    if answer:
        lines.append(f"上一轮结论：{answer}")
        lines.append("注意：上一轮结论里的 [n] 是上一轮编号，本轮引用前必须 evidence_read 重新登记。")
    if gaps:
        lines.append("未解决缺口：")
        lines.extend(f"- {gap}" for gap in gaps[:3])
    if items:
        lines.append("可用旧证据（引用前必须 evidence_read）：")
        for item in items[-6:]:
            locator = ", ".join(
                f"{name}={value}" for name, value in (item.locator or {}).items()
            )
            lines.append(
                f"- id={item.id} | {item.source_name or '未知来源'}"
                f" | {locator[:120]}"
            )
    lines.append(
        "需要更多历史证据时调用 session_recall(query=\"关键词\") 检索会话目录。"
    )
    lines.append("</session_state>")
    return "\n".join(lines)


def load_session_context(
    rt: Any, session_id: int | None, user_id: int | None
) -> None:
    """Load recent evidence/gaps into a dormant session recall buffer."""

    store = load_session_evidence(user_id, session_id)
    rt.session_evidence_store = store
    # Keep the legacy oldest-to-newest list for prompt rendering and callers
    # that predate the catalogue's search API.
    rt.session_recall_items = list(reversed(store.recent(_MAX_PRIOR_ITEMS)))
    rt.session_recall_gaps = store.gaps
    rt.session_state_text = _session_state_block(
        store.history, rt.session_recall_items, store.gaps
    )


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


__all__ = ["evidence_rows", "gap_closure_prompt", "load_session_context"]
