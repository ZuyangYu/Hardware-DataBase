from __future__ import annotations

from src.agents.state import Evidence
from src.circuit.index_service import CircuitIndexService
from src.pipelines.document_rag.schemas import RequestContext


class CircuitQueryTool:
    name = "circuit_query"
    description = "Retrieve structured evidence from archived circuit design files such as EDF or EDIF netlists."

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
        return self.index_service.query(
            kb_name=kb_name,
            query=query,
            ctx=ctx,
            top_k=top_k,
            filters=filters,
        )
