"""Model-driven recall of prior-turn evidence and unresolved gaps.

Nothing is injected automatically: the tool is the only way prior pointers enter
the current candidate pool, so an unrelated new question stays clean.
"""

from __future__ import annotations

from typing import Any

from src.agents.tools.runtime import format_tool_result, timed_tool_call

_MAX_RECALL_ITEMS = 12


def make_session_recall(rt: Any):
    """Return a tool that exposes prior-turn pointers only when the model asks."""

    def session_recall(query: str = "", include_gaps: bool = True) -> str:
        """查看本会话已有证据和未解决缺口，可按关键词搜索旧证据。

        仅在当前问题明显延续之前的讨论时调用；完全无关的新问题不要调用。
        query 为空时返回最近的证据；传入关键词时在整段会话目录中检索。
        返回的 id 可直接传给 evidence_read 重新登记为本轮引用。
        """
        text = " ".join(str(query or "").split())
        store = getattr(rt, "session_evidence_store", None)
        if text and store is not None:
            items = store.search(text, limit=_MAX_RECALL_ITEMS)
            search_query = f"session_history:{text[:120]}"
        else:
            items = list(getattr(rt, "session_recall_items", []) or [])[-_MAX_RECALL_ITEMS:]
            search_query = "session_history"
        gaps = (
            list(getattr(rt, "session_recall_gaps", []) or [])
            if include_gaps
            else []
        )
        if not items and not gaps:
            if text:
                return f"（本会话没有匹配“{text[:80]}”的旧证据或缺口）"
            return "（本会话没有可复用的上一轮证据或缺口）"
        rt.prior_gaps = gaps[:5]
        admitted, adds_nothing = timed_tool_call(
            rt,
            "session_recall",
            search_query,
            None,
            lambda: items,
            candidate_only=True,
        )
        rendered = format_tool_result(rt, adds_nothing, admitted)
        if text and not items:
            rendered = (
                f"（本会话没有匹配“{text[:80]}”的旧证据）\n\n{rendered}"
            )
        if gaps:
            rendered += "\n\n上一轮未解决缺口：\n" + "\n".join(
                f"- {gap}" for gap in gaps[:5]
            )
        return rendered

    return session_recall


__all__ = ["make_session_recall"]
