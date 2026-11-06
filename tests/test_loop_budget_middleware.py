"""Loop budget middleware stops the graph only at model boundaries.

LangGraph's ToolNode converts exceptions thrown from ``wrap_tool_call`` into
error ToolMessages, so all ``LoopStop`` raises must happen inside
``wrap_model_call``; the tool wrapper degrades instead of raising.
"""

from __future__ import annotations

import time

import pytest
from langchain_core.messages import ToolMessage

from src.agents.middleware.loop_budget import (
    CallBudgetExceeded,
    LoopBudget,
    LoopBudgetMiddleware,
    ProgressStopped,
    TimeBudgetExceeded,
)
from src.agents.tools.runtime import ToolRuntime


def _tool_request(call_id: str = "call-1", name: str = "knowledge_search"):
    """Minimal ToolCallRequest stand-in with the fields the wrapper touches."""

    from types import SimpleNamespace

    return SimpleNamespace(
        tool_call={"id": call_id, "name": name, "args": {}},
        tool=None,
        state={},
        runtime=None,
    )


def test_time_budget_raises_before_model_call():
    budget = LoopBudget(ToolRuntime(kb_name="kb", ctx=None), deadline=time.monotonic() - 1)
    middleware = LoopBudgetMiddleware(budget)

    with pytest.raises(TimeBudgetExceeded):
        middleware.wrap_model_call(object(), lambda request: request)


def test_progress_stop_raises_at_model_boundary_not_tool_wrap():
    runtime = ToolRuntime(kb_name="kb", ctx=None)
    runtime.no_progress_limit = 1
    runtime.note_no_progress()
    middleware = LoopBudgetMiddleware(LoopBudget(runtime))

    # 工具包装器不再抛异常（ToolNode 会吞）；由下一个 model 调用统一抛出。
    assert middleware.wrap_tool_call(object(), lambda request: "tool result") == "tool result"
    with pytest.raises(ProgressStopped):
        middleware.wrap_model_call(object(), lambda request: "model call")
    assert runtime.loop_stop_reason == "no_progress"


def test_call_budget_allows_exactly_cap_then_skips_extras():
    runtime = ToolRuntime(kb_name="kb", ctx=None)
    budget = LoopBudget(runtime, tool_call_cap=2)
    middleware = LoopBudgetMiddleware(budget)

    assert middleware.wrap_tool_call(object(), lambda request: "ok") == "ok"
    assert middleware.wrap_tool_call(object(), lambda request: "ok") == "ok"
    assert budget.tool_calls == 2

    # 第 3 次调用不执行、直接返回被跳过的 error ToolMessage（不抛异常）
    skipped = middleware.wrap_tool_call(
        _tool_request(), lambda request: pytest.fail("must not run")
    )
    assert isinstance(skipped, ToolMessage)
    assert skipped.status == "error"
    assert budget.tool_calls == 2

    # 下一个 model 调用抛出真正的循环停止
    with pytest.raises(CallBudgetExceeded):
        middleware.wrap_model_call(object(), lambda request: "model call")


def test_tool_call_budget_counts_executions_between_model_rounds():
    budget = LoopBudget(ToolRuntime(kb_name="kb", ctx=None), tool_call_cap=2)
    mw = LoopBudgetMiddleware(budget)
    tool = lambda: mw.wrap_tool_call(object(), lambda r: "ok")  # noqa: E731
    model = lambda: mw.wrap_model_call(object(), lambda r: "ok")  # noqa: E731

    assert model() == "ok"          # 第一个 model round 不消耗额度
    assert tool() == "ok"           # 工具 1
    assert model() == "ok"
    assert tool() == "ok"           # 工具 2 → 额度耗尽
    assert budget.tool_calls == 2
    with pytest.raises(CallBudgetExceeded):
        model()                     # 收尾 model 也被拦截，转入部分合成


def test_call_budget_disabled_when_no_cap():
    runtime = ToolRuntime(kb_name="kb", ctx=None)
    budget = LoopBudget(runtime)
    middleware = LoopBudgetMiddleware(budget)

    for _ in range(3):
        assert middleware.wrap_tool_call(object(), lambda request: "ok") == "ok"
    assert budget.tool_calls == 0
