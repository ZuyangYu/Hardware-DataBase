"""Expose the official ``read_file`` tool only on skill-bearing turns.

Deep Agents' ``SkillsMiddleware`` is progressive disclosure: the prompt lists
``SKILL.md`` paths and the model reads the body through ``read_file``.  The
domain harness still forbids arbitrary filesystem access, so ``read_file`` is
hidden unless this turn materialized an authorized, read-only skill root.

The same middleware also reports Skill body reads to the turn observer.  The
tool is supplied by deepagents' ``FilesystemMiddleware``, so it does not call
``ToolRuntime.tool_started`` on its own; without this hook a successful read
would leave no audit trail in the turn event stream.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _result_text(result: Any) -> str:
    if isinstance(result, ToolMessage):
        content = result.content
    else:
        content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


class SkillReadGateMiddleware(AgentMiddleware):
    """Hide ``read_file`` without a Skill root and report authorized reads."""

    name = "SkillReadGateMiddleware"

    def __init__(
        self,
        *,
        enabled: bool,
        observer: Callable[..., None] | None = None,
    ):
        self.enabled = bool(enabled)
        self.observer = observer

    def _tools(self, tools: list[Any]) -> list[Any]:
        if self.enabled:
            return tools
        return [tool for tool in tools if _tool_name(tool) != "read_file"]

    def _notify(
        self,
        phase: str,
        *,
        call_id: str,
        path: str,
        status: str = "done",
        result_bytes: int = 0,
        error: str = "",
    ) -> None:
        if self.observer is None:
            return
        try:
            self.observer(
                phase=phase,
                call_id=call_id,
                path=path,
                status=status,
                result_bytes=result_bytes,
                error=error,
            )
        except Exception:
            pass

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

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        call = request.tool_call or {}
        if not self.enabled or str(call.get("name") or "") != "read_file":
            return handler(request)
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        path = str(args.get("file_path") or args.get("path") or "")
        call_id = str(call.get("id") or "")
        self._notify("started", call_id=call_id, path=path)
        try:
            result = handler(request)
        except Exception as exc:
            self._notify(
                "finished",
                call_id=call_id,
                path=path,
                status="error",
                error=str(exc)[:200],
            )
            raise
        text = _result_text(result)
        self._notify(
            "finished",
            call_id=call_id,
            path=path,
            status="done",
            result_bytes=len(text.encode("utf-8")),
        )
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        call = request.tool_call or {}
        if not self.enabled or str(call.get("name") or "") != "read_file":
            return await handler(request)
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        path = str(args.get("file_path") or args.get("path") or "")
        call_id = str(call.get("id") or "")
        self._notify("started", call_id=call_id, path=path)
        try:
            result = await handler(request)
        except Exception as exc:
            self._notify(
                "finished",
                call_id=call_id,
                path=path,
                status="error",
                error=str(exc)[:200],
            )
            raise
        text = _result_text(result)
        self._notify(
            "finished",
            call_id=call_id,
            path=path,
            status="done",
            result_bytes=len(text.encode("utf-8")),
        )
        return result


__all__ = ["SkillReadGateMiddleware"]
