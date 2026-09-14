"""Behavioural safety checks for conversational action capabilities."""

from __future__ import annotations

from src.agents.orchestration.capabilities import (
    ACTION_CAPABILITIES,
    compute_effective_capabilities,
)
from src.agents.orchestration.contracts import (
    ActionSpec,
    ConversationActionPlan,
    UserConstraint,
    validate_action_plan,
)


def test_attachment_only_scope_never_exposes_knowledge_base_capabilities():
    effective = compute_effective_capabilities(
        deployed={
            "attachment.search",
            "document.authoring",
            "knowledge_base.catalog",
            "knowledge_base.search",
        },
        authorized={
            "attachment.search",
            "document.authoring",
            "knowledge_base.catalog",
            "knowledge_base.search",
        },
        source_scope="attachment_only",
    )

    assert effective == {"attachment.search", "document.authoring"}


def test_explicit_no_release_removes_release_and_rejects_release_action():
    effective = compute_effective_capabilities(
        deployed={"document.authoring", "document.release.request"},
        authorized={"document.authoring", "document.release.request"},
        accepted_constraints={"release"},
    )
    plan = ConversationActionPlan(
        plan_id="release-is-forbidden",
        goal="生成候选但不要发布",
        actions=[ActionSpec(
            action_id="release",
            kind="request_release",
            required_capabilities=["document.release.request"],
        )],
        constraints=[UserConstraint(
            constraint_id="no-release",
            kind="forbid_action",
            value="release",
            source_text_span="不要发布",
        )],
    )

    assert "document.release.request" not in effective
    issues = validate_action_plan(plan, available_capabilities=effective)
    assert [(issue.code, issue.action_id) for issue in issues] == [
        ("forbidden_action", "release"),
        ("unsupported_capability", "release"),
    ]


def test_every_supported_action_kind_has_a_capability_that_validates():
    actions = [
        ActionSpec(
            action_id=f"action-{index}",
            kind=kind,
            required_capabilities=[capability],
        )
        for index, (kind, capability) in enumerate(ACTION_CAPABILITIES.items(), start=1)
    ]
    plan = ConversationActionPlan(
        plan_id="all-action-kinds",
        goal="exercise all governed action types",
        actions=actions,
    )

    assert validate_action_plan(
        plan,
        available_capabilities=set(ACTION_CAPABILITIES.values()),
    ) == []


def test_revision_is_unavailable_without_a_candidate_artifact():
    effective = compute_effective_capabilities(
        deployed={"document.authoring", "document.revise"},
        authorized={"document.authoring", "document.revise"},
        object_state={"artifact_ref": ""},
    )

    assert effective == {"document.authoring"}
