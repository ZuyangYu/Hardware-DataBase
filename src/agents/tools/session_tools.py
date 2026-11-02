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

    def session_recall(include_gaps: bool = True) -> str:
        """查看本会话最近几轮已读证据和未解决缺口。

        仅在当前问题明显延续之前的讨论时调用；完全无关的新问题不要调用。
        返回的 id 可直接传给 evidence_read 重新登记为本轮引用。
        """
        items = list(getattr(rt, "session_recall_items", []) or [])[-_MAX_RECALL_ITEMS:]
        gaps = (
            list(getattr(rt, "session_recall_gaps", []) or [])
            if include_gaps
            else []
        )
        if not items and not gaps:
            return "（本会话没有可复用的上一轮证据或缺口）"
        rt.prior_gaps = gaps[:5]
        admitted, adds_nothing = timed_tool_call(
            rt,
            "session_recall",
            "session_history",
            None,
            lambda: items,
            candidate_only=True,
        )
        rendered = format_tool_result(rt, adds_nothing, admitted)
        if gaps:
            rendered += "\n\n上一轮未解决缺口：\n" + "\n".join(
                f"- {gap}" for gap in gaps[:5]
            )
        return rendered

    return session_recall


__all__ = ["make_session_recall"]
