"""Gap closure is an AgentMiddleware jump, not a runner-owned second loop."""

from __future__ import annotations

import time
import threading
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from src.agents.middleware.gap_closure import GapClosureMiddleware


def _candidate():
    return SimpleNamespace(
        id="doc:1",
        source_name="a.docx",
        locator={"page": 1},
        content="门限 2.884V",
    )


def _rt(*, candidates=None, tools=("document_search",)):
    return SimpleNamespace(
        candidate_evidence=dict(candidates or {}),
        diagnostics=[{"tool_name": name} for name in tools],
        candidate_order=list((candidates or {}).keys()),
        _lock=threading.Lock(),
        question="过压门限是多少？",
        prior_gaps=[],
    )


def _middleware(rt, **overrides):
    kwargs = dict(
        rt=rt,
        pre_candidates=set(),
        turn_started=time.monotonic(),
        is_general=False,
        query_mode="deep",
        has_checkpointer=True,
    )
    kwargs.update(overrides)
    return GapClosureMiddleware(**kwargs)


def test_gapped_draft_jumps_back_to_model_once():
    rt = _rt(candidates={"doc:1": _candidate()})
    middleware = _middleware(rt)
    state = {"messages": [AIMessage(content="过压门限数值未给出。")]}

    update = middleware.after_agent(state)

    assert update is not None
    assert update["jump_to"] == "model"
    assert isinstance(update["messages"][0], HumanMessage)
    assert "本轮回答仍存在证据缺口" in update["messages"][0].content
    assert middleware.after_agent(state) is None


def test_no_gap_does_not_jump():
    middleware = _middleware(_rt())
    update = middleware.after_agent(
        {"messages": [AIMessage(content="门限为 2.884V。")]}
    )
    assert update is None


def test_general_or_fast_turn_does_not_jump():
    state = {"messages": [AIMessage(content="过压门限数值未给出。")]}
    assert _middleware(_rt(candidates={"doc:1": _candidate()}), is_general=True).after_agent(state) is None
    assert _middleware(_rt(candidates={"doc:1": _candidate()}), query_mode="fast").after_agent(state) is None
