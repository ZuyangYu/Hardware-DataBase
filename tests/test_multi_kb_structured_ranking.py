"""Query relevance must outrank parser confidence for structured rows."""

from __future__ import annotations

import pytest

from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool
from src.pipelines.spreadsheet.table_store import TableIndexStore
from tests import test_multi_kb_structured_retrieval as structured_fixtures

indexes = structured_fixtures.indexes
auth_scope = structured_fixtures.auth_scope


@pytest.fixture
def ranked_index(indexes):
    resolver, snapshot, documents, spreadsheets, add = indexes
    record = add("A", "5V")
    source = snapshot.sources[0]
    index = TableIndexStore(spreadsheets.db_path(source.department_id, "A"))
    with index._connect() as connection:
        for position in range(12):
            connection.execute(
                "INSERT INTO table_semantic_rows "
                "(record_id, sheet_name, row_index, semantic_text, raw_text, confidence, confidence_score) "
                "VALUES (?, 'Power', ?, ?, ?, 'high', 1.0)",
                (record.id, 100 + position, f"Unrelated capacitor note {position}", f"note {position}"),
            )
        connection.execute(
            "INSERT INTO table_semantic_rows "
            "(record_id, sheet_name, row_index, semantic_text, raw_text, confidence, confidence_score) "
            "VALUES (?, 'Power', 14, ?, ?, 'medium', 0.3)",
            (record.id, "Power supply voltage: 18-32V", "supply 18-32V"),
        )
    ctx = resolver.context_for_source(snapshot.actor_user_id, source)
    return record, spreadsheets, ctx


def test_target_row_outranks_many_high_confidence_unrelated_rows(ranked_index):
    record, spreadsheets, ctx = ranked_index
    tool = SpreadsheetSemanticTool(spreadsheets)
    items = tool.run("供电电压 18-32V", "A", ctx, top_k=2, filters={"record_id": record.id})

    assert any("18-32V" in item.content for item in items)
    target = next(item for item in items if "18-32V" in item.content)
    unrelated = [item for item in items if "18-32V" not in item.content]
    assert target.score > max((item.score for item in unrelated), default=0)
    assert target.metadata["parser_confidence"] == 0.3
    assert target.metadata["query_relevance"] == target.score
    assert target.metadata["retriever_rank"] == 1
