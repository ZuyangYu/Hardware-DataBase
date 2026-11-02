"""Loop budget middleware: time and no-progress stops outside the runner loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ToolCallRequest,
)


class LoopStop(Exception):
    """Base class for budget-driven graph stops."""


class ProgressStopped(LoopStop):
    """No new evidence/candidates/sources for too many calls."""


class TimeBudgetExceeded(LoopStop):
    """The wall-clock budget for this stream round expired."""


@dataclass
class LoopBudget:
    rt: Any
    deadline: float | None = None

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline


class LoopBudgetMiddleware(AgentMiddleware):
    """Enforce turn time and no-progress budgets around model/tool calls."""

    name = "LoopBudgetMiddleware"

    def __init__(self, budget: LoopBudget):
        self.budget = budget

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        if self.budget.expired():
            raise TimeBudgetExceeded()
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        if self.budget.expired():
            raise TimeBudgetExceeded()
        return await handler(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        result = handler(request)
        if self.budget.rt.should_stop_for_no_progress():
            self.budget.rt.mark_ended()
            raise ProgressStopped()
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        result = await handler(request)
        if self.budget.rt.should_stop_for_no_progress():
            self.budget.rt.mark_ended()
            raise ProgressStopped()
        return result


__all__ = [
    "LoopBudget",
    "LoopBudgetMiddleware",
    "LoopStop",
    "ProgressStopped",
    "TimeBudgetExceeded",
]
