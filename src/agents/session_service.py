"""Session preparation: checkpointer, thread config and initial messages.

The runner should not own LangGraph session mechanics.  This module is the
single place that decides whether a turn resumes a checkpointed thread, seeds
DB history, or runs stateless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.agents.turn_meta import _build_messages


def _default_checkpointer() -> Any:
    from src.agents.checkpoints.factory import create_checkpointer

    return create_checkpointer()


@dataclass
class PreparedSession:
    checkpointer: Any = None
    config: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, str]] = field(default_factory=list)


def prepare_session(
    *,
    thread_id: str,
    persist_thread: bool,
    recursion_limit: int,
    history: list[tuple[str, str]],
    query: str,
    checkpointer_factory: Callable[[], Any] | None = None,
) -> PreparedSession:
    """Build the per-turn session envelope without touching agent state."""

    checkpointer = None
    if persist_thread and str(thread_id or "").strip():
        factory = checkpointer_factory or _default_checkpointer
        checkpointer = factory()

    config: dict[str, Any] = {"recursion_limit": recursion_limit}
    if checkpointer is not None:
        config["configurable"] = {"thread_id": str(thread_id)}
        # checkpoint 已存在 → 只喂本轮新消息（历史由框架恢复）；
        # 首次接触该 thread（新会话/存量会话迁移）→ 把 DB 近期历史一次性播种。
        has_state = checkpointer.get_tuple(config) is not None
        if has_state or not history:
            messages: list[dict[str, str]] = [{"role": "user", "content": query}]
        else:
            messages = _build_messages(query, history)
    else:
        messages = _build_messages(query, history)
    return PreparedSession(
        checkpointer=checkpointer, config=config, messages=messages
    )


__all__ = ["PreparedSession", "prepare_session"]
