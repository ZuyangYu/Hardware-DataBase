from __future__ import annotations

from src.agents.orchestration.contracts import (
    ActionSpec,
    ConversationActionPlan,
    UserConstraint,
)


def test_executor_runs_ready_actions_in_dependency_order_and_records_domain_results():
    """Dropping a dependency or its result must make this observable outcome fail."""
    from src.agents.orchestration.executor import ConversationActionExecutor

    plan = ConversationActionPlan(
        plan_id="plan-1",
        goal="search then create",
        actions=[
            ActionSpec(action_id="a1", kind="search", required_capabilities=["evidence.search"]),
            ActionSpec(
                action_id="a2",
                kind="create_document",
                depends_on=["a1"],
                required_capabilities=["document.authoring"],
            ),
        ],
    )

    def search(_action, _results):
        return {"status": "completed", "result_refs": ["evidence:1"]}

    def create(_action, results):
        assert results["a1"].result_refs == ["evidence:1"]
        return {"status": "accepted", "result_refs": ["artifact:candidate-1"]}

    result = ConversationActionExecutor(
        handlers={"search": search, "create_document": create},
        available_capabilities={"evidence.search", "document.authoring"},
    ).execute(plan)

    assert [(receipt.action_id, receipt.status, receipt.result_refs) for receipt in result.receipts] == [
        ("a1", "completed", ["evidence:1"]),
        ("a2", "accepted", ["artifact:candidate-1"]),
    ]


def test_executor_refuses_an_action_forbidden_by_the_user_before_handler_execution():
    """A forbidden write must never be dispatched merely because it is in a plan."""
    from src.agents.orchestration.executor import ConversationActionExecutor

    plan = ConversationActionPlan(
        plan_id="plan-2",
        goal="do not release",
        actions=[ActionSpec(action_id="a1", kind="request_release")],
        constraints=[UserConstraint(
            constraint_id="no-release",
            kind="forbid_action",
            value="request_release",
        )],
    )

    result = ConversationActionExecutor(
        handlers={"request_release": lambda *_args: {"status": "completed"}},
    ).execute(plan)

    assert [(receipt.status, receipt.error_code) for receipt in result.receipts] == [
        ("blocked", "forbidden_action"),
    ]


def test_executor_enforces_the_canonical_capability_when_a_model_omits_it():
    """An omitted model field must not turn a document write into an allowed call."""
    from src.agents.orchestration.executor import ConversationActionExecutor

    dispatched: list[str] = []
    plan = ConversationActionPlan(
        plan_id="missing-required-capability",
        goal="generate a candidate",
        actions=[ActionSpec(action_id="write", kind="create_document")],
    )

    result = ConversationActionExecutor(
        handlers={"create_document": lambda *_args: dispatched.append("called")},
        available_capabilities={"evidence.search"},
    ).execute(plan)

    assert dispatched == []
    assert [(receipt.status, receipt.error_code, receipt.message) for receipt in result.receipts] == [
        ("blocked", "unsupported_capability", "missing capabilities: document.authoring"),
    ]


def test_executor_blocks_writes_when_the_plan_has_unresolved_references():
    """Executor callers must retain the ambiguity guard even without pre-validation."""
    from src.agents.orchestration.executor import ConversationActionExecutor

    dispatched: list[str] = []
    plan = ConversationActionPlan(
        plan_id="ambiguous-target",
        goal="那个脚不要了",
        unresolved_refs=["ambiguous_pin_reference"],
        actions=[ActionSpec(action_id="write", kind="create_document")],
    )

    result = ConversationActionExecutor(
        handlers={"create_document": lambda *_args: dispatched.append("called")},
        available_capabilities={"document.authoring"},
    ).execute(plan)

    assert dispatched == []
    assert [(receipt.status, receipt.error_code) for receipt in result.receipts] == [
        ("blocked", "unresolved_reference"),
    ]
