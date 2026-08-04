"""Public entry point for the circuit domain agent (plan §3.1 / §3.2).

``query_circuit_data`` is the legacy tool signature surfaced to function
routers. It now requires request authorization context and supplies the domain
agent with a read-only, department-scoped store while holding the circuit index
reader lock for the complete query.
"""

from __future__ import annotations

import os
from typing import Any

from src.circuit.answer_synthesizer import CircuitAnswerSynthesizer
from src.circuit.entity_resolver import EntityResolver
from src.circuit.index_lock import circuit_index_read_lock
from src.circuit.index_service import CircuitIndexService
from src.circuit.query_agent import CircuitQueryAgent
from src.circuit.query_context import CircuitToolResponse
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.recovery_manager import RecoveryManager
from src.circuit.response_policy import CircuitResponsePolicySelector
from src.circuit.session_context_store import SessionContextStore
from src.pipelines.document_rag.schemas import RequestContext


class _AuthorizedCircuitStore:
    """Read-only store view restricted to one authorized KB/design set."""

    def __init__(self, delegate, kb_name: str, allowed_design_ids: frozenset[str]):
        self._delegate = delegate
        self._kb_name = kb_name
        self._allowed_design_ids = allowed_design_ids
        self.root = delegate.root

    def _allows(self, kb_name: str, design_id: str) -> bool:
        return kb_name == self._kb_name and design_id in self._allowed_design_ids

    def list_designs(self, kb_name: str):
        if kb_name != self._kb_name:
            return []
        return [
            design
            for design in self._delegate.list_designs(kb_name)
            if design.design_id in self._allowed_design_ids
        ]

    def load(self, kb_name: str, design_id: str):
        if not self._allows(kb_name, design_id):
            return None
        return self._delegate.load(kb_name, design_id)

    def read_index(self) -> dict[str, Any]:
        index = dict(self._delegate.read_index())
        index["designs"] = [
            entry
            for entry in index.get("designs", [])
            if entry.get("kb_name") == self._kb_name
            and entry.get("design_id") in self._allowed_design_ids
        ]
        return index

    def circuit_version(self, kb_name: str, design_id: str):
        if not self._allows(kb_name, design_id):
            return None
        return self._delegate.circuit_version(kb_name, design_id)

    def list_module_screenshots(self, kb_name: str, design_id: str):
        if not self._allows(kb_name, design_id):
            return []
        return self._delegate.list_module_screenshots(kb_name, design_id)

    def list_pdf_cache(self, kb_name: str, design_id: str):
        if not self._allows(kb_name, design_id):
            return []
        return self._delegate.list_pdf_cache(kb_name, design_id)


class _NoSemanticVectorIndex:
    """Legacy agent path is structured-only; governed semantic lives in the facade."""

    @staticmethod
    def semantic_search(*args, **kwargs):
        return []

    @staticmethod
    def is_available() -> bool:
        return False


def query_circuit_data(
    question: str,
    kb_name: str,
    session_id: str,
    circuit_id: str | None = None,
    circuit_scope: dict | None = None,
    history: list | None = None,
    upstream_hint: dict | None = None,
    engine: CircuitQueryEngine | None = None,
    session_store: SessionContextStore | None = None,
    entity_resolver: EntityResolver | None = None,
    recovery_manager: RecoveryManager | None = None,
    answer_synthesizer: CircuitAnswerSynthesizer | None = None,
    response_policy_selector: CircuitResponsePolicySelector | None = None,
    ctx: RequestContext | None = None,
    index_service: CircuitIndexService | None = None,
) -> CircuitToolResponse:
    """EDF/PDF circuit query entry point (plan §3.2 / §3.3).

    Routes the question through the bounded Plan-and-Execute agent:
    intent parse → scope resolve → entity resolve → bounded plan → execute →
    synthesize, with recovery when a step comes back empty. Legacy injected
    engines/resolvers fail closed because their authorization behavior cannot
    be verified at this public boundary.
    """
    metadata = getattr(ctx, "metadata", {}) or {}
    department_id = str(
        metadata.get("resource_department_id") or metadata.get("department_id") or ""
    )
    if not department_id:
        raise PermissionError("Circuit queries require a department context.")
    if engine is not None and type(engine) is not CircuitQueryEngine:
        raise TypeError("Custom legacy circuit engines cannot be authorization verified.")
    if entity_resolver is not None or recovery_manager is not None:
        raise TypeError("Custom legacy circuit resolvers cannot be authorization verified.")

    if index_service is None:
        index_service = CircuitIndexService(store=engine.store) if engine is not None else CircuitIndexService()
    if engine is not None and os.path.realpath(engine.store.root) != os.path.realpath(index_service.store.root):
        raise ValueError("Circuit engine and authorization service must use the same storage root.")

    _ensure_generation_model_bound()
    with circuit_index_read_lock(index_service.store.root):
        allowed_designs = index_service._allowed_designs_unlocked(kb_name, ctx)
        authorized_store = _AuthorizedCircuitStore(
            index_service.store,
            kb_name,
            frozenset(allowed_designs),
        )
        authorized_engine = CircuitQueryEngine(
            store=authorized_store,
            vector_index=_NoSemanticVectorIndex(),
        )
        agent = CircuitQueryAgent(
            engine=authorized_engine,
            session_store=session_store,
            entity_resolver=EntityResolver(authorized_engine),
            recovery_manager=RecoveryManager(authorized_engine),
            answer_synthesizer=answer_synthesizer,
            response_policy_selector=response_policy_selector,
        )
        return agent.query(
            question=question,
            kb_name=kb_name,
            session_id=session_id,
            circuit_id=circuit_id,
            circuit_scope=circuit_scope,
            history=history,
            upstream_hint=upstream_hint,
        )


def _ensure_generation_model_bound() -> None:
    """Compatibility hook for the pre-agentic circuit query path.

    The old feature branch bound llama_index through ``src.core.model_factory``.
    Latest develop replaced that stack with ``src.core.llm_client`` and the
    LangGraph runner, so this direct tool path stays rule-only until the circuit
    planner/synthesizer is migrated onto the shared LLM client.
    """
    return None
