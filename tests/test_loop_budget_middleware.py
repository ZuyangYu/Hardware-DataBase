"""Loop budget middleware owns time and no-progress stops."""

from __future__ import annotations

import time

import pytest

from src.agents.middleware.loop_budget import (
    LoopBudget,
    LoopBudgetMiddleware,
    ProgressStopped,
    TimeBudgetExceeded,
)
from src.agents.tools.runtime import ToolRuntime


def test_time_budget_raises_before_model_call():
    budget = LoopBudget(ToolRuntime(kb_name="kb", ctx=None), deadline=time.monotonic() - 1)
    middleware = LoopBudgetMiddleware(budget)

    with pytest.raises(TimeBudgetExceeded):
        middleware.wrap_model_call(object(), lambda request: request)


def test_progress_stop_raises_after_tool_call():
    runtime = ToolRuntime(kb_name="kb", ctx=None)
    runtime.no_progress_limit = 1
    runtime.note_no_progress()
    middleware = LoopBudgetMiddleware(LoopBudget(runtime))

    with pytest.raises(ProgressStopped):
        middleware.wrap_tool_call(object(), lambda request: "tool result")

    assert runtime.loop_stop_reason == "no_progress"
