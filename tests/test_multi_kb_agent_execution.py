"""R3 T3: durable multi-KB scopes reach the agent execution boundary."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from src.agents.tools.runtime import ToolRuntime
from src.pipelines.document_rag.schemas import Evidence as ScopedEvidence
from tests.test_multi_kb_store_contract import snapshot


def test_document_search_uses_one_shared_multi_kb_service_and_preserves_source_identity():
    from src.agents.tools.document_rag_tool import make_document_search

    scope = snapshot(1)
    calls = []

    class Service:
        def search(self, **kwargs):
            calls.append(kwargs)
            return type("Outcome", (), {
                "evidence": [
                    ScopedEvidence(
                        id="chunk-a",
                        content="A evidence",
                        source_name="a.pdf",
                        metadata={
                            "kb_id": 101,
                            "department_id": 7,
                            "knowledge_source": scope.sources[0].model_dump(mode="json"),
                        },
                        score=0.9,
                    ),
                    ScopedEvidence(
                        id="chunk-b",
                        content="B evidence",
                        source_name="b.pdf",
                        metadata={
                            "kb_id": 205,
                            "department_id": 7,
                            "knowledge_source": scope.sources[1].model_dump(mode="json"),
                        },
                        score=0.8,
                    ),
                ],
            })()

    class Backend:
        def retrieve(self, *_args, **_kwargs):
            pytest.fail("multi-KB execution must not call the legacy single-KB retrieve path")

    rt = ToolRuntime(
        kb_name="KB101",
        ctx=None,
        knowledge_scope=scope,
        multi_kb_retrieval=Service(),
        retrieval_budget=object(),
        question="question",
        evidence_budget=8,
    )
    result = make_document_search(rt, Backend(), None)("question", 4)
    assert calls and calls[0]["snapshot"] is scope
    assert {item.metadata["kb_id"] for item in rt.evidence} == {101, 205}
    assert "KB101" in result or "a.pdf" in result
    assert "b.pdf" in result


def test_multi_kb_runner_does_not_mount_or_prefetch_long_term_memory(monkeypatch):
    from src.agents import runner as runner_mod

    captured = {}

    class Backend:
        def retrieve(self, *_args, **_kwargs):
            return []

    class Agent:
        def stream(self, *_args, **_kwargs):
            yield AIMessage(content="ok"), {"langgraph_node": "model"}

    def create_agent(**kwargs):
        captured.update(kwargs)
        return Agent()

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", create_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **_: None)
    memory_factory_called = []

    def memory_factory():
        memory_factory_called.append(True)
        raise AssertionError("multi-KB turns must isolate long-term memory")

    scope = snapshot(1)
    runner = runner_mod.MultiSourceAgentRunner(
        rag_backend=Backend(), memory_service_factory=memory_factory
    )
    list(
        runner.stream(
            query="question",
            kb_name="KB101",
            history=[],
            knowledge_scope=scope,
            thread_id="multi-kb-test",
        )
    )
    names = {
        str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))
        for tool in captured["tools"]
    }
    assert "memory_search" not in names
    assert "circuit_search" not in names
    assert "spreadsheet_row_search" not in names
    assert "spreadsheet_cell_lookup" not in names
    assert "spreadsheet_schema_lookup" not in names
    assert "spreadsheet_sql_query" not in names
    assert "conversation_search" not in names
    assert not memory_factory_called
    assert runner.get_last_retrieval_summary()["source_dependencies"]["kb_ids"] == [101, 205]


def test_query_generator_forwards_persisted_knowledge_scope():
    from src.api.routes.query import _query_generator

    scope = snapshot(1)
    captured = {}

    class Pipeline:
        def query(self, *args, knowledge_scope=None, **kwargs):
            captured["knowledge_scope"] = knowledge_scope
            captured.update(kwargs)
            return iter(())

    list(
        _query_generator(
            Pipeline(), "question", "KB101", [], None, "thread", None,
            knowledge_scope=scope,
        )
    )
    assert captured["knowledge_scope"] is scope


def test_multi_kb_batch_expands_inside_one_service_call_without_nested_pool():
    from src.agents.tools.document_rag_tool import make_document_search_batch

    scope = snapshot(1)
    calls = []

    class Service:
        def search(self, **kwargs):
            calls.append(kwargs)
            return type("Outcome", (), {"evidence": []})()

    rt = ToolRuntime(
        kb_name="KB101",
        ctx=None,
        knowledge_scope=scope,
        multi_kb_retrieval=Service(),
        retrieval_budget=object(),
    )
    make_document_search_batch(rt, object(), None)(["q1", "q2"], 3)
    assert len(calls) == 1
    assert calls[0]["queries"] == ["q1", "q2"]


def _strict_scoped_evidence(scope, source_index, content, *, score=0.9):
    from hashlib import sha256

    source = scope.sources[source_index]
    return ScopedEvidence(
        id=f"chunk-{source.kb_id}",
        content=content,
        source_name=f"{source.kb_name}.pdf",
        score=score,
        metadata={
            "tenant_key": scope.tenant_key,
            "department_id": source.department_id,
            "kb_id": source.kb_id,
            "knowledge_source": source.model_dump(mode="json"),
            "processor_kind": "ragflow",
            "local_record_id": source.kb_id,
            "source_content_hash": f"{source.kb_id:064x}",
            "processing_artifact_id": "artifact-1",
            "index_revision": 1,
            "remote_document_id": f"remote-{source.kb_id}",
            "chunk_or_structured_locator": f"chunk-{source.kb_id}",
            "evidence_text_hash": sha256(content.encode("utf-8")).hexdigest(),
        },
    )


def test_multi_kb_document_tool_fuses_candidates_and_surfaces_partial_source_status():
    from src.agents.tools.document_rag_tool import make_document_search

    scope = snapshot(1)
    class Service:
        def search(self, **_kwargs):
            return type("Outcome", (), {
                "evidence": [
                    _strict_scoped_evidence(scope, 0, "A evidence"),
                    _strict_scoped_evidence(scope, 1, "B evidence", score=0.8),
                ],
                "source_outcomes": [],
            })()

    outcome = Service().search()
    outcome.source_outcomes = [
        type("Source", (), {"kb_id": 101, "query": "question", "retriever_kind": "document", "status": "success_with_hits", "evidence": [outcome.evidence[0]], "error_code": None, "diagnostics": {}})(),
        type("Source", (), {"kb_id": 205, "query": "question", "retriever_kind": "document", "status": "timeout", "evidence": [], "error_code": "timeout", "diagnostics": {}})(),
    ]

    class Backend:
        def retrieve(self, *_args, **_kwargs):
            pytest.fail("multi-KB execution must not call legacy backend")

    class ServiceWithOutcome(Service):
        def search(self, **_kwargs):
            return outcome

    rt = ToolRuntime(
        kb_name="KB101", ctx=None, knowledge_scope=scope,
        multi_kb_retrieval=ServiceWithOutcome(), retrieval_budget=object(),
        question="question", evidence_budget=8,
    )
    result = make_document_search(rt, Backend(), None)("question", 4)
    assert "A evidence" in result
    assert "来源检索未完成" in result
    assert "timeout" in result
    assert len(rt.evidence) == 1
    assert rt.evidence[0].metadata["fusion_rank"] == 1

    from src.agents.runner import MultiSourceAgentRunner

    summary = MultiSourceAgentRunner(rag_backend=Backend())._build_retrieval_summary(rt, [], {})
    assert summary["status"] == "partial_failure"
    assert summary["error_message"] == "timeout"
    assert summary["multi_kb_source_outcomes"][1]["status"] == "timeout"
    assert summary["multi_kb_fusions"][0]["coverage"] == {101: True, 205: False}


@pytest.mark.parametrize("batch", [False, True], ids=["single-query", "batch-query"])
@pytest.mark.parametrize("source_status, expected", [
    ("success_empty", "no_evidence"),
    ("timeout", "failed"),
    ("budget_exhausted", "failed"),
])
def test_multi_kb_empty_results_distinguish_failed_sources_from_zero_hits(batch, source_status, expected):
    from src.agents.runner import MultiSourceAgentRunner
    from src.agents.tools.document_rag_tool import make_document_search, make_document_search_batch
    from src.services.knowledge_scope_models import KnowledgeSourceRetrievalOutcome, MultiKnowledgeRetrievalOutcome

    scope = snapshot(1)

    class Service:
        def search(self, **kwargs):
            return MultiKnowledgeRetrievalOutcome([], [
                KnowledgeSourceRetrievalOutcome(
                    source.kb_id, query, "document", source_status, [],
                    None if source_status == "success_empty" else source_status,
                )
                for query in kwargs["queries"] for source in scope.sources
            ], scope.selection_hash)

    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=scope,
                     multi_kb_retrieval=Service(), retrieval_budget=object())
    if batch:
        rendered = make_document_search_batch(rt, object(), None)(["voltage", "current"])
    else:
        rendered = make_document_search(rt, object(), None)("voltage")
    summary = MultiSourceAgentRunner(rag_backend=object())._build_retrieval_summary(rt, [], {})
    assert summary["status"] == expected
    assert len(summary["multi_kb_source_outcomes"]) == (4 if batch else 2)
    assert ("来源检索未完成" in rendered) == (source_status != "success_empty")
    assert rt.evidence == []


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "concurrent"])
@pytest.mark.parametrize("same_evidence", [False, True], ids=["distinct-evidence", "same-evidence"])
@pytest.mark.parametrize("token_limit, item_limit", [(8000, 24), (32000, 2)])
def test_multi_kb_tool_calls_share_the_turn_context_and_item_budget(monkeypatch, parallel, same_evidence, token_limit, item_limit):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from src.agents.tools.document_rag_tool import make_document_search
    from src.services.knowledge_scope_models import KnowledgeSourceRetrievalOutcome, MultiKnowledgeRetrievalOutcome

    monkeypatch.setattr("src.settings.MULTI_KB_CONTEXT_MAX_TOKENS", token_limit)
    monkeypatch.setattr("src.settings.MULTI_KB_EVIDENCE_MAX_ITEMS", item_limit)
    scope = snapshot(1)
    barrier = Barrier(2)

    class Service:
        def search(self, **kwargs):
            query = kwargs["queries"][0]
            if parallel:
                barrier.wait(timeout=5)
            content = ("same" if same_evidence else query) + "电" * 1100
            items = [_strict_scoped_evidence(scope, index, content) for index in (0, 1)]
            return MultiKnowledgeRetrievalOutcome(items, [
                KnowledgeSourceRetrievalOutcome(source.kb_id, query, "document", "success_with_hits", [item])
                for source, item in zip(scope.sources, items)
            ], scope.selection_hash)

    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=scope,
                     multi_kb_retrieval=Service(), retrieval_budget=object(), evidence_budget=30)
    search = make_document_search(rt, object(), None)
    if parallel:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rendered = list(pool.map(search, ["first", "second"]))
    else:
        rendered = [search("first"), search("second")]
    assert len(rt.evidence) == 2
    assert {item.metadata["kb_id"] for item in rt.evidence} == {101, 205}
    assert sum(item.metadata["context_token_estimate"] for item in rt.evidence) <= token_limit
    assert sum("电" * 1100 in output for output in rendered) == 1
    audits = [fusion["runtime_admission"] for fusion in rt.multi_kb_fusions]
    assert sum(audit["admitted_items"] for audit in audits) == (4 if same_evidence else 2)
    assert sum(audit["budget_discarded"] for audit in audits) == (0 if same_evidence else 2)
    assert all(audit["turn_context_used"] <= token_limit for audit in audits)


def test_multi_kb_changed_query_does_not_render_the_same_evidence_body_again():
    from src.agents.tools.document_rag_tool import make_document_search
    from src.services.knowledge_scope_models import KnowledgeSourceRetrievalOutcome, MultiKnowledgeRetrievalOutcome

    scope = snapshot(1)
    content = "stable evidence body"

    class Service:
        def search(self, **kwargs):
            old = _strict_scoped_evidence(scope, 0, content)
            items = [old]
            if kwargs["queries"][0] == "mixed":
                items.append(_strict_scoped_evidence(scope, 1, "new source evidence"))
            return MultiKnowledgeRetrievalOutcome(items, [
                KnowledgeSourceRetrievalOutcome(item.metadata["kb_id"], kwargs["queries"][0],
                                                "document", "success_with_hits", [item])
                for item in items
            ], scope.selection_hash)

    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=scope,
                     multi_kb_retrieval=Service(), retrieval_budget=object())
    search = make_document_search(rt, object(), None)
    assert content in search("first")
    repeated = search("different")
    assert content not in repeated
    assert "[1]" in repeated
    mixed = search("mixed")
    assert content not in mixed
    assert "[1]" in mixed
    assert "new source evidence" in mixed
    assert len(rt.evidence) == 2
