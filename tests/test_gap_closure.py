"""Gap-closure policy is deterministic and independent of the runner loop."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.gap_closure import decide_gap_closure


def _rt(*, candidates=None, tools=()):
    return SimpleNamespace(
        candidate_evidence=dict(candidates or {}),
        diagnostics=[{"tool_name": name} for name in tools],
    )


def _decide(rt, **overrides):
    kwargs = dict(
        draft="过压门限数值未给出。",
        rt=rt,
        pre_candidates=set(),
        elapsed_seconds=10.0,
        gap_rounds=0,
        is_general=False,
        query_mode="deep",
        has_checkpointer=True,
    )
    kwargs.update(overrides)
    return decide_gap_closure(**kwargs)


def test_no_gaps_does_not_close():
    decision = _decide(_rt(), draft="门限为 2.884V。")
    assert decision.should_close is False
    assert decision.reason == "no_gaps"


def test_unread_candidate_makes_closure_actionable():
    decision = _decide(_rt(candidates={"doc:1": object()}, tools=("kb_grep", "document_search")))
    assert decision.should_close is True
    assert decision.gaps == ["过压门限数值未给出"]


def test_single_tool_turn_makes_closure_actionable():
    decision = _decide(_rt(tools=("document_search",)))
    assert decision.should_close is True


def test_multiple_tools_without_new_candidates_does_not_close():
    decision = _decide(
        _rt(tools=("document_search", "circuit_search"))
    )
    assert decision.should_close is False
    assert decision.reason == "no_actionable_pointers"


def test_slow_turn_skips_closure():
    decision = _decide(_rt(candidates={"doc:1": object()}), elapsed_seconds=361)
    assert decision.should_close is False
    assert decision.reason == "slow_turn"


def test_closure_is_limited_to_one_round():
    decision = _decide(_rt(candidates={"doc:1": object()}), gap_rounds=1)
    assert decision.should_close is False
    assert decision.reason == "already_closed"


def test_general_and_fast_turns_skip_closure():
    assert _decide(_rt(), is_general=True).reason == "disabled"
    assert _decide(_rt(), query_mode="fast").reason == "disabled"
