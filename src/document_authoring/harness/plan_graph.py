"""Dependency-aware execution primitives for compiled document graphs.

The scheduler is deliberately callback-based.  It owns readiness, barriers,
receipt reuse and structural nodes; retrieval, writing and persistence remain
owned by the document-authoring runtime.
"""

from __future__ import annotations

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
        committed_receipts: dict[str, Any] | None = None,
    ) -> PlanDAGExecutionResult:
        if not isinstance(graph, CompiledTaskGraph):
            graph = CompiledTaskGraph.model_validate(graph)
        result = PlanDAGExecutionResult()
        for node_id, receipt in (committed_receipts or {}).items():
            if node_id not in {node.node_id for node in graph.nodes}:
                raise PlanDAGExecutionError(
                    f"receipt references unknown graph node: {node_id}",
                    statuses=result.statuses,
                    node_id=node_id,
                )
            result.statuses[node_id] = "committed"
            result.receipts[node_id] = receipt
            result.completed_nodes.append(node_id)

        node_by_id = {node.node_id: node for node in graph.nodes}
        while len(result.completed_nodes) < len(graph.nodes):
            made_progress = False
            for node_id in graph.topological_order:
                if node_id in result.statuses:
                    continue
                node = node_by_id[node_id]
                if any(result.statuses.get(dep) != "committed" for dep in node.dependencies):
                    continue
                made_progress = True
                if node.kind == "unit":
                    try:
                        output = execute_unit(node, 1)
                    except Exception as exc:
                        result.statuses[node_id] = "failed"
                        raise PlanDAGExecutionError(
                            str(exc), statuses=result.statuses, node_id=node_id,
                        ) from exc
                    result.outputs[node_id] = output
                    result.receipts[node_id] = output
                result.statuses[node_id] = "committed"
                result.completed_nodes.append(node_id)
            if not made_progress:
                raise PlanDAGExecutionError(
                    "compiled graph cannot make progress", statuses=result.statuses,
                )
        return result
