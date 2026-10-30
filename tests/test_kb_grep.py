"""kb_grep returns non-citable pointers; evidence_read promotes one."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agents.schemas import Evidence
from src.agents.tools.evidence_tools import make_evidence_read
from src.agents.tools.grep_tools import make_kb_grep
from src.agents.tools.runtime import ToolRuntime
from src.pipelines.document_rag.schemas import ParsedChunk, ParseResult


@pytest.fixture(autouse=True)
def _isolated_pointer_index(tmp_path, monkeypatch):
    import src.agents.tools.grep_tools as grep_tools

    monkeypatch.setattr(
        grep_tools,
        "pointer_index_path",
        lambda department_id, kb_name: str(tmp_path / f"{department_id}-{kb_name}.db"),
    )


class _Store:
    def __init__(self, records):
        self.records = records

    def list_documents(self, kb_name, department_id):
        return [record for record in self.records if record.kb_name == kb_name]


class _Backend:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = 0

    def get_parse_result(self, kb_name, document_id, **_kwargs):
        self.calls += 1
        return ParseResult(
            document_id=document_id,
            file_name="600608964_ADAS_HSI.docx",
            chunk_count=len(self.chunks),
            chunks=self.chunks,
        )


def _record():
    return SimpleNamespace(
        id=7,
        kb_id=4,
        kb_name="AAA",
        department_id="1739",
        document_id="doc-7",
        original_file_name="600608964_ADAS_HSI.docx",
        document_name="600608964_ADAS_HSI.docx",
        content_kind="document_text",
        processor_kind="ragflow",
        status="parsed",
        content_hash="hash-7",
        source_group="HSI",
    )


def _runtime():
    return ToolRuntime(
        kb_name="AAA",
        ctx=SimpleNamespace(metadata={"department_id": "1739", "kb_id": 4}),
    )


def test_kb_grep_returns_pointers_without_registering_evidence():
    chunks = [
        ParsedChunk(
            index=0,
            content="TC377 最小系统由主控芯片、晶体和硬件配置组成，VCC3V3_MCU 供电。",
            metadata={"ragflow_chunk_id": "chunk-1", "page_num": 3, "score": 0.8},
        ),
        ParsedChunk(
            index=1,
            content="CAN 收发器连接 X1900。",
            metadata={"ragflow_chunk_id": "chunk-2", "page_num": 4},
        ),
    ]
    rt = _runtime()
    grep = make_kb_grep(rt, _Backend(chunks), _Store([_record()]))

    rendered = grep(["TC377", "VCC3V3"], max_hits=10)

    assert "候选证据" in rendered
    assert "id=grep:7:chunk-1" in rendered
    assert "600608964_ADAS_HSI.docx" in rendered
    assert "page_num=3" in rendered
    assert "CAN 收发器" not in rendered
    assert rt.evidence == []
    assert "grep:7:chunk-1" in rt.candidate_evidence


def test_evidence_read_promotes_a_grep_pointer():
    chunks = [
        ParsedChunk(
            index=0,
            content="TC377 最小系统由主控芯片、晶体和硬件配置组成。",
            metadata={"ragflow_chunk_id": "chunk-1", "page_num": 3},
        )
    ]
    rt = _runtime()
    grep = make_kb_grep(rt, _Backend(chunks), _Store([_record()]))
    grep(["TC377"])

    rendered = make_evidence_read(rt)(["grep:7:chunk-1"])

    assert len(rt.evidence) == 1
    assert rt.evidence[0].metadata["citation_number"] == 1
    assert "[1] 来源" in rendered
    assert rt.candidate_evidence == {}


def test_kb_grep_cache_avoids_repeated_parse_reads():
    backend = _Backend([
        ParsedChunk(
            index=0,
            content="TC377 最小系统。",
            metadata={"ragflow_chunk_id": "chunk-1"},
        )
    ])
    rt = _runtime()
    grep = make_kb_grep(rt, backend, _Store([_record()]))

    grep(["TC377"])
    grep(["TC377"])

    assert backend.calls == 1


class _Spreadsheet:
    def __init__(self, path):
        self.path = str(path)

    def db_path(self, department_id, kb_name, create=True):
        return self.path


class _Circuit:
    def query(self, **_kwargs):
        return [
            Evidence(
                id="circuit:1",
                content="TC377 MCU 的 VCC3V3_MCU 网络",
                source_name="825504380_ADAS_SCH_TCN2.EDF",
                content_kind="circuit_net",
                processor_kind="circuit",
                score=0.9,
                locator={"design": "ADAS", "net": "VCC3V3_MCU"},
                metadata={},
            )
        ]


def test_kb_grep_includes_spreadsheet_rows(tmp_path):
    import sqlite3 as _sqlite3

    db_path = tmp_path / "table_indexes.db"
    with _sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE table_semantic_rows ("
            "record_id INTEGER, sheet_name TEXT, row_index INTEGER, semantic_text TEXT, raw_text TEXT)"
        )
        conn.execute(
            "INSERT INTO table_semantic_rows VALUES (?, ?, ?, ?, ?)",
            (8, "Pin Definition", 29, "TC377 最小系统 VCC3V3_MCU 供电", ""),
        )
    record = SimpleNamespace(
        id=8,
        kb_id=4,
        kb_name="AAA",
        department_id="1739",
        document_id="table:8",
        original_file_name="600606894_ADAS_产品硬件架构设计说明书.xlsx",
        document_name="硬件架构.xlsx",
        content_kind="spreadsheet_table",
        processor_kind="spreadsheet_table",
        status="indexed",
        content_hash="hash-8",
    )
    rt = _runtime()
    grep = make_kb_grep(
        rt, _Backend([]), _Store([record]), spreadsheet_service=_Spreadsheet(db_path)
    )

    rendered = grep(["TC377"])

    assert "id=grep:xlsx:8:Pin Definition:29" in rendered
    assert "600606894_ADAS_产品硬件架构设计说明书.xlsx" in rendered
    assert rt.evidence == []


def test_kb_grep_includes_circuit_net_evidence():
    record = SimpleNamespace(
        id=9,
        kb_id=4,
        kb_name="AAA",
        department_id="1739",
        document_id="circuit:9",
        original_file_name="825504380_ADAS_SCH_TCN2.EDF",
        document_name="原理图网表.EDF",
        content_kind="circuit_net",
        processor_kind="circuit_design",
        status="indexed",
        content_hash="hash-9",
    )
    rt = _runtime()
    grep = make_kb_grep(
        rt, _Backend([]), _Store([record]), circuit_service=_Circuit()
    )

    rendered = grep(["TC377"])

    assert "id=circuit:1" in rendered
    assert "825504380_ADAS_SCH_TCN2.EDF" in rendered
    assert rt.evidence == []


class _CircuitDesign:
    design_id = "design-1"
    nets = [
        SimpleNamespace(
            name="VCC3V3_EQ",
            connections=[SimpleNamespace(refdes="C1832", pin="1")],
        )
    ]
    instances = []


class _CircuitStoreService:
    """Circuit service whose canonical store backs the persistent index."""

    def __init__(self):
        self.store = self
        self.query_calls = 0

    def list_designs(self, kb_name):
        return [_CircuitDesign()]

    def query(self, **_kwargs):
        self.query_calls += 1
        return []


def _pointer(id, content, source, kind, processor_kind="ragflow"):
    return Evidence(
        id=id,
        content=content,
        source_name=source,
        content_kind=kind,
        processor_kind=processor_kind,
        score=0.5,
        locator={},
        metadata={},
    )


def test_pointer_index_merges_fts_and_like_hits(tmp_path):
    import sqlite3 as _sqlite3

    from src.agents.tools.pointer_index import PointerIndex

    index = PointerIndex(str(tmp_path / "index.db"))
    index.build([
        _pointer("doc:1", "供电VCC3V3_EQ电源监控", "a.docx", "document_text"),
        _pointer("circuit:1", "网表VCC3V3_EQ网络", "b.EDF", "circuit_net", "circuit"),
        _pointer("doc:2", "VCC3V3_EQ 分词正常", "c.docx", "document_text"),
    ])

    with _sqlite3.connect(index.path) as conn:
        fts_ids = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM pointer_fts WHERE pointer_fts MATCH ?",
                ('"VCC3V3_EQ"',),
            )
        }

    assert "doc:1" not in fts_ids
    assert "circuit:1" not in fts_ids
    found = index.search(["VCC3V3_EQ"], regex=False, limit=10)
    assert {item.id for item in found} == {"doc:1", "circuit:1", "doc:2"}
    assert index.search(["%"], regex=False, limit=10) == []


def test_kb_grep_serves_edf_pointer_from_persistent_index():
    circuit_record = SimpleNamespace(
        id=9,
        kb_id=4,
        kb_name="AAA",
        department_id="1739",
        document_id="circuit:9",
        original_file_name="825504380_ADAS_SCH_TCN2.EDF",
        document_name="原理图网表.EDF",
        content_kind="circuit_net",
        processor_kind="circuit_design",
        status="indexed",
        content_hash="hash-9",
    )
    circuit = _CircuitStoreService()
    rt = _runtime()
    grep = make_kb_grep(
        rt, _Backend([]), _Store([_record(), circuit_record]), circuit_service=circuit
    )

    first = grep(["VCC3V3_EQ"])
    second = grep(["VCC3V3_EQ"])

    assert "825504380_ADAS_SCH_TCN2.EDF" in first
    assert "825504380_ADAS_SCH_TCN2.EDF" in second
    assert circuit.query_calls == 0
    assert rt.evidence == []


def test_pointer_index_rebuilds_only_when_sources_change(tmp_path):
    from src.agents.tools.pointer_index import PointerIndex, records_signature

    records = [_record()]
    signature = records_signature(records)
    index = PointerIndex(str(tmp_path / "index.db"))
    index.build([_pointer("doc:1", "TC377", "a.docx", "document_text")], signature)

    assert index.needs_rebuild(signature) is False

    changed = [SimpleNamespace(**{**vars(records[0]), "content_hash": "hash-new"})]
    assert index.needs_rebuild(records_signature(changed)) is True


def test_kb_grep_surfaces_edf_pointer_ahead_of_docx_flood():
    chunks = [
        ParsedChunk(
            index=index,
            content="TC377 TC377 TC377 最小系统说明。",
            metadata={"ragflow_chunk_id": f"chunk-{index}"},
        )
        for index in range(15)
    ]
    circuit_record = SimpleNamespace(
        id=9,
        kb_id=4,
        kb_name="AAA",
        department_id="1739",
        document_id="circuit:9",
        original_file_name="825504380_ADAS_SCH_TCN2.EDF",
        document_name="原理图网表.EDF",
        content_kind="circuit_net",
        processor_kind="circuit_design",
        status="indexed",
        content_hash="hash-9",
    )
    rt = _runtime()
    grep = make_kb_grep(
        rt,
        _Backend(chunks),
        _Store([_record(), circuit_record]),
        circuit_service=_CircuitStoreService(),
    )

    rendered = grep(["TC377", "VCC3V3_EQ"], max_hits=30)

    rows = [line for line in rendered.splitlines() if line.startswith("- id=")]
    assert any("825504380_ADAS_SCH_TCN2.EDF" in line for line in rows[:3])
