"""Generic pending-decision aggregation and policy regressions."""

from __future__ import annotations

from src.agents.orchestration.decisions import (
    DecisionPolicyMode,
    decisions_from_grouped_items,
    group_pending_decisions,
    resolve_decision_policy,
)


def test_groups_items_by_kind_and_group_key_with_counts():
    decisions = decisions_from_grouped_items(
        [
            {"exception_id": "e1", "refdes": "X1900", "user_instruction": "确认该脚是否需要暴露"},
            {"exception_id": "e2", "refdes": "X1900", "user_instruction": "确认该脚是否需要暴露"},
            {"exception_id": "e3", "refdes": "X1902", "user_instruction": "确认该脚是否需要暴露"},
        ],
        kind="pin_exposure",
        group_field="refdes",
        title="位号 {group} 待确认",
        options=["include", "exclude"],
        default_option="include",
    )

    assert len(decisions) == 2
    by_key = {decision.group_key: decision for decision in decisions}
    assert by_key["X1900"].count == 2
    assert by_key["X1900"].items[0].item_id == "e1"
    assert by_key["X1902"].count == 1
    assert all(decision.default_option == "include" for decision in decisions)


def test_grouping_deduplicates_replayed_items():
    decisions = group_pending_decisions([
        {"kind": "missing_field", "group_key": "conclusion", "item_id": "a"},
        {"kind": "missing_field", "group_key": "conclusion", "item_id": "a"},
        {"kind": "missing_field", "group_key": "conclusion", "item_id": "b"},
    ])

    assert len(decisions) == 1
    assert decisions[0].count == 2
    assert [item.item_id for item in decisions[0].items] == ["a", "b"]


def test_blocking_groups_sort_first():
    decisions = group_pending_decisions([
        {"kind": "note", "group_key": "a", "item_id": "1", "blocking": False},
        {"kind": "note", "group_key": "b", "item_id": "2", "blocking": True},
    ])

    assert decisions[0].group_key == "b"
    assert decisions[0].blocking is True


def test_policy_defaults_to_continue_and_allows_explicit_overrides():
    assert resolve_decision_policy(document_type="icd") is DecisionPolicyMode.CONTINUE_WITH_DEFAULTS
    assert resolve_decision_policy(
        kind="pin_exposure",
        overrides={"pin_exposure": "ask_before_write"},
    ) is DecisionPolicyMode.ASK_BEFORE_WRITE
    assert resolve_decision_policy(
        document_type="icd",
        kind="pin_exposure",
        overrides={"icd": "block", "pin_exposure": "continue_with_defaults"},
    ) is DecisionPolicyMode.CONTINUE_WITH_DEFAULTS
    assert resolve_decision_policy(
        document_type="report", overrides={"report": "not-a-mode"},
    ) is DecisionPolicyMode.CONTINUE_WITH_DEFAULTS
