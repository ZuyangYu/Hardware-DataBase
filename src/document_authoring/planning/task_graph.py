"""Strict, deterministic execution graph compiled from an accepted plan."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import DocumentPlan, UnitTaskSpec, planning_content_hash


NodeKind = Literal[
    "preflight",
    "unit",
    "barrier",
    "aggregate",
    "pre_render_review",
    "render",
    "post_render_review",
    "release",
]
NonEmptyId = Annotated[str, Field(min_length=1, max_length=512)]


class TaskGraphCompileError(ValueError):
    """Raised when a plan cannot be represented as a safe execution graph."""


class _GraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CompiledTaskEdge(_GraphModel):
    upstream_node_id: NonEmptyId
    downstream_node_id: NonEmptyId


class CompiledTaskBarrier(_GraphModel):
    barrier_id: NonEmptyId
    member_node_ids: list[NonEmptyId] = Field(min_length=1, max_length=100_000)
    node_id: NonEmptyId


class CompiledTaskNode(_GraphModel):
    node_id: NonEmptyId
    kind: NodeKind
    task_id: NonEmptyId | None = None
    unit_id: NonEmptyId | None = None
    dependencies: list[NonEmptyId] = Field(default_factory=list, max_length=100_000)
    barrier_id: NonEmptyId | None = None
    input_schema: dict = Field(default_factory=dict, max_length=256)
    output_schema: dict = Field(default_factory=dict, max_length=256)
    action_key: NonEmptyId
    max_attempts: int = Field(default=1, ge=1, le=20)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_identity(self) -> "CompiledTaskNode":
        if self.kind == "unit" and (not self.task_id or not self.unit_id):
            raise ValueError("unit graph nodes require task_id and unit_id")
        if self.kind != "unit" and (self.task_id is not None or self.unit_id is not None):
            raise ValueError("non-unit graph nodes may not carry task identity")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("graph node dependencies must be unique")
        if self.node_id in self.dependencies:
            raise ValueError("graph node may not depend on itself")
        return self


class CompiledTaskGraph(_GraphModel):
    graph_id: NonEmptyId
    version: int = Field(ge=1)
    plan_id: NonEmptyId
    plan_hash: NonEmptyId
    source_snapshot_id: NonEmptyId
    source_snapshot_hash: NonEmptyId
    nodes: list[CompiledTaskNode] = Field(min_length=1, max_length=100_000)
    edges: list[CompiledTaskEdge] = Field(default_factory=list, max_length=200_000)
    barriers: list[CompiledTaskBarrier] = Field(default_factory=list, max_length=100_000)
    topological_order: list[NonEmptyId] = Field(min_length=1, max_length=100_000)
    graph_hash: str | None = None

    @model_validator(mode="after")
    def validate_graph(self) -> "CompiledTaskGraph":
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("graph node IDs must be unique")
        node_id_set = set(node_ids)
        action_keys = [node.action_key for node in self.nodes]
        if len(action_keys) != len(set(action_keys)):
            raise ValueError("graph action keys must be unique")

        edge_pairs = {(edge.upstream_node_id, edge.downstream_node_id) for edge in self.edges}
        if len(edge_pairs) != len(self.edges):
            raise ValueError("graph edges must be unique")
        expected_pairs: set[tuple[str, str]] = set()
        for node in self.nodes:
            for dependency in node.dependencies:
                if dependency not in node_id_set:
                    raise ValueError(f"graph dependency references unknown node: {dependency}")
                expected_pairs.add((dependency, node.node_id))
        if edge_pairs != expected_pairs:
            raise ValueError("graph edges do not match node dependencies")

        order = list(self.topological_order)
        if len(order) != len(node_ids) or set(order) != node_id_set:
            raise ValueError("topological_order must contain every graph node exactly once")
        positions = {node_id: index for index, node_id in enumerate(order)}
        for upstream, downstream in expected_pairs:
            if positions[upstream] >= positions[downstream]:
                raise ValueError("topological_order violates a graph dependency")

        barrier_ids = [barrier.barrier_id for barrier in self.barriers]
        if len(barrier_ids) != len(set(barrier_ids)):
            raise ValueError("graph barrier IDs must be unique")
        for barrier in self.barriers:
            if barrier.node_id not in node_id_set:
                raise ValueError("barrier node is not in graph")
            if set(barrier.member_node_ids) - node_id_set:
                raise ValueError("barrier references an unknown member node")
            if barrier.node_id in barrier.member_node_ids:
                raise ValueError("barrier node cannot be one of its members")

        expected_hash = planning_content_hash(self, exclude={"graph_hash"})
        if self.graph_hash is not None and self.graph_hash != expected_hash:
            raise ValueError("graph_hash does not match the canonical compiled graph")
        object.__setattr__(self, "graph_hash", expected_hash)
        return self


class TaskGraphCompiler:
    """Compile a validated plan into a stable pipeline graph."""

    def compile(self, plan: DocumentPlan) -> CompiledTaskGraph:
        if not isinstance(plan, DocumentPlan):
            plan = DocumentPlan.model_validate(plan)
        if not plan.is_executable:
            raise TaskGraphCompileError("DocumentPlan must be executable before graph compilation")

        task_by_id = {task.task_id: task for task in plan.unit_tasks}
        dependency_sets = self._validate_plan_dependencies(plan, task_by_id)
        graph_id = f"document-plan:{plan.document_plan_id}:v{plan.version}"

        nodes: list[CompiledTaskNode] = [CompiledTaskNode(
            node_id="preflight",
            kind="preflight",
            action_key=f"{graph_id}:preflight",
            max_attempts=1,
        )]
        unit_node_by_task: dict[str, str] = {}
        for task in plan.unit_tasks:
            node_id = f"unit:{task.task_id}"
            unit_node_by_task[task.task_id] = node_id
            dependencies = ["preflight"]
            dependencies.extend(unit_node_by_task[upstream] for upstream in ())
            nodes.append(CompiledTaskNode(
                node_id=node_id,
                kind="unit",
                task_id=task.task_id,
                unit_id=task.unit_id,
                dependencies=dependencies,
                barrier_id=task.barrier,
                input_schema=task.input_schema,
                output_schema=task.output_schema,
                action_key=task.action_key,
                max_attempts=task.max_attempts,
                timeout_seconds=task.timeout_seconds,
            ))

        # Unit nodes are first created in plan order, then their dependency
        # list is patched from the validated plan. This keeps node order
        # stable even when an upstream task appears later in the input list.
        patched_nodes: list[CompiledTaskNode] = []
        for node in nodes:
            if node.kind != "unit":
                patched_nodes.append(node)
                continue
            task_id = node.task_id
            assert task_id is not None
            dependencies = ["preflight"]
            for upstream in dependency_sets[task_id]:
                upstream_task = task_by_id[upstream]
                dependencies.append(
                    f"barrier:{upstream_task.barrier}"
                    if upstream_task.barrier
                    else unit_node_by_task[upstream]
                )
            patched_nodes.append(node.model_copy(update={"dependencies": _unique(dependencies)}))
        nodes = patched_nodes

        barrier_order: list[str] = []
        members: dict[str, list[str]] = defaultdict(list)
        for task in plan.unit_tasks:
            if task.barrier:
                if task.barrier not in members:
                    barrier_order.append(task.barrier)
                members[task.barrier].append(unit_node_by_task[task.task_id])
        barriers: list[CompiledTaskBarrier] = []
        for barrier_id in barrier_order:
            node_id = f"barrier:{barrier_id}"
            member_nodes = members[barrier_id]
            nodes.append(CompiledTaskNode(
                node_id=node_id,
                kind="barrier",
                barrier_id=barrier_id,
                dependencies=member_nodes,
                action_key=f"{graph_id}:barrier:{barrier_id}",
                max_attempts=1,
            ))
            barriers.append(CompiledTaskBarrier(
                barrier_id=barrier_id,
                member_node_ids=member_nodes,
                node_id=node_id,
            ))

        aggregate_dependencies: list[str] = []
        for task in plan.unit_tasks:
            terminal = f"barrier:{task.barrier}" if task.barrier else unit_node_by_task[task.task_id]
            if terminal not in aggregate_dependencies:
                aggregate_dependencies.append(terminal)
        nodes.extend([
            CompiledTaskNode(
                node_id="aggregate",
                kind="aggregate",
                dependencies=aggregate_dependencies,
                action_key=f"{graph_id}:aggregate",
                max_attempts=1,
            ),
            CompiledTaskNode(
                node_id="pre-render-review",
                kind="pre_render_review",
                dependencies=["aggregate"],
                action_key=f"{graph_id}:pre-render-review",
                max_attempts=1,
            ),
            CompiledTaskNode(
                node_id="render",
                kind="render",
                dependencies=["pre-render-review"],
                action_key=f"{graph_id}:render",
                max_attempts=1,
            ),
            CompiledTaskNode(
                node_id="post-render-review",
                kind="post_render_review",
                dependencies=["render"],
                action_key=f"{graph_id}:post-render-review",
                max_attempts=1,
            ),
            CompiledTaskNode(
                node_id="release",
                kind="release",
                dependencies=["post-render-review"],
                action_key=f"{graph_id}:release",
                max_attempts=1,
            ),
        ])

        edges = [
            CompiledTaskEdge(upstream_node_id=dependency, downstream_node_id=node.node_id)
            for node in nodes
            for dependency in node.dependencies
        ]
        topological_order = _stable_topological_order(nodes)
        return CompiledTaskGraph(
            graph_id=graph_id,
            version=plan.version,
            plan_id=plan.document_plan_id,
            plan_hash=plan.plan_hash,
            source_snapshot_id=plan.source_snapshot_id,
            source_snapshot_hash=plan.source_snapshot_hash,
            nodes=nodes,
            edges=edges,
            barriers=barriers,
            topological_order=topological_order,
        )

    @staticmethod
    def _validate_plan_dependencies(
        plan: DocumentPlan,
        task_by_id: dict[str, UnitTaskSpec],
    ) -> dict[str, list[str]]:
        edge_dependencies: dict[str, list[str]] = defaultdict(list)
        for edge in plan.dependency_edges:
            edge_dependencies[edge.downstream_task_id].append(edge.upstream_task_id)

        result: dict[str, list[str]] = {}
        for task in plan.unit_tasks:
            declared = list(task.dependencies)
            from_edges = edge_dependencies.get(task.task_id, [])
            if set(declared) != set(from_edges):
                raise TaskGraphCompileError(
                    f"task dependency declarations disagree for {task.task_id}"
                )
            result[task.task_id] = _unique(declared)

        # A DocumentPlan validates dangling IDs; this explicit graph-side
        # check keeps the compiler safe when called with a future-compatible
        # model or a deserialized object bypassing model validation.
        task_ids = set(task_by_id)
        for task_id, dependencies in result.items():
            unknown = set(dependencies) - task_ids
            if unknown:
                raise TaskGraphCompileError(
                    f"task {task_id} has dangling dependencies: {sorted(unknown)}"
                )
            if task_id in dependencies:
                raise TaskGraphCompileError(f"task dependency cycle contains self edge: {task_id}")
        _assert_acyclic(result)
        return result


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _assert_acyclic(dependencies: dict[str, list[str]]) -> None:
    indegree = {task_id: len(values) for task_id, values in dependencies.items()}
    downstream: dict[str, list[str]] = defaultdict(list)
    for task_id, values in dependencies.items():
        for upstream in values:
            downstream[upstream].append(task_id)
    ready = deque(task_id for task_id, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        current = ready.popleft()
        visited += 1
        for child in downstream[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(dependencies):
        raise TaskGraphCompileError("task dependency cycle detected")


def _stable_topological_order(nodes: list[CompiledTaskNode]) -> list[str]:
    order_index = {node.node_id: index for index, node in enumerate(nodes)}
    indegree = {node.node_id: len(node.dependencies) for node in nodes}
    downstream: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for dependency in node.dependencies:
            downstream[dependency].append(node.node_id)
    ready = sorted(
        (node_id for node_id, degree in indegree.items() if degree == 0),
        key=order_index.__getitem__,
    )
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for child in sorted(downstream[current], key=order_index.__getitem__):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=order_index.__getitem__)
    if len(result) != len(nodes):
        raise TaskGraphCompileError("compiled task graph contains a cycle")
    return result
