"""Unified KB tools: search/read/list over the DocumentManager chunk source."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.agents.tools.kb_workspace_tools import make_kb_workspace_tools
from src.agents.tools.runtime import ToolRuntime
from src.pipelines.document_rag.schemas import ParsedChunk, ParseResult


class _Store:
    def __init__(self, records):
        self.records = records

    def list_documents(self, kb_name, department_id):
        return [record for record in self.records if record.kb_name == kb_name]


class _Backend:
    def __init__(self, chunks, *, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.calls = 0

    def get_parse_result(self, kb_name, document_id, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("raw rag backend unavailable")
        return ParseResult(
            document_id=document_id,
            file_name="825504380_ADAS_SCH_TCN2.pdf",
            chunk_count=len(self.chunks),
            chunks=self.chunks,
        )


def _record():
    return SimpleNamespace(
        id=36,
        kb_id=4,
        kb_name="AAA",
        department_id="1739",
        document_id="doc-36",
        original_file_name="825504380_ADAS_SCH_TCN2.pdf",
        document_name="825504380_ADAS_SCH_TCN2.pdf",
        content_kind="document_text",
        processor_kind="ragflow",
        status="parsed",
        content_hash="hash-36",
        source_group="SCH",
    )


def _runtime():
    return ToolRuntime(
        kb_name="AAA",
        ctx=SimpleNamespace(metadata={"department_id": "1739", "kb_id": 4}),
    )


def _chunks():
    return [
        ParsedChunk(
            index=0,
            content="DEBUG CONNECTOR X2000 连接 EQ6 JTAG 与 TC367 JTAG。",
            metadata={"ragflow_chunk_id": "chunk-1", "page_num": 12},
        ),
        ParsedChunk(
            index=1,
            content="U1800 TPS62872 完成 VCC3V3 到 VCC0V75。",
            metadata={"ragflow_chunk_id": "chunk-2", "page_num": 18},
        ),
    ]


def _tools(*, document_manager=None, backend=None):
    rt = _runtime()
    backend = backend or _Backend(_chunks())
    return rt, make_kb_workspace_tools(
        rt, backend, _Store([_record()]), document_manager=document_manager
    )


def test_list_knowledge_files_lists_matching_files():
    _, (search, read, files) = _tools()

    payload = json.loads(files("**/*TCN2*"))

    assert payload["count"] == 1
    assert payload["files"][0]["path"] == "/kb/AAA/825504380_ADAS_SCH_TCN2.pdf"
    assert payload["files"][0]["file_id"] == "36"


def test_search_knowledge_exact_returns_locator_and_snippet_without_evidence():
    rt, (search, read, files) = _tools()

    payload = json.loads(search("X2000", mode="exact", top_k=10))

    assert payload["mode"] == "exact"
    assert payload["result_count"] == 1
    first = payload["results"][0]
    assert first["file_name"].endswith("TCN2.pdf")
    assert first["locator"]["page_num"] == 12
    assert "X2000" in first["snippet"]
    assert rt.evidence == []


def test_search_knowledge_auto_detects_exact_term():
    _, (search, read, files) = _tools()

    payload = json.loads(search("TPS62872", top_k=5))

    assert payload["mode"] == "exact"
    assert payload["result_count"] == 1


def test_search_knowledge_semantic_uses_lexical_ranking():
    _, (search, read, files) = _tools()

    payload = json.loads(search("VCC3V3 到 VCC0V75", mode="semantic", top_k=5))

    assert payload["mode"] == "semantic"
    assert payload["result_count"] >= 1
    assert "TPS62872" in payload["results"][0]["snippet"]


def test_read_knowledge_returns_text_and_next_index():
    _, (search, read, files) = _tools()

    payload = json.loads(read("36", start_index=0, max_chunks=1))

    assert payload["file_id"] == "36"
    assert payload["chunk_count"] == 2
    assert len(payload["chunks"]) == 1
    assert "X2000" in payload["chunks"][0]["content"]
    assert payload["next_index"] == 1


def test_read_knowledge_reports_missing_file():
    _, (search, read, files) = _tools()

    payload = json.loads(read("not-there.pdf"))

    assert "not found" in payload["error"]


def test_document_manager_is_preferred_over_raw_rag_backend():
    raw = _Backend(_chunks(), fail=True)
    manager = _Backend(_chunks())
    _, (search, read, files) = _tools(document_manager=manager, backend=raw)

    payload = json.loads(search("X2000", mode="exact"))

    assert payload["result_count"] == 1
    assert manager.calls == 1
    assert raw.calls == 0
