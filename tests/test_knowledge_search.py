"""Capability-layer retrieval fans out queries without exposing adapters."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.tools.knowledge_search import make_knowledge_search


def _runtime():
    return SimpleNamespace(check_cancel=lambda: None)


def test_knowledge_search_fans_out_queries_and_sources():
    calls: list[tuple] = []

    def document_batch(queries, top_k=6):
        calls.append(("document", tuple(queries), top_k))
        return "DOC-CANDIDATES"

    def grep(patterns, max_hits=20):
        calls.append(("grep", tuple(patterns), max_hits))
        return "GREP-CANDIDATES"

    def circuit(query, top_k=6):
        calls.append(("circuit", query, top_k))
        return f"CIRCUIT:{query}"

    tool = make_knowledge_search(
        _runtime(),
        document_search_batch=document_batch,
        kb_grep=grep,
        circuit_search=circuit,
    )

    rendered = tool(
        queries=["TC377 MCU", "LPDDR4", "CANFD"],
        source_types=["document", "grep", "circuit"],
        top_k=5,
    )

    assert ("document", ("TC377 MCU", "LPDDR4", "CANFD"), 5) in calls
    assert ("grep", ("TC377 MCU", "LPDDR4", "CANFD"), 15) in calls
    assert ("circuit", "TC377 MCU", 5) in calls
    assert "DOC-CANDIDATES" in rendered
    assert "GREP-CANDIDATES" in rendered
    assert "CIRCUIT:TC377 MCU" in rendered
    assert "【检索覆盖】" in rendered
    assert "evidence_read" in rendered


def test_knowledge_search_skips_unrequested_and_unavailable_sources():
    calls: list[str] = []

    def grep(patterns, max_hits=20):
        calls.append("grep")
        return "GREP"

    tool = make_knowledge_search(_runtime(), kb_grep=grep)

    rendered = tool(queries=["TC377"], source_types=["grep", "wiki"], top_k=3)

    assert calls == ["grep"]
    assert "未配置/不可用：wiki" in rendered


def test_knowledge_search_rejects_empty_queries():
    tool = make_knowledge_search(_runtime())

    assert "未提供有效查询" in tool(queries=[" ", ""], source_types=["grep"])
