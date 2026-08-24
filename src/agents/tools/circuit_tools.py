from __future__ import annotations

import logging

from src.agents.state import Evidence
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
        if operation not in ALLOWED_QUERY_OPERATIONS:
            logger.warning("Rejected illegal circuit_query.query_operation: %r", operation)
            raise ValueError(f"Unsupported circuit_query.query_operation: {operation!r}")
        self._require_department_context(ctx)
        if operation == "resolved_connections":
            leaked = {key for key in filters if str(key).casefold() in _FORBIDDEN_RESOLVED_FILTERS}
            if leaked:
                raise ValueError(
                    "resolved_connections consumes the service-side resolution result; "
                    f"caller-provided refdes filters are not allowed: {sorted(leaked)}"
                )
            return self.index_service.typed_query(
                kb_name=kb_name,
                operation=operation,
                query=query,
                ctx=ctx,
                top_k=top_k,
            )
        if operation != "auto":
            return self.index_service.typed_query(
                kb_name=kb_name,
                operation=operation,
                query=query,
                ctx=ctx,
                top_k=top_k,
            )
        return self.index_service.query(
            kb_name=kb_name,
            query=query,
            ctx=ctx,
            top_k=top_k,
            filters=filters,
        )

    @staticmethod
    def _require_department_context(ctx: RequestContext | None) -> None:
        department_id = str((getattr(ctx, "metadata", {}) or {}).get("department_id") or "")
        if not department_id:
            raise PermissionError(
                "circuit_query requires department context and knowledge-base read permission."
            )
