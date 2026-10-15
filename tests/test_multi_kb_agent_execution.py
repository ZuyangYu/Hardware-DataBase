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
