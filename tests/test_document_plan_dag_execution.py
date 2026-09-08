from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.document_authoring.harness.graph import (
    PlanExecutionRouteError,
    select_execution_route,
)
from src.document_authoring.harness.langgraph_state import initial_authoring_state
from src.document_authoring.harness.plan_graph import (
    PlanDAGExecutionError,
    PlanDAGExecutor,
)
from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    TemplateVersion,
)
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.planning.models import DocumentPlan
from src.document_authoring.planning.task_graph import TaskGraphCompiler

from tests.test_document_planning_contracts import _plan_payload


def _plan(**overrides) -> DocumentPlan:
    return DocumentPlan.model_validate(_plan_payload(**overrides))


def _plan_with_dependency() -> DocumentPlan:
    base = _plan_payload()
    base["dependency_edges"] = [
        {"upstream_task_id": "task-cover", "downstream_task_id": "task-pins"},
    ]
    base["unit_tasks"][1]["dependencies"] = ["task-cover"]
    base["unit_tasks"][1]["barrier"] = "table-inputs"
    return DocumentPlan.model_validate(base)


def test_run_and_manifest_retain_exact_plan_graph_identity() -> None:
    manifest = AuthoringRunManifest(
        run_manifest_id="manifest-1",
        work_order_id="wo-1",
        harness_policy_id="policy-1",
        harness_policy_version="1",
        writer_provider_id="managed",
        prompt_version="1",
        source_set_snapshot_id="source-1",
        input_fingerprint="fp-1",
        task_graph_id="document-plan:plan-1:v1",
        task_graph_version=1,
        task_graph_hash="sha256:graph",
        document_plan_id="plan-1",
        document_plan_version=1,
        document_plan_hash="sha256:plan",
        execution_route="plan_dag",
    )
    run = HarnessRun(
        harness_run_id="run-1",
        work_order_id="wo-1",
        run_manifest_id=manifest.run_manifest_id,
        task_graph_id=manifest.task_graph_id,
        task_graph_version=manifest.task_graph_version,
        task_graph_hash=manifest.task_graph_hash,
        document_plan_id=manifest.document_plan_id,
        document_plan_version=manifest.document_plan_version,
        document_plan_hash=manifest.document_plan_hash,
        execution_route="plan_dag",
    )

    assert run.task_graph_id == manifest.task_graph_id
    assert run.task_graph_hash == manifest.task_graph_hash
    assert run.document_plan_hash == manifest.document_plan_hash
    assert run.execution_route == "plan_dag"


def test_initial_plan_state_contains_graph_refs_and_node_statuses() -> None:
    state = initial_authoring_state(
        work_order_id="wo-1",
        harness_run_id="run-1",
        run_manifest_id="manifest-1",
        source_set_snapshot_id="source-1",
        input_fingerprint="fp-1",
        schema_version="1",
        unit_ids=["cover", "pins"],
        document_plan_id="plan-1",
        document_plan_version=1,
        document_plan_hash="sha256:plan",
        task_graph_id="graph-1",
        task_graph_version=1,
        task_graph_hash="sha256:graph",
    )

    assert state["document_plan_id"] == "plan-1"
    assert state["task_graph_hash"] == "sha256:graph"
    assert state["node_statuses"] == {}


@pytest.mark.parametrize(
    ("order", "enabled", "expected"),
    [
        (SimpleNamespace(), False, "legacy_schema"),
        (
            SimpleNamespace(
                document_plan_id="plan-1",
                document_plan_version=1,
                document_plan_hash="sha256:plan",
            ),
            False,
            "legacy_schema",
        ),
        (
            SimpleNamespace(
                document_plan_id="plan-1",
                document_plan_version=1,
                document_plan_hash="sha256:plan",
            ),
            True,
            "plan_dag",
        ),
    ],
)
def test_execution_route_is_explicit_and_flag_gated(order, enabled, expected) -> None:
    assert select_execution_route(order, enabled=enabled) == expected


def test_execution_route_rejects_partial_plan_binding_instead_of_falling_back() -> None:
    with pytest.raises(PlanExecutionRouteError, match="incomplete"):
        select_execution_route(
            SimpleNamespace(document_plan_id="plan-1", document_plan_version=1),
            enabled=True,
        )


def test_plan_executor_waits_for_dependencies_and_barriers() -> None:
    graph = TaskGraphCompiler().compile(_plan_with_dependency())
    calls: list[str] = []

    def execute_unit(node, attempt):
        calls.append(node.node_id)
        return {"node_id": node.node_id, "attempt": attempt}

    result = PlanDAGExecutor(max_workers=2).run(graph, execute_unit=execute_unit)

    assert calls == ["unit:task-cover", "unit:task-pins"]
    assert result.statuses["unit:task-cover"] == "committed"
    assert result.statuses["unit:task-pins"] == "committed"
    assert result.statuses["barrier:table-inputs"] == "committed"
    assert result.statuses["release"] == "committed"
    assert result.completed_nodes == graph.topological_order


def test_plan_executor_reuses_committed_receipts_and_recovers_after_failure() -> None:
    graph = TaskGraphCompiler().compile(_plan_with_dependency())
    calls: list[tuple[str, int]] = []
    failed_once = {"value": False}

    def execute_unit(node, attempt):
        calls.append((node.node_id, attempt))
        if node.node_id == "unit:task-pins" and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("temporary retrieval failure")
        return node.node_id

    with pytest.raises(PlanDAGExecutionError, match="temporary retrieval"):
        PlanDAGExecutor().run(graph, execute_unit=execute_unit)

    recovered = PlanDAGExecutor().run(
        graph,
        execute_unit=execute_unit,
        committed_receipts={"unit:task-cover": "cover-receipt"},
    )

    assert ("unit:task-cover", 1) not in calls[1:]
    assert ("unit:task-pins", 1) in calls
    assert recovered.statuses["release"] == "committed"


def test_runtime_create_run_binds_compiled_graph_when_flag_is_enabled(tmp_path, monkeypatch) -> None:
    import src.settings

    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", True)
    plan = _plan(status="accepted")
    order = DocumentWorkOrder(
        work_order_id="wo-plan",
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="ADAS",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id=plan.source_snapshot_id,
        template_version_id="template-001",
        document_schema_id="schema-001",
        document_schema_version="1",
        template_schema_id="schema-001",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="internal_harness",
        harness_policy_id="policy-1",
        harness_policy_version="1",
        requested_executor="internal_harness",
        input_fingerprint_version=3,
        output_spec_id=plan.output_spec_id,
        output_spec_version=plan.output_spec_version,
        output_spec_hash=plan.output_spec_hash,
        document_plan_id=plan.document_plan_id,
        document_plan_version=plan.version,
        document_plan_hash=plan.plan_hash,
        created_by="user-a",
    )
    policy = HarnessPolicy(
        harness_policy_id="policy-1", version="1", status="approved",
        writer_provider_id="managed",
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id=plan.source_snapshot_id,
        tenant_id="tenant-a", knowledge_base_name="ADAS", source_names=["design.pdf"],
        created_by="user-a",
    )
    template = TemplateVersion(
        template_version_id="template-001", template_id="template-001",
        format="xlsx", content_hash="template-hash", template_schema_id="schema-001",
        template_schema_version="1", renderer_policy_id="renderer-1",
    )
    schema = DocumentSchema(
        document_schema_id="schema-001", version="1", document_type="icd",
        status="approved", execution_mode="internal_harness",
    )
    authoring_store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts")
    )
    authoring_store.create_work_order(order)
    runtime = InternalDocumentHarnessRuntime(
        # The runtime should initialize the graph store against the same DB.
        store=authoring_store,
    )

    run, manifest = runtime.create_run(
        order, policy, snapshot, template, schema, document_plan=plan,
    )

    assert manifest.execution_route == "plan_dag"
    assert manifest.task_graph_id == run.task_graph_id
    assert manifest.task_graph_hash == run.task_graph_hash
    assert manifest.document_plan_hash == plan.plan_hash
    assert runtime.task_graphs.get(
        run.task_graph_id, run.task_graph_version, run.task_graph_hash,
        tenant_id="tenant-a", user_id="user-a", task_id=None,
    ) is not None
