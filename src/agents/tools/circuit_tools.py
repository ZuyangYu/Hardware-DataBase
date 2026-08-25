"""Circuit (EDIF/EDF netlist) structured retrieval tool."""

from __future__ import annotations

from src.circuit.index_service import CircuitIndexService


def make_circuit_search(rt, circuit_service: CircuitIndexService):
    """Return a ``circuit_search(query, top_k)`` tool closure."""

    def circuit_search(query: str, top_k: int = rt.top_k) -> str:
        """在知识库中检索电路设计（EDF/EDIF 网表）的结构化信息：网络、器件实例、模块、模块间连接、电源/偏置/保护拓扑等。"""
        from src.agents.tools.runtime import format_evidence_for_llm, timed_tool_call

        def run(query: str, top_k: int):
            return circuit_service.query(
                kb_name=rt.kb_name,
                query=query,
                ctx=rt.ctx,
                top_k=top_k,
            )

        items = timed_tool_call(rt, "circuit_search", query, None, lambda: run(query, max(1, min(int(top_k), 20))))
        return format_evidence_for_llm(items)

    return circuit_search
