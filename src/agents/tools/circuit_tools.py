"""Circuit (EDIF/EDF netlist) structured retrieval tool."""

from __future__ import annotations

import logging

from src.agents.schemas import Evidence
from src.circuit.index_service import CircuitIndexService
from src.pipelines.document_rag.schemas import RequestContext

logger = logging.getLogger(__name__)

# Typed read-model contract for the main agent's ``circuit_query`` tool. The
# whitelist is enforced at both the tool and the service boundary.
ALLOWED_QUERY_OPERATIONS = (
    "auto",
    "structure_overview",
    "module_list",
    "resolve_identity",
    "resolved_connections",
)

_FORBIDDEN_RESOLVED_FILTERS = frozenset({"refdes", "component_refdes", "resolved_refdes"})


def _require_department_context(ctx: RequestContext | None) -> None:
    department_id = str((getattr(ctx, "metadata", {}) or {}).get("department_id") or "")
    if not department_id:
        raise PermissionError(
            "circuit_query requires department context and knowledge-base read permission."
        )


def _validate_operation(operation: str, filters: dict) -> None:
    if operation not in ALLOWED_QUERY_OPERATIONS:
        logger.warning("Rejected illegal circuit_query.query_operation: %r", operation)
        raise ValueError(f"Unsupported circuit_query.query_operation: {operation!r}")
    if operation == "resolved_connections":
        leaked = {key for key in filters if str(key).casefold() in _FORBIDDEN_RESOLVED_FILTERS}
        if leaked:
            raise ValueError(
                "resolved_connections consumes the service-side resolution result; "
                f"caller-provided refdes filters are not allowed: {sorted(leaked)}"
            )


def make_circuit_search(rt, circuit_service: CircuitIndexService):
    """Return a ``circuit_search(query, top_k, query_operation)`` tool closure."""

    def circuit_search(query: str, top_k: int = rt.top_k, query_operation: str = "auto") -> str:
        """在知识库中检索电路设计（EDF/EDIF 网表）的结构化信息：网络、器件实例、模块、模块间连接、电源/偏置/保护拓扑等。
        query_operation 可选：structure_overview | module_list | resolve_identity | resolved_connections，默认 auto。"""
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        def run(query: str, top_k: int):
            return _authorized_circuit_query(
                circuit_service,
                kb_name=rt.kb_name,
                ctx=rt.ctx,
                query=query,
                top_k=top_k,
                operation=str(query_operation or "auto"),
                filters={},
            )

        items, adds_nothing = timed_tool_call(rt, "circuit_search", query, None, lambda: run(query, max(1, min(int(top_k), 20))))
        return format_tool_result(rt, adds_nothing, items)

    return circuit_search


def _authorized_circuit_query(
    circuit_service: CircuitIndexService,
    *,
    kb_name: str,
    ctx: RequestContext | None,
    query: str,
    top_k: int,
    operation: str,
    filters: dict,
) -> list[Evidence]:
    """Whitelist + department authorization shared by every circuit entrypoint."""
    _validate_operation(operation, dict(filters or {}))
    _require_department_context(ctx)
    if operation != "auto":
        return circuit_service.typed_query(
            kb_name=kb_name,
            operation=operation,
            query=query,
            ctx=ctx,
            top_k=top_k,
        )
    return circuit_service.query(
        kb_name=kb_name,
        query=query,
        ctx=ctx,
        top_k=top_k,
        filters=filters,
    )


class CircuitQueryTool:
    name = "circuit_query"
    description = (
        "Retrieve structured evidence from archived circuit design files such as "
        "EDF or EDIF netlists. filters.query_operation must be one of: "
        + "|".join(ALLOWED_QUERY_OPERATIONS)
    )

    def __init__(self, index_service: CircuitIndexService | None = None):
        self.index_service = index_service or CircuitIndexService()

    def run(
        self,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        filters = dict(filters or {})
        operation = str(filters.get("query_operation") or "auto")
        return _authorized_circuit_query(
            self.index_service,
            kb_name=kb_name,
            ctx=ctx,
            query=query,
            top_k=top_k,
            operation=operation,
            filters=filters,
        )
