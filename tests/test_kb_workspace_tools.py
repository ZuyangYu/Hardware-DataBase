"""Typed KB tools: documents search/read/list; tables/circuits stay separate."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.agents.schemas import Evidence
from src.agents.tools.kb_workspace_tools import make_kb_workspace_tools
from src.agents.tools.runtime import ToolRuntime
from src.agents.turn_meta import _general_subagent_spec
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


class _Semantic:
    def __init__(self):
        self.calls = 0

    def run(self, query, top_k=8, target=None):
        self.calls += 1
        return [
            Evidence(
                id="document:semantic-1",
                content="文档语义命中：TCN2 电源树。",
                source_name="825504380_ADAS_SCH_TCN2.pdf",
                content_kind="document_text",
                processor_kind="ragflow",
                locator={"chunk_index": 9},
                score=0.91,
                metadata={"chunk_id": "rag-9", "local_record_id": 36},
            )
        ]


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


def _table_record():
    return SimpleNamespace(
        id=29,
        kb_id=4,
        kb_name="AAA",
        department_id="1739",
        document_id="doc-29",
        original_file_name="ICD.xlsx",
        document_name="ICD.xlsx",
        content_kind="spreadsheet_table",
        processor_kind="spreadsheet_table",
        status="indexed",
        content_hash="hash-29",
        source_group="ICD",
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


def _tools(*, document_manager=None, backend=None, semantic=None, records=None):
    rt = _runtime()
    backend = backend or _Backend(_chunks())
    return rt, make_kb_workspace_tools(
        rt,
        backend,
        _Store(records or [_record()]),
        document_manager=document_manager,
        semantic_search=semantic,
    )


def test_list_knowledge_files_returns_kind():
    _, (search, read, files) = _tools(records=[_record(), _table_record()])

    payload = json.loads(files("**/*"))

    assert payload["count"] == 2
    kinds = {row["file_id"]: row["kind"] for row in payload["files"]}
    assert kinds == {"36": "document", "29": "table"}
    only_docs = json.loads(files("**/*", kind="document"))
    assert only_docs["count"] == 1


def test_search_documents_exact_registers_evidence_with_citation():
    rt, (search, read, files) = _tools()

    payload = json.loads(search("X2000", mode="exact", top_k=10))

    assert payload["mode"] == "exact"
    assert payload["result_count"] == 1
    assert payload["evidence_count"] == 1
    first = payload["results"][0]
    assert first["file_name"].endswith("TCN2.pdf")
    assert first["locator"]["page_num"] == 12
    assert "X2000" in first["snippet"]
    assert first["citation_number"] == 1
    assert len(rt.evidence) == 1
    evidence = rt.evidence[0]
    assert evidence.content.startswith("DEBUG CONNECTOR X2000")
    assert evidence.metadata["source_type"] == "knowledge_base_document"
    assert evidence.metadata["kb_id"] == 4
    assert evidence.metadata["local_record_id"] == 36


def test_repeated_search_reuses_citation_without_duplicating_evidence():
    rt, (search, read, files) = _tools()

    first = json.loads(search("X2000", mode="exact", top_k=10))
    second = json.loads(search("X2000", mode="exact", top_k=10))

    assert first["results"][0]["citation_number"] == 1
    assert second["results"][0]["citation_number"] == 1
    assert len(rt.evidence) == 1


def test_search_documents_auto_detects_exact_term():
    _, (search, read, files) = _tools()

    payload = json.loads(search("TPS62872", top_k=5))

    assert payload["mode"] == "exact"
    assert payload["result_count"] == 1


def test_search_documents_semantic_uses_document_search_runner():
    semantic = _Semantic()
    _, (search, read, files) = _tools(semantic=semantic)

    payload = json.loads(search("TCN2 电源树", mode="semantic", top_k=5))

    assert payload["mode"] == "semantic"
    assert semantic.calls == 1
    assert payload["result_count"] == 1
    assert payload["results"][0]["file_id"] == "36"
    assert payload["results"][0]["rag_id"] == "rag-9"


def test_read_document_returns_text_and_registers_evidence():
    rt, (search, read, files) = _tools()

    payload = json.loads(read("36", start_index=0, max_chunks=1))

    assert payload["file_id"] == "36"
    assert payload["chunk_count"] == 2
    assert len(payload["chunks"]) == 1
    assert "X2000" in payload["chunks"][0]["content"]
    assert payload["chunks"][0]["citation_number"] == 1
    assert payload["evidence_count"] == 1
    assert len(rt.evidence) == 1
    assert rt.evidence[0].metadata["source_type"] == "knowledge_base_document"
    assert payload["next_index"] == 1


def test_read_document_rejects_non_document():
    _, (search, read, files) = _tools(records=[_table_record()])

    payload = json.loads(read("ICD.xlsx"))

    assert payload["kind"] == "table"
    assert "not a document" in payload["error"]


def test_document_manager_is_preferred_over_raw_rag_backend():
    raw = _Backend(_chunks(), fail=True)
    manager = _Backend(_chunks())
    _, (search, read, files) = _tools(document_manager=manager, backend=raw)

    payload = json.loads(search("X2000", mode="exact"))

    assert payload["result_count"] == 1
    assert manager.calls == 1
    assert raw.calls == 0


def test_general_subagent_tools_keep_parent_runtime_bindings():
    """Subagent tools are the same rt-bound closures as the main agent."""

    rt, tools = _tools()
    spec = _general_subagent_spec(tools=tools, model=object())

    search = next(
        tool for tool in spec["tools"] if tool.__name__ == "search_documents"
    )
    payload = json.loads(search("X2000", mode="exact"))

    assert payload["evidence_count"] == 1
    assert len(rt.evidence) == 1
    assert rt.evidence[0].metadata["local_record_id"] == 36


def test_directory_scope_and_relative_paths_in_typed_tools(tmp_path):
    from src.pipelines.document_store import PipelineDocumentStore
    from src.services.pipeline_directories import PipelineDirectoryService

    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    directories = PipelineDirectoryService(store=store)
    docs = directories.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="docs", created_by="tester",
    )
    design = directories.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="design", created_by="tester",
    )
    for directory, suffix in ((docs, "docs"), (design, "design")):
        store.upsert_document(
            kb_name="kb",
            document_name="spec.pdf",
            original_file_name="spec.pdf",
            dataset_kind="design",
            dataset_id="dataset",
            document_id=f"remote-{suffix}",
            source_group="文档资料",
            department_id="dept",
            uploaded_by="tester",
            kb_id=7,
            directory_id=directory["id"],
            status="parsed",
            content_hash=f"hash-{suffix}",
        )
    rt = ToolRuntime(
        kb_name="kb",
        ctx=SimpleNamespace(metadata={"department_id": "dept", "kb_id": 7}),
    )
    backend = _Backend(
        [ParsedChunk(index=0, content="spec.pdf 电源树", metadata={"ragflow_chunk_id": "c1"})]
    )
    search, read, files = make_kb_workspace_tools(rt, backend, store)

    payload = json.loads(search("电源树", mode="exact", top_k=10))
    assert payload["result_count"] == 2
    assert {row["file_name"] for row in payload["results"]} == {
        "docs/spec.pdf",
        "design/spec.pdf",
    }
    assert {item.source_name for item in rt.evidence} == {
        "docs/spec.pdf",
        "design/spec.pdf",
    }

    scoped = json.loads(
        search(
            "电源树",
            mode="exact",
            directory_id=docs["id"],
            recursive=False,
        )
    )
    assert scoped["result_count"] == 1
    assert scoped["results"][0]["file_name"] == "docs/spec.pdf"

    listing = json.loads(files("**/*"))
    assert {row["relative_path"] for row in listing["files"]} == {
        "docs/spec.pdf",
        "design/spec.pdf",
    }
    scoped_listing = json.loads(
        files("**/*", directory_id=design["id"], recursive=False)
    )
    assert [row["relative_path"] for row in scoped_listing["files"]] == [
        "design/spec.pdf"
    ]
