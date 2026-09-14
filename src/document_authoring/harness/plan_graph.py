"""Dependency-aware execution primitives for compiled document graphs.

The scheduler is deliberately callback-based.  It owns readiness, barriers,
receipt reuse and structural nodes; retrieval, writing and persistence remain
owned by the document-authoring runtime.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from src.document_authoring.planning.task_graph import CompiledTaskGraph, CompiledTaskNode


class PlanDAGExecutionError(RuntimeError):
    """Raised when a plan node cannot be committed safely."""

    def __init__(self, message: str, *, statuses: dict[str, str], node_id: str | None = None):
        super().__init__(message)
        self.statuses = dict(statuses)
        self.node_id = node_id


@dataclass
class PlanDAGExecutionResult:
    statuses: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    receipts: dict[str, Any] = field(default_factory=dict)
    completed_nodes: list[str] = field(default_factory=list)


UnitExecutor = Callable[[CompiledTaskNode, int], Any]
StructuralExecutor = Callable[[CompiledTaskNode, int, PlanDAGExecutionResult], Any]


class PlanDAGExecutor:
    """Run ready graph nodes in stable order and resume from receipts.

    Structural nodes are deterministic control-plane commits.  Unit nodes are
    the only nodes delegated to the runtime callback.  The callback is called
    after all dependencies are committed and is expected to return a value
    that the runtime can persist as a Receipt.
    """

    def __init__(self, *, max_workers: int = 1):
        self.max_workers = max(1, int(max_workers))

    def run(
        self,
        graph: CompiledTaskGraph,
        *,
        execute_unit: UnitExecutor,
        execute_node: StructuralExecutor | None = None,
        committed_receipts: dict[str, Any] | None = None,
    ) -> PlanDAGExecutionResult:
        if not isinstance(graph, CompiledTaskGraph):
            graph = CompiledTaskGraph.model_validate(graph)
        result = PlanDAGExecutionResult()
        node_by_id = {node.node_id: node for node in graph.nodes}
        graph_node_ids = set(node_by_id)
        committed = dict(committed_receipts or {})
        for node_id in committed:
            if node_id not in graph_node_ids:
                raise PlanDAGExecutionError(
                    f"receipt references unknown graph node: {node_id}",
                    statuses=result.statuses,
                    node_id=node_id,
                )
        for node_id in graph.topological_order:
            if node_id not in committed:
                continue
            node = node_by_id[node_id]
            missing_dependencies = [
                dependency
                for dependency in node.dependencies
                if dependency not in committed
            ]
            if missing_dependencies:
                raise PlanDAGExecutionError(
                    "committed receipt bypasses graph dependencies: "
                    f"{node_id} requires {missing_dependencies}",
                    statuses=result.statuses,
                    node_id=node_id,
                )
            result.statuses[node_id] = "committed"
            result.receipts[node_id] = committed[node_id]
            result.outputs[node_id] = committed[node_id]
            result.completed_nodes.append(node_id)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            while len(result.completed_nodes) < len(graph.nodes):
                ready = [
                    node_by_id[node_id]
                    for node_id in graph.topological_order
                    if node_id not in result.statuses
                    and all(
                        result.statuses.get(dependency) == "committed"
                        for dependency in node_by_id[node_id].dependencies
                    )
                ]
                if not ready:
                    raise PlanDAGExecutionError(
                        "compiled graph cannot make progress", statuses=result.statuses,
                    )

                # Structural nodes are barriers/control-plane transitions. A
                # ready structural node is handled alone so its callback sees
                # a complete, deterministic dependency state. Ready unit
                # nodes are the only work fanned out to bounded workers.
                structural = next((node for node in ready if node.kind != "unit"), None)
                if structural is not None:
                    try:
                        output = (
                            execute_node(structural, 1, result)
                            if execute_node is not None else None
                        )
                    except Exception as exc:
                        result.statuses[structural.node_id] = "failed"
                        raise PlanDAGExecutionError(
                            str(exc), statuses=result.statuses, node_id=structural.node_id,
                        ) from exc
                    result.outputs[structural.node_id] = output
                    result.receipts[structural.node_id] = output
                    result.statuses[structural.node_id] = "committed"
                    result.completed_nodes.append(structural.node_id)
                    continue

                batch = ready[: self.max_workers]
                futures = {
                    node.node_id: pool.submit(execute_unit, node, 1)
                    for node in batch
                }
                failure: tuple[str, BaseException] | None = None
                for node in batch:
                    try:
                        output = futures[node.node_id].result()
                    except Exception as exc:
                        result.statuses[node.node_id] = "failed"
                        if failure is None:
                            failure = (node.node_id, exc)
                        continue
                    result.outputs[node.node_id] = output
                    result.receipts[node.node_id] = output
                    result.statuses[node.node_id] = "committed"

                # Merge the batch in graph order, independent of which worker
                # completed first. This keeps checkpoints and test projections
                # deterministic while still allowing the external calls to
                # overlap.
                for node in batch:
                    if result.statuses.get(node.node_id) == "committed":
                        result.completed_nodes.append(node.node_id)
                if failure is not None:
                    node_id, exc = failure
                    raise PlanDAGExecutionError(
                        str(exc), statuses=result.statuses, node_id=node_id,
                    ) from exc
        return result
