"""Same-turn gap closure as an official AgentMiddleware hook."""

from __future__ import annotations

import time
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage

from src.agents.gap_closure import decide_gap_closure
from src.agents.session_recall import gap_closure_prompt


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return str(content or "")


class GapClosureMiddleware(AgentMiddleware):
    """Jump back to the model once when the final draft declares a gap."""

    name = "GapClosureMiddleware"

    def __init__(
        self,
        *,
        rt: Any,
        pre_candidates: set[str],
        turn_started: float,
        is_general: bool,
        query_mode: str,
        has_checkpointer: bool,
    ):
        self.rt = rt
        self.pre_candidates = set(pre_candidates)
        self.turn_started = turn_started
        self.is_general = is_general
        self.query_mode = query_mode
        self.has_checkpointer = has_checkpointer
        self.rounds = 0

    @hook_config(can_jump_to=["model"])
    def after_agent(
        self, state: AgentState, runtime: Any = None
    ) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        draft = ""
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                draft = _message_text(message)
                break
        if not draft:
            return None
        decision = decide_gap_closure(
            draft=draft,
            rt=self.rt,
            pre_candidates=self.pre_candidates,
            elapsed_seconds=time.monotonic() - self.turn_started,
            gap_rounds=self.rounds,
            is_general=self.is_general,
            query_mode=self.query_mode,
            has_checkpointer=self.has_checkpointer,
        )
        if not decision.should_close:
            return None
        self.rounds += 1
        return {
            "messages": [
                HumanMessage(content=gap_closure_prompt(self.rt, decision.gaps))
            ],
            "jump_to": "model",
        }


__all__ = ["GapClosureMiddleware"]
