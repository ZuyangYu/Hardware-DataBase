"""Expose the official ``read_file`` tool only on skill-bearing turns.

Deep Agents' ``SkillsMiddleware`` is progressive disclosure: the prompt lists
``SKILL.md`` paths and the model reads the body through ``read_file``.  The
domain harness still forbids arbitrary filesystem access, so ``read_file`` is
hidden unless this turn materialized an authorized, read-only skill root.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


class SkillReadGateMiddleware(AgentMiddleware):
    """Hide ``read_file`` when the turn has no materialized skill root."""

    name = "SkillReadGateMiddleware"

    def __init__(self, *, enabled: bool):
        self.enabled = bool(enabled)

    def _tools(self, tools: list[Any]) -> list[Any]:
        if self.enabled:
            return tools
        return [tool for tool in tools if _tool_name(tool) != "read_file"]

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        return handler(request.override(tools=self._tools(request.tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        return await handler(request.override(tools=self._tools(request.tools)))


__all__ = ["SkillReadGateMiddleware"]
