"""Controlled fanout across authorized single-KB adapters; never nested pools."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import replace
from typing import Callable, Mapping, Sequence

import src.settings as settings
from src.agents.tools.retrieval_executor import get_multi_kb_retrieval_executor
from src.pipelines.document_rag.schemas import DocumentRetrievalContract, ScopedDocumentRetrievalOutcome, ScopedRetrievalError
from src.services.knowledge_scope import KnowledgeScopeDenied, KnowledgeScopeInvalid, KnowledgeScopeResolver
from src.services.knowledge_scope_models import (
    KnowledgeScopeSnapshot, KnowledgeSourceRetrievalOutcome, MultiKnowledgeRetrievalOutcome,
)


class RetrievalBudget:
    def __init__(self, *, max_calls: int = 24, timeout_seconds: float = 30, should_cancel: Callable[[], bool] | None = None):
        if max_calls < 1 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("invalid_retrieval_budget")
        self.max_calls = int(max_calls)
        self.deadline_at = time.monotonic() + float(timeout_seconds)
        self._should_cancel = should_cancel
        self._lock = threading.Lock()
        self._calls_used = 0

    @property
    def calls_used(self) -> int:
        with self._lock:
            return self._calls_used

    def cancelled(self) -> bool:
        return bool(self._should_cancel is not None and self._should_cancel())

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    def before_attempt(self) -> None:
        with self._lock:
            if self.cancelled():
                raise ScopedRetrievalError("cancelled")
            if self.remaining_seconds() <= 0:
                raise ScopedRetrievalError("timeout")
            if self._calls_used >= self.max_calls:
                raise ScopedRetrievalError("budget_exhausted")
            self._calls_used += 1


class MultiKnowledgeRetrievalService:
    def __init__(self, resolver: KnowledgeScopeResolver, document_backend, *, adapters: Mapping | None = None):
        self.resolver = resolver
        self.adapters = {"document": document_backend, **dict(adapters or {})}

    def search(
        self, *, snapshot: KnowledgeScopeSnapshot, queries: Sequence[str],
        retriever_kind: str = "document", per_source_top_k: int = 8,
        budget: RetrievalBudget, source_ref: str | None = None,
        document_contracts: Mapping[int, DocumentRetrievalContract] | None = None,
        work_items: Sequence | None = None,
    ) -> MultiKnowledgeRetrievalOutcome:
        sources = self.resolver.revalidate(snapshot.actor_user_id, snapshot)
        selected_ids = {source.kb_id for source in sources}
        if work_items is not None:
            if queries or source_ref is not None or document_contracts is not None:
                raise KnowledgeScopeInvalid("knowledge_scope_work_items_conflict")
            return self._search_work_items(
                snapshot=snapshot, sources=sources, work_items=work_items,
                per_source_top_k=per_source_top_k, budget=budget,
            )
        if source_ref is not None:
            if not re.fullmatch(r"kb:[1-9][0-9]*", source_ref) or int(source_ref[3:]) not in selected_ids:
                raise KnowledgeScopeInvalid("knowledge_scope_source_ref_invalid")
            sources = tuple(s for s in sources if s.kb_id == int(source_ref[3:]))
        if document_contracts is not None:
            if set(document_contracts) - selected_ids or any(c.mode != "frozen" for c in document_contracts.values()):
                raise KnowledgeScopeInvalid("knowledge_scope_document_contract_invalid")
            sources = tuple(s for s in sources if s.kb_id in document_contracts)
        normalized = list(dict.fromkeys(str(q).strip() for q in queries if str(q).strip()))[:budget.max_calls]
        items = [(query, source) for query in normalized for source in sources]
        if not items:
            return MultiKnowledgeRetrievalOutcome([], [], snapshot.selection_hash)
        executor = get_multi_kb_retrieval_executor()
        backend = self.adapters.get(retriever_kind)

        def retrieve_one(item):
            query, source = item
            self.resolver.revalidate(snapshot.actor_user_id, snapshot)
            ctx = self.resolver.context_for_source(snapshot.actor_user_id, source)
            if backend is None:
                return ScopedDocumentRetrievalOutcome("filter_unsupported", error_code="retriever_unsupported")
            contract = document_contracts[source.kb_id] if document_contracts is not None else DocumentRetrievalContract()
            outcome = backend.retrieve_with_scope(
                source.kb_name, query, ctx=ctx, contract=contract,
                top_k=max(1, min(int(per_source_top_k), 20)), should_cancel=budget.cancelled,
                before_request=budget.before_attempt, remaining_seconds=budget.remaining_seconds,
            )
            self.resolver.revalidate(snapshot.actor_user_id, snapshot)
            if outcome.status == "access_denied":
                raise KnowledgeScopeDenied()
            if outcome.status != "success_with_hits":
                return replace(outcome, evidence=[])
            evidence = []
            for rank, item in enumerate(outcome.evidence, 1):
                metadata = dict(item.metadata or {})
                if metadata.get("kb_id") != source.kb_id:
                    return ScopedDocumentRetrievalOutcome("source_unavailable", error_code="source_identity_mismatch")
                metadata.update({"knowledge_source": source.model_dump(mode="json"), "selection_hash": snapshot.selection_hash,
                                 "query": query, "retriever_rank": rank, "raw_score": item.score,
                                 "tenant_key": snapshot.tenant_key})
                evidence.append(replace(item, metadata=metadata))
            return replace(outcome, evidence=evidence)

        results = executor.map_bounded(
            retrieve_one, items, fanout=min(settings.RETRIEVAL_PER_REQUEST_FANOUT, budget.max_calls),
            deadline=float(settings.RETRIEVAL_QUEUE_MAX_WAIT_SECONDS),
            execution_deadline_at=budget.deadline_at, cancel_check=budget.cancelled,
        )
        self.resolver.revalidate(snapshot.actor_user_id, snapshot)
        outcomes = []
        evidence = []
        for result in results:
            query, source = items[result.index]
            if isinstance(result.error, KnowledgeScopeDenied):
                raise result.error
            if result.status == "ok":
                outcome = result.value
            elif isinstance(result.error, ScopedRetrievalError):
                outcome = ScopedDocumentRetrievalOutcome(result.error.status, error_code=result.error.error_code)
            else:
                status = result.status if result.status in {"timeout", "cancelled"} else "retrieval_failed"
                outcome = ScopedDocumentRetrievalOutcome(status, error_code="retrieval_saturated" if result.status == "saturated" else status)
            outcomes.append(KnowledgeSourceRetrievalOutcome(source.kb_id, query, retriever_kind, outcome.status, outcome.evidence, outcome.error_code, outcome.diagnostics))
            evidence.extend(outcome.evidence)
        coverage = {s.kb_id: any(o.kb_id == s.kb_id and o.evidence for o in outcomes) for s in sources}
        return MultiKnowledgeRetrievalOutcome(evidence, outcomes, snapshot.selection_hash, coverage=coverage,
                                            partial=any(o.status not in {"success_with_hits", "success_empty"} for o in outcomes))

    def _search_work_items(
        self, *, snapshot: KnowledgeScopeSnapshot, sources: Sequence,
        work_items: Sequence, per_source_top_k: int, budget: RetrievalBudget,
    ) -> MultiKnowledgeRetrievalOutcome:
        """Execute server-compiled KB x retriever x query work items."""
        sources_by_id = {source.kb_id: source for source in sources}
        selected_ids = set(sources_by_id)
        items = list(work_items)
        if not items:
            return MultiKnowledgeRetrievalOutcome([], [], snapshot.selection_hash)
        for item in items:
            if item.kb_id not in selected_ids:
                raise KnowledgeScopeInvalid("knowledge_scope_work_item_outside_selection")
            if item.contract.mode != "frozen":
                raise KnowledgeScopeInvalid("knowledge_scope_work_item_requires_frozen_contract")
        executor = get_multi_kb_retrieval_executor()

        def retrieve_one(item):
            source = sources_by_id[item.kb_id]
            self.resolver.revalidate(snapshot.actor_user_id, snapshot)
            ctx = self.resolver.context_for_source(snapshot.actor_user_id, source)
            backend = self.adapters.get(item.retriever_kind)
            if backend is None:
                return ScopedDocumentRetrievalOutcome("filter_unsupported", error_code="retriever_unsupported")
            retrieve = getattr(backend, "retrieve_batch_with_scope", None)
            batch_records = len(item.contract.allowed_record_ids or ())
            if retrieve is None or batch_records <= 1:
                retrieve = backend.retrieve_with_scope
                call_top_k = max(1, min(int(per_source_top_k), 20))
            else:
                call_top_k = max(1, min(int(per_source_top_k) * batch_records, 20))
            outcome = retrieve(
                source.kb_name, item.normalized_query, ctx=ctx, contract=item.contract,
                top_k=call_top_k, should_cancel=budget.cancelled,
                before_request=budget.before_attempt, remaining_seconds=budget.remaining_seconds,
                locators_by_record=item.locators_by_record,
            )
            self.resolver.revalidate(snapshot.actor_user_id, snapshot)
            if outcome.status == "access_denied":
                raise KnowledgeScopeDenied()
            if outcome.status != "success_with_hits":
                return replace(outcome, evidence=[])
            evidence = []
            for rank, evidence_item in enumerate(outcome.evidence, 1):
                metadata = dict(evidence_item.metadata or {})
                if metadata.get("kb_id") != source.kb_id:
                    return ScopedDocumentRetrievalOutcome("source_unavailable", error_code="source_identity_mismatch")
                metadata.update({
                    "knowledge_source": source.model_dump(mode="json"),
                    "selection_hash": snapshot.selection_hash,
                    "query": item.normalized_query,
                    "retriever_rank": rank,
                    "raw_score": evidence_item.score,
                    "tenant_key": snapshot.tenant_key,
                    # Task labels travel alongside evidence for diagnostics and
                    # coverage, never as part of the evidence identity.
                    "work_id": item.work_id,
                    "target_ids": list(item.target_ids),
                    "query_id": item.query_id,
                })
                evidence.append(replace(evidence_item, metadata=metadata))
            return replace(outcome, evidence=evidence)

        results = executor.map_bounded(
            retrieve_one, items, fanout=min(settings.RETRIEVAL_PER_REQUEST_FANOUT, max(1, budget.max_calls)),
            deadline=float(settings.RETRIEVAL_QUEUE_MAX_WAIT_SECONDS),
            execution_deadline_at=budget.deadline_at, cancel_check=budget.cancelled,
        )
        self.resolver.revalidate(snapshot.actor_user_id, snapshot)
        outcomes = []
        evidence = []
        for result in results:
            item = items[result.index]
            if isinstance(result.error, KnowledgeScopeDenied):
                raise result.error
            if result.status == "ok":
                outcome = result.value
            elif isinstance(result.error, ScopedRetrievalError):
                outcome = ScopedDocumentRetrievalOutcome(result.error.status, error_code=result.error.error_code)
            else:
                status = result.status if result.status in {"timeout", "cancelled"} else "retrieval_failed"
                outcome = ScopedDocumentRetrievalOutcome(status, error_code="retrieval_saturated" if result.status == "saturated" else status)
            outcomes.append(KnowledgeSourceRetrievalOutcome(
                item.kb_id, item.normalized_query, item.retriever_kind, outcome.status,
                outcome.evidence, outcome.error_code, outcome.diagnostics,
                work_id=item.work_id, target_ids=item.target_ids, query_id=item.query_id,
            ))
            evidence.extend(outcome.evidence)
        targeted = {item.kb_id for item in items}
        coverage = {kb_id: any(o.kb_id == kb_id and o.evidence for o in outcomes) for kb_id in targeted}
        return MultiKnowledgeRetrievalOutcome(evidence, outcomes, snapshot.selection_hash, coverage=coverage,
                                            partial=any(o.status not in {"success_with_hits", "success_empty"} for o in outcomes))
