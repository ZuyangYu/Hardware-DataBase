"""One KB batch: one candidate query and one validation pair for many records."""

from __future__ import annotations

import sqlite3

import pytest

from src.pipelines.document_rag.schemas import DocumentRetrievalContract, DocumentSourceStamp
from src.services.structured_retrieval import build_structured_retrieval_adapters
from src.services.retrieval_targets import TargetLocator
from tests import test_multi_kb_structured_retrieval as structured_fixtures

indexes = structured_fixtures.indexes
auth_scope = structured_fixtures.auth_scope


@pytest.fixture
def batch_index(indexes, monkeypatch):
    resolver, snapshot, documents, spreadsheets, add = indexes
    first = add("A", "5V", "first.xlsx")
    second = add("A", "12V", "second.xlsx")
    source = snapshot.sources[0]
    index = speadsheet_path = spreadsheets.db_path(source.department_id, "A")
    with sqlite3.connect(index) as connection:
        connection.execute(
            "INSERT INTO table_semantic_rows "
            "(record_id, sheet_name, row_index, semantic_text, raw_text) VALUES (?, 'Pin Definition', 29, ?, ?)",
            (first.id, "Pin Number: 2; Voltage: 18-32V", "pin 2 18-32V"),
        )
        connection.execute(
            "INSERT INTO table_semantic_rows "
            "(record_id, sheet_name, row_index, semantic_text, raw_text) VALUES (?, 'Pin Definition', 29, ?, ?)",
            (second.id, "Pin Number: 14; Voltage: 9-16V", "pin 14 9-16V"),
        )
        connection.execute(
            "INSERT INTO table_cells "
            "(record_id, sheet_name, row_index, col_index, cell_ref, value, raw_value, header) "
            "VALUES (?, 'Pin Definition', 29, 1, 'B29', '2', '2', 'Pin Number')",
            (first.id,),
        )
        connection.execute(
            "INSERT INTO table_cells "
            "(record_id, sheet_name, row_index, col_index, cell_ref, value, raw_value, header) "
            "VALUES (?, 'Pin Definition', 29, 1, 'B29', '14', '14', 'Pin Number')",
            (second.id,),
        )
    return resolver, snapshot, documents, spreadsheets, first, second, speadsheet_path


def _contract(records):
    return DocumentRetrievalContract(
        mode="frozen", allowed_record_ids=tuple(record.id for record in records),
        expected_source_stamps={record.id: DocumentSourceStamp(
            record.kb_id, record.id, record.content_hash, record.document_id, 0,
        ) for record in records},
    )


def test_batch_cell_query_keeps_each_record_locator_and_counts_one_call(batch_index):
    resolver, snapshot, documents, spreadsheets, first, second, _ = batch_index
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    adapter = adapters["spreadsheet_cell"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])
    calls = []

    outcome = adapter.retrieve_batch_with_scope(
        "A", "管脚电压", ctx=ctx, contract=_contract([first, second]), top_k=5,
        before_request=lambda: calls.append(1),
        locators_by_record={
            first.id: (TargetLocator("t1", sheet_name="Pin Definition", pin_value="2"),),
            second.id: (TargetLocator("t2", sheet_name="Pin Definition", pin_value="14"),),
        },
    )

    assert outcome.status == "success_with_hits", outcome.diagnostics
    assert calls == [1]
    assert outcome.diagnostics["candidate_queries"] == 1
    assert outcome.diagnostics["validation_queries"] == 2
    by_record = {item.metadata["local_record_id"]: item for item in outcome.evidence}
    assert "2" in by_record[first.id].content
    assert "14" in by_record[second.id].content


def test_batch_semantic_resolves_pin_column_from_sheet_header(batch_index, monkeypatch):
    resolver, snapshot, documents, spreadsheets, first, second, path = batch_index
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE table_semantic_rows SET values_json = ?, semantic_text = ?, raw_text = ? "
            "WHERE record_id = ? AND row_index = 2",
            ('{"12": "2", "VCC2": "+BB", "VCC2_Reserved": "电源（+24V）", "C2": "A2"}',
             "12: 2; VCC2: +BB; VCC2_Reserved: 电源（+24V）; C2: A2", "2 | +BB | 电源（+24V） | A2", first.id),
        )
        connection.execute(
            "INSERT INTO table_semantic_rows (record_id, sheet_name, row_index, semantic_text, raw_text, values_json) "
            "VALUES (?, 'Power', 18, '12: 管脚号; VCC2: 管脚定义; VCC2_Reserved: 功能描述; C2: 备注', "
            "'管脚号 | 管脚定义 | 功能描述 | 备注', ?)",
            (first.id, '{"12": "管脚号", "VCC2": "管脚定义", "VCC2_Reserved": "功能描述", "C2": "备注"}'),
        )
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    adapter = adapters["spreadsheet_semantic"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])

    outcome = adapter.retrieve_with_scope(
        "A", "管脚分配 第2脚", ctx=ctx, contract=_contract([first]), top_k=5,
        locators_by_record={first.id: (TargetLocator("t1", sheet_name="Power", pin_value="2", pin_field="管脚"),)},
    )

    assert outcome.status == "success_with_hits", outcome.diagnostics
    assert "+BB" in outcome.evidence[0].content
    assert "24V" in outcome.evidence[0].content


def test_batch_semantic_pin_locators_return_full_rows_per_record(batch_index):
    resolver, snapshot, documents, spreadsheets, first, second, _ = batch_index
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    adapter = adapters["spreadsheet_semantic"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])

    outcome = adapter.retrieve_batch_with_scope(
        "A", "管脚定义", ctx=ctx, contract=_contract([first, second]), top_k=5,
        before_request=lambda: None,
        locators_by_record={
            first.id: (TargetLocator("t1", sheet_name="Pin Definition", pin_value="2"),),
            second.id: (TargetLocator("t2", sheet_name="Pin Definition", pin_value="14"),),
        },
    )

    assert outcome.status == "success_with_hits", outcome.diagnostics
    by_record = {item.metadata["local_record_id"]: item.content for item in outcome.evidence}
    assert "Pin Number: 2" in by_record[first.id]
    assert "Voltage: 18-32V" in by_record[first.id]
    assert "Pin Number: 14" in by_record[second.id]
    assert "Voltage: 9-16V" in by_record[second.id]


def test_batch_validation_failure_drops_the_whole_kb_batch(batch_index):
    resolver, snapshot, documents, spreadsheets, first, second, path = batch_index
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    adapter = adapters["spreadsheet_semantic"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM table_documents WHERE record_id = ?", (second.id,))

    outcome = adapter.retrieve_batch_with_scope(
        "A", "U1", ctx=ctx, contract=_contract([first, second]), top_k=5,
        before_request=lambda: None,
    )

    assert outcome.status == "source_unavailable"
    assert outcome.error_code == "structured_record_missing"
    assert outcome.evidence == []


def test_batch_semantic_query_runs_one_candidate_sql_and_one_validation_pair(batch_index, monkeypatch):
    resolver, snapshot, documents, spreadsheets, first, second, _ = batch_index
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    adapter = adapters["spreadsheet_semantic"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])
    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    outcome = adapter.retrieve_batch_with_scope(
        "A", "U1", ctx=ctx, contract=_contract([first, second]), top_k=5,
        before_request=lambda: None,
    )

    assert outcome.status == "success_with_hits", outcome.diagnostics
    candidate_sql = [sql for sql in statements if "FROM table_semantic_rows" in sql]
    document_sql = [sql for sql in statements if "FROM table_documents" in sql]
    assert len(candidate_sql) == 1
    assert len(document_sql) == 2


def test_per_record_and_per_source_candidate_limits_are_separate(batch_index, monkeypatch):
    import src.settings as settings

    resolver, snapshot, documents, spreadsheets, first, second, path = batch_index
    with sqlite3.connect(path) as connection:
        for record in (first, second):
            connection.execute(
                "INSERT INTO table_semantic_rows (record_id, sheet_name, row_index, semantic_text, raw_text) "
                "VALUES (?, 'Power', 3, 'Component: U1; Voltage: 3V', 'U1 3V')",
                (record.id,),
            )
    monkeypatch.setattr(settings, "MULTI_KB_PER_RECORD_CANDIDATES", 1)
    monkeypatch.setattr(settings, "MULTI_KB_PER_SOURCE_CANDIDATES", 10)
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    adapter = adapters["spreadsheet_semantic"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])

    outcome = adapter.retrieve_with_scope(
        "A", "U1", ctx=ctx, contract=_contract([first, second]), top_k=10,
        before_request=lambda: None,
    )

    assert outcome.status == "success_with_hits", outcome.diagnostics
    assert outcome.diagnostics["per_record_limit"] == 1
    assert outcome.diagnostics["per_source_limit"] == 10
    assert outcome.diagnostics["candidates"] == 2
    assert {item.metadata["local_record_id"] for item in outcome.evidence} == {first.id, second.id}


def test_nineteen_spreadsheet_records_use_one_candidate_query(indexes, monkeypatch):
    resolver, snapshot, documents, spreadsheets, add = indexes
    records = [add("A", f"{index}V", f"record-{index}.xlsx") for index in range(19)]
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    adapter = adapters["spreadsheet_semantic"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])
    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    outcome = adapter.retrieve_batch_with_scope(
        "A", "U1", ctx=ctx, contract=_contract(records), top_k=20,
        before_request=lambda: None,
    )

    assert outcome.status == "success_with_hits", outcome.diagnostics
    assert outcome.diagnostics["candidates"] == 19
    assert outcome.diagnostics["per_source_limit"] == 8
    assert len({item.metadata["local_record_id"] for item in outcome.evidence}) == 8
    assert len([sql for sql in statements if "FROM table_semantic_rows" in sql]) == 1
    assert len([sql for sql in statements if "FROM table_documents" in sql]) == 2
