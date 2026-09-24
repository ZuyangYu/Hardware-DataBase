from __future__ import annotations

from copy import deepcopy

import pytest

from src.document_authoring.planning.models import DocumentPlan
from src.document_authoring.planning.task_graph import (
    CompiledTaskGraph,
    TaskGraphCompileError,
    TaskGraphCompiler,
)

from tests.test_document_planning_contracts import _plan_payload


def _plan(**overrides) -> DocumentPlan:
    payload = _plan_payload(**overrides)
    return DocumentPlan.model_validate(payload)


def test_compiler_creates_stable_pipeline_and_topological_order() -> None:
    plan = _plan(
        dependency_edges=[
            {"upstream_task_id": "task-cover", "downstream_task_id": "task-pins"},
        ],
        unit_tasks=[
            {
                "task_id": "task-cover",
                "unit_id": "cover",
                "plan_version": 1,
                "dependencies": [],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "allowed_sources": ["kb.read@1"],
                "allowed_retrievers": ["default@1"],
                "allowed_tools": ["retrieve"],
                "action_key": "document-plan:plan-001:cover",
            },
            {
                "task_id": "task-pins",
                "unit_id": "pins",
                "plan_version": 1,
                "dependencies": ["task-cover"],
                "barrier": "table-inputs",
                "input_schema": {"type": "table"},
                "output_schema": {"type": "table"},
                "row_scope": "all_selected_pins",
                "allowed_sources": ["kb.read@1"],
                "allowed_retrievers": ["default@1"],
                "allowed_tools": ["retrieve"],
                "action_key": "document-plan:plan-001:pins",
            },
        ],
    )

    first = TaskGraphCompiler().compile(plan)
    second = TaskGraphCompiler().compile(DocumentPlan.model_validate(plan.model_dump(mode="json")))

    assert isinstance(first, CompiledTaskGraph)
    assert first.graph_id == "document-plan:plan-001:v1"
    assert first.graph_hash == second.graph_hash
    assert first.topological_order.index("preflight") == 0
    assert first.topological_order.index("unit:task-cover") < first.topological_order.index("unit:task-pins")
    assert "barrier:table-inputs" in first.topological_order
    assert first.topological_order[-4:] == [
        "aggregate",
        "pre-render-review",
        "render",
        "post-render-review",
    ] or first.topological_order[-5:] == [
        "aggregate",
        "pre-render-review",
        "render",
        "post-render-review",
        "release",
    ]


def test_compiler_rejects_cycle_and_dangling_dependency() -> None:
    cyclic = _plan(
        dependency_edges=[
            {"upstream_task_id": "task-cover", "downstream_task_id": "task-pins"},
            {"upstream_task_id": "task-pins", "downstream_task_id": "task-cover"},
        ],
        unit_tasks=[
            {
                **_plan_payload()["unit_tasks"][0],
                "dependencies": ["task-pins"],
            },
            {
                **_plan_payload()["unit_tasks"][1],
                "dependencies": ["task-cover"],
            },
        ],
    )
    with pytest.raises(TaskGraphCompileError, match="cycle"):
        TaskGraphCompiler().compile(cyclic)

    dangling_payload = _plan_payload(
        dependency_edges=[],
        unit_tasks=[
            {
                **_plan_payload()["unit_tasks"][0],
                "dependencies": ["missing-task"],
            },
            _plan_payload()["unit_tasks"][1],
        ],
    )
    dangling_payload["dependency_edges"] = []
    # DocumentPlan validates the task dependency reference before compilation.
    with pytest.raises(ValueError, match="unknown task IDs"):
        DocumentPlan.model_validate(dangling_payload)


def test_compiler_rejects_non_executable_plan_before_creating_graph() -> None:
    blocked = _plan(issues=[{
        "code": "layout_capability_unavailable",
        "severity": "error",
        "message": "template-free layout is unavailable",
        "blocking": True,
    }])

    with pytest.raises(TaskGraphCompileError, match="executable"):
        TaskGraphCompiler().compile(blocked)


def test_graph_hash_changes_for_dependency_or_action_key_but_not_mapping_order() -> None:
    baseline = _plan()
    reordered_payload = deepcopy(_plan_payload())
    reordered_payload["layout_contract"]["bindings"] = {"pins": ["A10"], "cover": ["A1"]}

    first = TaskGraphCompiler().compile(baseline)
    reordered = TaskGraphCompiler().compile(DocumentPlan.model_validate(reordered_payload))
    assert first.graph_hash == reordered.graph_hash

    changed_dependency = _plan(
        dependency_edges=[
            {"upstream_task_id": "task-cover", "downstream_task_id": "task-pins"},
        ],
        unit_tasks=[
            _plan_payload()["unit_tasks"][0],
            {**_plan_payload()["unit_tasks"][1], "dependencies": ["task-cover"]},
        ],
    )
    changed_action = _plan(unit_tasks=[
        {**_plan_payload()["unit_tasks"][0], "action_key": "document-plan:plan-001:cover:v2"},
        _plan_payload()["unit_tasks"][1],
    ])
    assert first.graph_hash != TaskGraphCompiler().compile(changed_dependency).graph_hash
    assert first.graph_hash != TaskGraphCompiler().compile(changed_action).graph_hash


def test_compiled_graph_rejects_duplicate_node_ids_and_mismatched_hash() -> None:
    plan = _plan()
    graph = TaskGraphCompiler().compile(plan)
    payload = graph.model_dump(mode="json")
    payload["nodes"].append(payload["nodes"][0])
    with pytest.raises(ValueError, match="node"):
        CompiledTaskGraph.model_validate(payload)

    mismatched = graph.model_dump(mode="json")
    mismatched["graph_hash"] = "sha256:wrong"
    with pytest.raises(ValueError, match="graph_hash"):
        CompiledTaskGraph.model_validate(mismatched)
