"""R3 T4: one bounded fanout, a shared actual-call budget, and live ACLs."""

from __future__ import annotations

import threading
import time

import pytest

import src.settings as settings
from src.agents.tools import retrieval_executor as pools
from src.pipelines.document_rag.schemas import Evidence, ScopedDocumentRetrievalOutcome, ScopedRetrievalError
from tests import test_knowledge_scope as scope_fixtures

auth_scope = scope_fixtures.auth_scope
resolve_scope = scope_fixtures.resolve_scope


@pytest.fixture(autouse=True)
def pool(monkeypatch):
    pools.reset_retrieval_executor(deadline_seconds=1)
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True)
    monkeypatch.setattr(settings, "RETRIEVAL_EXECUTOR_MAX_WORKERS", 1)
    pools.initialize_retrieval_executor()
    yield
    pools.reset_retrieval_executor(deadline_seconds=1)


def test_single_worker_expands_queries_times_sources_once(auth_scope):
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget

    resolver, snapshot = resolve_scope(auth_scope)
    calls = []

    class Backend:
        def retrieve_with_scope(self, kb_name, query, *, ctx, before_request, **_kwargs):
            before_request()
            calls.append((kb_name, query, ctx.metadata["kb_id"], ctx.metadata["department_id"], len(ctx.kb_permissions)))
            return ScopedDocumentRetrievalOutcome("success_with_hits", [Evidence(id="same-chunk", content=f"{kb_name}:{query}", metadata={"kb_id": ctx.metadata["kb_id"]})])

    budget = RetrievalBudget(max_calls=4, timeout_seconds=1)
    outcome = MultiKnowledgeRetrievalService(resolver, Backend()).search(snapshot=snapshot, queries=["voltage", "current"], budget=budget)
    assert len(calls) == 4
    assert {call[0] for call in calls} == {"A", "B"}
    assert {call[4] for call in calls} == {1}
    assert budget.calls_used == 4
    assert len(outcome.evidence) == 4
    assert {entry.status for entry in outcome.source_outcomes} == {"success_with_hits"}


def test_call_budget_is_shared_across_queries_and_sources(auth_scope):
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget

    resolver, snapshot = resolve_scope(auth_scope)
    calls = []

    class Backend:
        def retrieve_with_scope(self, _kb, query, *, before_request, **_kwargs):
            before_request()
            calls.append(query)
            return ScopedDocumentRetrievalOutcome("success_empty")

    result = MultiKnowledgeRetrievalService(resolver, Backend()).search(snapshot=snapshot, queries=["one", "two"], budget=RetrievalBudget(max_calls=2, timeout_seconds=1))
    assert len(calls) == 2
    assert [entry.status for entry in result.source_outcomes].count("budget_exhausted") == 2
    assert result.partial is True


def test_revoke_during_retrieval_terminates_entire_scope(auth_scope):
    from src.services.knowledge_scope import KnowledgeScopeDenied
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget

    auth, _, _, owner, _ = auth_scope
    resolver, snapshot = resolve_scope(auth_scope)

    class Backend:
        def retrieve_with_scope(self, kb_name, *_args, before_request, **_kwargs):
            before_request()
            if kb_name == "B":
                auth.set_knowledge_base_sharing("B", owner, False)
            return ScopedDocumentRetrievalOutcome("success_with_hits", [Evidence(id="e", content="private")])

    with pytest.raises(KnowledgeScopeDenied):
        MultiKnowledgeRetrievalService(resolver, Backend()).search(snapshot=snapshot, queries=["question"], budget=RetrievalBudget(max_calls=4, timeout_seconds=2))


def test_deadline_discards_running_result_without_waiting_for_thread(auth_scope):
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget

    resolver, snapshot = resolve_scope(auth_scope)
    release = threading.Event()

    class Backend:
        def retrieve_with_scope(self, *_args, before_request, **_kwargs):
            before_request()
            release.wait(timeout=2)
            return ScopedDocumentRetrievalOutcome("success_with_hits", [Evidence(id="late", content="too late")])

    started = time.monotonic()
    try:
        result = MultiKnowledgeRetrievalService(resolver, Backend()).search(snapshot=snapshot, queries=["question"], budget=RetrievalBudget(max_calls=4, timeout_seconds=0.05))
        assert time.monotonic() - started < 0.5
        assert result.evidence == []
        assert {entry.status for entry in result.source_outcomes} == {"timeout"}
        assert pools.get_retrieval_executor().stats()["pending"] >= 1
    finally:
        release.set()


def test_explicit_empty_frozen_mapping_never_expands_to_chat(auth_scope):
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget

    resolver, snapshot = resolve_scope(auth_scope)

    class Backend:
        def retrieve_with_scope(self, *_args, **_kwargs):
            pytest.fail("empty frozen source set cannot query")

    result = MultiKnowledgeRetrievalService(resolver, Backend()).search(snapshot=snapshot, queries=["question"], budget=RetrievalBudget(max_calls=4, timeout_seconds=1), document_contracts={})
    assert result.evidence == []
    assert result.source_outcomes == []


def test_arbitrary_source_ref_or_unselected_contract_is_rejected(auth_scope):
    from src.services.knowledge_scope import KnowledgeScopeInvalid
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget
    from src.pipelines.document_rag.schemas import DocumentRetrievalContract

    resolver, snapshot = resolve_scope(auth_scope)
    service = MultiKnowledgeRetrievalService(resolver, object())
    for kwargs in [{"source_ref": "unselected-kb-name"}, {"source_ref": "kb:999999"}, {"document_contracts": {999999: DocumentRetrievalContract()}}]:
        with pytest.raises(KnowledgeScopeInvalid):
            service.search(snapshot=snapshot, queries=["question"], budget=RetrievalBudget(max_calls=4, timeout_seconds=1), **kwargs)


def test_budget_reservation_is_atomic_and_cancelled_calls_are_not_charged():
    from concurrent.futures import ThreadPoolExecutor
    from src.services.multi_kb_retrieval import RetrievalBudget

    budget = RetrievalBudget(max_calls=3, timeout_seconds=1)

    def reserve(_item):
        try:
            budget.before_attempt()
            return "admitted"
        except ScopedRetrievalError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(reserve, range(20)))
    assert result.count("admitted") == 3
    assert result.count("budget_exhausted") == 17
    assert budget.calls_used == 3
    cancelled = RetrievalBudget(max_calls=3, timeout_seconds=1, should_cancel=lambda: True)
    with pytest.raises(ScopedRetrievalError, match="cancelled"):
        cancelled.before_attempt()
    assert cancelled.calls_used == 0


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf"])
def test_config_rejects_unbounded_or_nonpositive_multi_kb_deadline(seconds):
    with pytest.raises(ValueError):
        settings.validate_settings_values({"MULTI_KB_RETRIEVAL_DEADLINE_SECONDS": seconds})
