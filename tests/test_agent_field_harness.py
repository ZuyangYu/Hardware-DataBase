"""Focused contracts for the bounded external-agent executor adapter."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.document_authoring.harness.agent_loop import (
    AgentFieldHarness,
    AgentToolNotAllowed,
    ExecutorSelection,
    HarnessExecutorSelectionError,
    HarnessPolicyError,
    REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE,
    REASON_AGENT_MODE_DISABLED,
    REASON_AGENT_TOOLS_NOT_IMPLEMENTED,
    agent_thread_id,
    select_executor,
    select_harness_executor,
)
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
)


def _schema(mode: str = "external_agent", *field_ids: str) -> DocumentSchema:
    return DocumentSchema(
        document_schema_id="schema",
        version="1",
        document_type="checklist",
        status="approved",
        execution_mode=mode,
        fields=[
            DocumentFieldSchema(
                field_id=field_id,
                label=field_id,
                retrieval_policy_id=f"retrieval-{field_id}",
                verification_policy_id=f"verify-{field_id}",
            )
            for field_id in (field_ids or ("pending",))
        ],
    )


def _order(mode: str = "external_agent", **overrides) -> DocumentWorkOrder:
    payload = dict(
        work_order_id="work-order",
        project_id="project",
        baseline_id="baseline",
        baseline_content_hash="baseline-hash",
        source_set_snapshot_id="snapshot",
        template_version_id="template",
        document_schema_id="schema",
        document_schema_version="1",
        template_schema_id="template-schema",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode=mode,
        requested_executor=mode,
        harness_policy_id="policy",
        harness_policy_version="1",
        created_by="tester",
    )
    if mode == "deterministic_only":
        payload.pop("harness_policy_id")
        payload.pop("harness_policy_version")
    payload.update(overrides)
    return DocumentWorkOrder.model_validate(payload)


def _policy(**overrides) -> HarnessPolicy:
    payload = dict(
        harness_policy_id="policy",
        version="1",
        status="approved",
    )
    payload.update(overrides)
    return HarnessPolicy.model_validate(payload)


def _run(**overrides) -> HarnessRun:
    payload = dict(
        harness_run_id="run-1",
        work_order_id="work-order",
        run_manifest_id="manifest",
    )
    payload.update(overrides)
    return HarnessRun.model_validate(payload)


@dataclass
class Fallback:
    calls: list = field(default_factory=list)
    result: object = object()

    def execute(self, context):
        self.calls.append(context)
        return self.result


@dataclass
class FieldFallback:
    calls: list = field(default_factory=list)

    def run_field(self, field_id, context):
        self.calls.append((field_id, context))
        return {"field_id": field_id}


def test_external_four_gates_select_agent_but_skeleton_falls_back_on_same_run():
    schema = _schema("external_agent", "committed", "pending")
    order = _order()
    policy = _policy(agent_tools=["read_field_brief"], allowed_tools=[])
    run = _run(unit_statuses={"field:committed": "committed"})
    fallback = Fallback()

    selection = select_harness_executor(
        schema=schema,
        work_order=order,
        policy=policy,
        fallback_executor=fallback,
        agent_mode_enabled=True,
        harness_run=run,
    )
    assert isinstance(selection, ExecutorSelection)
    assert isinstance(selection.executor, AgentFieldHarness)
    assert selection.effective_executor == "agent_field_harness"
    assert selection.gates == {
        "schema_order_requested_consistent": True,
        "feature_flag": True,
        "approved_policy": True,
        "agent_infrastructure": True,
    }

    result = selection.executor.run(
        work_order=order,
        harness_run=run,
        schema=schema,
        policy=policy,
    )
    assert result is fallback.result
    assert len(fallback.calls) == 1
    assert fallback.calls[0].harness_run is run
    assert fallback.calls[0].field_ids == ("pending",)
    assert run.effective_executor == "authoring_graph"
    assert run.degraded_reasons == [REASON_AGENT_TOOLS_NOT_IMPLEMENTED]
    assert run.agent_thread_id == agent_thread_id("run-1", "pending")
    assert selection.executor.last_execution.fallback_field_ids == ["pending"]


def test_field_fallback_uses_run_field_and_skips_committed_units():
    schema = _schema("external_agent", "already", "todo-a", "todo-b")
    order = _order()
    policy = _policy()
    run = _run(unit_statuses={"field:already": "ready_to_render"})
    fallback = FieldFallback()
    harness = AgentFieldHarness(fallback, policy=policy)

    result = harness.run(
        work_order=order,
        harness_run=run,
        schema=schema,
        policy=policy,
    )
    assert result == {"field_id": "todo-a"}
    assert [field_id for field_id, _context in fallback.calls] == ["todo-a", "todo-b"]
    assert all(context.harness_run is run for _field_id, context in fallback.calls)
    assert run.degraded_reasons == [REASON_AGENT_TOOLS_NOT_IMPLEMENTED]
    assert harness.agent_thread_ids == {
        "todo-a": agent_thread_id("run-1", "todo-a"),
        "todo-b": agent_thread_id("run-1", "todo-b"),
    }


def test_flag_or_infrastructure_only_are_legal_degradation_reasons():
    schema = _schema()
    order = _order()
    policy = _policy()

    disabled = select_harness_executor(
        schema=schema, work_order=order, policy=policy,
        fallback_executor=Fallback(), agent_mode_enabled=False,
    )
    assert disabled.effective_executor == "authoring_graph"
    assert disabled.degraded_reasons == [REASON_AGENT_MODE_DISABLED]

    unavailable = select_harness_executor(
        schema=schema, work_order=order, policy=policy,
        fallback_executor=Fallback(), agent_mode_enabled=True,
        agent_infrastructure_available=False,
    )
    assert unavailable.effective_executor == "authoring_graph"
    assert unavailable.degraded_reasons == [REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE]


def test_schema_or_requested_executor_mismatch_is_not_silently_degraded():
    with pytest.raises(HarnessExecutorSelectionError, match="executor_mismatch"):
        select_harness_executor(
            schema=_schema("internal_harness"),
            work_order=_order("external_agent"),
            policy=_policy(),
            fallback_executor=Fallback(),
            agent_mode_enabled=False,
        )

    with pytest.raises(HarnessExecutorSelectionError, match="executor_mismatch"):
        select_harness_executor(
            schema=_schema(),
            work_order=_order(),
            policy=_policy(),
            requested_executor="internal_harness",
            fallback_executor=Fallback(),
            agent_mode_enabled=False,
        )


def test_invalid_policy_fails_closed_even_when_flag_is_off():
    with pytest.raises(HarnessPolicyError, match="policy_not_approved"):
        select_harness_executor(
            schema=_schema(), work_order=_order(),
            policy=_policy(status="draft"), fallback_executor=Fallback(),
            agent_mode_enabled=False,
        )

    with pytest.raises(HarnessPolicyError, match="policy_hash_mismatch"):
        select_harness_executor(
            schema=_schema(), work_order=_order(),
            policy=_policy(), fallback_executor=Fallback(),
            expected_policy_hash="not-the-policy-hash", agent_mode_enabled=False,
        )


def test_deterministic_mode_does_not_require_or_validate_agent_policy():
    selection = select_harness_executor(
        schema=_schema("deterministic_only"),
        work_order=_order("deterministic_only"),
        fallback_executor=Fallback(),
        agent_mode_enabled=True,
    )
    assert selection.effective_executor == "deterministic_rule"
    assert selection.degraded_reasons == []


def test_agent_profile_is_restricted_and_agent_tools_do_not_use_graph_allowlist():
    policy = _policy(agent_tools=["read_field_brief"], allowed_tools=[])
    harness = AgentFieldHarness(Fallback(), policy=policy)
    assert harness.visible_tools == ("read_field_brief",)
    assert "filesystem" not in harness.visible_tools
    assert "command" not in harness.visible_tools
    with pytest.raises(AgentToolNotAllowed, match="agent_tool_not_allowed"):
        harness.require_agent_tool("filesystem")

    with pytest.raises(HarnessPolicyError, match="agent_tool_not_allowed"):
        invalid_policy = _policy().model_dump(mode="json")
        invalid_policy["agent_tools"] = ["shell"]
        select_harness_executor(
            schema=_schema(), work_order=_order(),
            policy=invalid_policy, fallback_executor=Fallback(),
            agent_mode_enabled=True,
        )


def test_stable_thread_id_is_independent_of_harness_instance():
    first = AgentFieldHarness(Fallback(), policy=_policy())
    second = AgentFieldHarness(Fallback(), policy=_policy())
    assert first.agent_thread_id("run-1", "field-a") == second.agent_thread_id("run-1", "field-a")
    assert first.agent_thread_id("run-1", "field-a") != first.agent_thread_id("run-1", "field-b")


def test_select_executor_legacy_shape_returns_protocol_executor():
    executor = select_executor(
        schema=_schema(), work_order=_order(), policy=_policy(),
        fallback_executor=Fallback(), agent_mode_enabled=True,
    )
    assert isinstance(executor, AgentFieldHarness)
    assert executor.effective_executor == "agent_field_harness"
