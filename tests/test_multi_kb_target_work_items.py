"""Server-compiled work items constrain execution to the resolved targets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.settings as settings
from src.agents.tools import retrieval_executor as pools
from src.pipelines.document_rag.schemas import DocumentRetrievalContract, DocumentSourceStamp, ScopedDocumentRetrievalOutcome
from src.services.knowledge_scope import KnowledgeScopeInvalid
from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget
from src.services.retrieval_targets import RetrievalWorkItem, TargetLocator


@pytest.fixture(autouse=True)
def executor(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True)
    pools.reset_retrieval_executor(deadline_seconds=1)
    pools.initialize_retrieval_executor()
    yield
    pools.reset_retrieval_executor(deadline_seconds=1)


SOURCES = (
    SimpleNamespace(kb_id=5, kb_name="AP230060", department_id="47", role="primary",
                    model_dump=lambda mode="json": {"kb_id": 5, "kb_name": "AP230060",
                                                    "department_id": "47", "role": "primary"}),
    SimpleNamespace(kb_id=6, kb_name="AP230062", department_id="47", role="peer",
                    model_dump=lambda mode="json": {"kb_id": 6, "kb_name": "AP230062",
                                                    "department_id": "47", "role": "peer"}),
)
SNAPSHOT = SimpleNamespace(actor_user_id=2, tenant_key="default", selection_hash="a" * 64,
                           purpose="compare", sources=SOURCES)


class Resolver:
    def revalidate(self, actor_user_id, snapshot):
        return SOURCES

    def context_for_source(self, actor_user_id, source):
        return SimpleNamespace(user_id=actor_user_id, metadata={"kb_id": source.kb_id})


class Backend:
    def __init__(self, status="success_with_hits"):
        self.status = status
        self.calls = []

    def retrieve_with_scope(self, kb_name, query, *, ctx, contract, top_k, **kwargs):
        self.calls.append({"kb_name": kb_name, "query": query, "contract": contract, **kwargs})
        evidence = []
        if self.status == "success_with_hits":
            from src.pipelines.document_rag.schemas import Evidence
            evidence = [Evidence(id=f"e-{kb_name}", content=f"{kb_name} evidence", source_name="f",
                                 score=0.9, metadata={"kb_id": ctx.metadata["kb_id"]})]
        return ScopedDocumentRetrievalOutcome(self.status, evidence=evidence)


def work_item(kind="document", kb_id=5):
    stamp = DocumentSourceStamp(kb_id, 101, "hash", "remote", 1)
    return RetrievalWorkItem(
        work_id="w1", target_ids=("t1",), query_id="q1", kb_id=kb_id,
        retriever_kind=kind, normalized_query="pin 2",
        contract=DocumentRetrievalContract(mode="frozen", allowed_record_ids=(101,),
                                           expected_source_stamps={101: stamp}),
        locators_by_record={101: (TargetLocator("t1", sheet_name="Pin Definition", pin_value="2"),)},
    )


def test_work_items_execute_only_compiled_kb_and_carry_task_labels():
    backend = Backend()
    outcome = MultiKnowledgeRetrievalService(Resolver(), object(), adapters={"document": backend}).search(
        snapshot=SNAPSHOT, queries=[], budget=RetrievalBudget(max_calls=4, timeout_seconds=5),
        work_items=[work_item()],
    )

    assert len(backend.calls) == 1
    assert backend.calls[0]["kb_name"] == "AP230060"
    assert backend.calls[0]["contract"].allowed_record_ids == (101,)
    assert outcome.source_outcomes[0].work_id == "w1"
    assert outcome.source_outcomes[0].target_ids == ("t1",)
    assert outcome.source_outcomes[0].query_id == "q1"
    assert outcome.evidence[0].metadata["work_id"] == "w1"
    assert outcome.evidence[0].metadata["target_ids"] == ["t1"]


def test_work_items_refuse_legacy_query_mixing():
    with pytest.raises(KnowledgeScopeInvalid):
        MultiKnowledgeRetrievalService(Resolver(), object(), adapters={"document": Backend()}).search(
            snapshot=SNAPSHOT, queries=["widened"], budget=RetrievalBudget(max_calls=4, timeout_seconds=5),
            work_items=[work_item()],
        )


def test_work_item_outside_authorized_selection_is_rejected():
    with pytest.raises(KnowledgeScopeInvalid):
        MultiKnowledgeRetrievalService(Resolver(), object(), adapters={"document": Backend()}).search(
            snapshot=SNAPSHOT, queries=[], budget=RetrievalBudget(max_calls=4, timeout_seconds=5),
            work_items=[work_item(kb_id=99)],
        )


def test_partial_failure_reports_one_source_without_faking_other_source():
    backend = Backend(status="source_unavailable")
    outcome = MultiKnowledgeRetrievalService(Resolver(), object(), adapters={"document": backend}).search(
        snapshot=SNAPSHOT, queries=[], budget=RetrievalBudget(max_calls=4, timeout_seconds=5),
        work_items=[work_item()],
    )
    assert outcome.evidence == []
    assert outcome.source_outcomes[0].status == "source_unavailable"
    assert outcome.partial is True


def test_work_item_variants_pick_target_project_query_and_keep_original_obligation():
    from types import SimpleNamespace

    from src.agents.tools.document_rag_tool import _work_item_variants

    target_a = SimpleNamespace(target_id="t1", kb_name="R3_REAL_EPS_AP230060_20261019")
    target_b = SimpleNamespace(target_id="t2", kb_name="R3_REAL_EPS_AP230062_20261019")
    item_a = work_item(kb_id=5)
    item_b = RetrievalWorkItem(
        work_id="w2", target_ids=("t2",), query_id="q2", kb_id=6,
        retriever_kind="document", normalized_query="蓄电池供电工作范围",
        contract=work_item(kb_id=6).contract,
    )
    rt = SimpleNamespace(turn_retrieval_targets=SimpleNamespace(targets=(target_a, target_b)))
    variants = _work_item_variants(rt, [item_a, item_b], [
        "AP230060 KL30 正常工作电压范围", "AP230062 蓄电池供电工作范围",
    ])
    by_work = {}
    for variant in variants:
        by_work.setdefault(variant.work_id, []).append(variant.normalized_query)
    assert by_work["w1"] == ["KL30 正常工作电压范围", "pin 2"]
    assert by_work["w2"] == ["蓄电池供电工作范围"]
