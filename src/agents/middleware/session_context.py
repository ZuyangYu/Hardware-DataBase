"""Official-style middleware that carries compact session continuity."""

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage

_MARKER = "<session_state>"


def _with_block(message: SystemMessage | None, block: str) -> SystemMessage:
    content = message.content if message is not None else ""
    text = content if isinstance(content, str) else str(content or "")
    if _MARKER in text:
        return message if message is not None else SystemMessage(content=text)
    merged = f"{text}\n\n{block}".strip() if text else block
    return SystemMessage(content=merged)


class SessionContextMiddleware(AgentMiddleware):
    """Append the previous turn's compact state to every model request.

    The block is pointers and conclusions, not evidence bodies, so it costs
    little context and never becomes citable without ``evidence_read``.
    """

    name = "SessionContextMiddleware"

    def __init__(self, block: str):
        self.block = str(block or "").strip()

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        if not self.block:
            return handler(request)
        return handler(
            request.override(system_message=_with_block(request.system_message, self.block))
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        if not self.block:
            return await handler(request)
        return await handler(
            request.override(system_message=_with_block(request.system_message, self.block))
        )


__all__ = ["SessionContextMiddleware"]
