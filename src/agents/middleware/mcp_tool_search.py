"""Reveal only the MCP tools selected through ``mcp_tool_search``.

All guarded MCP wrappers stay in the agent toolset so the model can call a
selected tool on the next model invocation; this middleware hides the rest from
the schema sent to the provider.  Local tools and the search tool are always
visible.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest

from src.agents.mcp.policy import is_mcp_tool_name


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


class McpToolSearchMiddleware(AgentMiddleware):
    """Filter the provider schema down to selected external tools."""

    name = "McpToolSearchMiddleware"

    def __init__(self, rt: Any):
        self.rt = rt

    def _visible_tools(self, tools: list[Any]) -> list[Any]:
        selected = set(getattr(self.rt, "selected_mcp_tools", None) or ())
        visible: list[Any] = []
        for tool in tools:
            name = _tool_name(tool)
            if is_mcp_tool_name(name) and name not in selected:
                continue
            visible.append(tool)
        return visible

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        return handler(request.override(tools=self._visible_tools(request.tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        return await handler(
            request.override(tools=self._visible_tools(request.tools))
        )


__all__ = ["McpToolSearchMiddleware"]
