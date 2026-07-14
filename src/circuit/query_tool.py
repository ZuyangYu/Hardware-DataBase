"""Public entry point for the circuit domain agent (plan §3.1 / §3.2).

``query_circuit_data`` is the tool signature surfaced to the top-level router
and LLM function router. It is a thin facade that constructs a
``CircuitQueryAgent`` (passing through any injected dependencies for testing)
and delegates to ``CircuitQueryAgent.query``. All orchestration, recovery and
answer synthesis lives in ``query_agent.py``.
"""

from __future__ import annotations

from src.circuit.answer_synthesizer import CircuitAnswerSynthesizer
from src.circuit.entity_resolver import EntityResolver
from src.circuit.query_agent import CircuitQueryAgent
from src.circuit.query_context import CircuitToolResponse
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.recovery_manager import RecoveryManager
from src.circuit.response_policy import CircuitResponsePolicySelector
from src.circuit.session_context_store import SessionContextStore


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
) -> CircuitToolResponse:
    """EDF/PDF circuit query entry point (plan §3.2 / §3.3).

    Routes the question through the bounded Plan-and-Execute agent:
    intent parse → scope resolve → entity resolve → bounded plan → execute →
    synthesize, with recovery when a step comes back empty.
    """
    _ensure_generation_model_bound()
    agent = CircuitQueryAgent(
        engine=engine,
        session_store=session_store,
        entity_resolver=entity_resolver,
        recovery_manager=recovery_manager,
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
