"""Loop budget middleware: time, no-progress and tool-call stops.

LangGraph's ``ToolNode`` wraps middleware exceptions into ``status="error"``
ToolMessages by default, so any ``LoopStop`` raised inside ``wrap_tool_call``
would be swallowed and the agent loop would keep running. All budget stops
therefore raise at the **model call boundary** (``wrap_model_call``), which
propagates cleanly to the runner. The tool wrapper only marks the runtime,
consumes the tool-call allowance, and degrades the *current* call to a
non-executed error ToolMessage once a stop condition is already reached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage


class LoopStop(Exception):
    """Base class for budget-driven graph stops."""


class ProgressStopped(LoopStop):
    """No new evidence/candidates/sources for too many calls."""


class TimeBudgetExceeded(LoopStop):
    """The wall-clock budget for this stream round expired."""


class CallBudgetExceeded(LoopStop):
    """The user-declared tool-call budget for this turn ran out."""


def _skip_tool_message(request: ToolCallRequest, note: str) -> Any:
    """Degrade a tool call to a non-executed error ToolMessage.

    Used when a stop condition is already true: raising here would be
    swallowed by ToolNode, so the call is simply not executed and the next
    model boundary raises the real ``LoopStop``.
    """

    call = getattr(request, "tool_call", None) or {}
    return ToolMessage(
        content=note,
        name=str(call.get("name") or "tool"),
        tool_call_id=str(call.get("id") or ""),
        status="error",
    )


@dataclass
class LoopBudget:
    rt: Any
    deadline: float | None = None
    tool_call_cap: int | None = None
    tool_calls: int = 0

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def consume_tool_allowance(self) -> bool:
        """Consume one allowance slot for a tool execution.

        Returns ``True`` when the call may execute. ``False`` means the
        user-declared cap is reached; the caller must skip the execution
        (without raising) so the next model boundary raises
        ``CallBudgetExceeded``.
        """

        if self.tool_call_cap is None:
            return True
        if self.tool_calls >= int(self.tool_call_cap):
            return False
        self.tool_calls += 1
        return True

    def check_model_boundary(self) -> None:
        """Raise the configured stop at the model call boundary.

        This is the only place a ``LoopStop`` may be raised: exceptions
        thrown from ``wrap_tool_call`` never reach the runner.
        """

        if self.expired():
            raise TimeBudgetExceeded()
        if self.tool_call_cap is not None and self.tool_calls >= int(self.tool_call_cap):
            self.rt.mark_ended()
            raise CallBudgetExceeded()
        if self.rt.should_stop_for_no_progress():
            self.rt.mark_ended()
            raise ProgressStopped()


class LoopBudgetMiddleware(AgentMiddleware):
    """Enforce turn time, no-progress and user tool-call budgets."""

    name = "LoopBudgetMiddleware"

    def __init__(self, budget: LoopBudget):
        self.budget = budget

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        self.budget.check_model_boundary()
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        self.budget.check_model_boundary()
        return await handler(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        if self.budget.expired():
            note = "已超过本轮执行时间预算，调用未执行。"
        elif not self.budget.consume_tool_allowance():
            note = (
                f"本轮工具调用总计已达上限 {self.budget.tool_call_cap} 次"
                "（用户在问题上指定的约束），本调用未执行。"
            )
        else:
            result = handler(request)
            # Propagate the no-progress signal to the next model boundary;
            # the runtime keeps this flag so the raise happens where it can
            # actually stop the graph.
            return result
        return _skip_tool_message(request, note)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        if self.budget.expired():
            return _skip_tool_message(
                request, "已超过本轮执行时间预算，调用未执行。"
            )
        if not self.budget.consume_tool_allowance():
            return _skip_tool_message(
                request,
                (
                    f"本轮工具调用总计已达上限 {self.budget.tool_call_cap} 次"
                    "（用户在问题上指定的约束），本调用未执行。"
                ),
            )
        return await handler(request)


__all__ = [
    "LoopBudget",
    "LoopBudgetMiddleware",
    "LoopStop",
    "ProgressStopped",
    "TimeBudgetExceeded",
    "CallBudgetExceeded",
]
