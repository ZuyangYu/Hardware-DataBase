"""Legacy single-KB spreadsheet rows must carry readback-capable identity."""

from __future__ import annotations

import sqlite3

from src.agents.tools.runtime import ToolRuntime
from src.agents.tools.spreadsheet_tools import make_spreadsheet_tools
from src.services.citation_readback import read_citation_content
from src.services.evidence_fusion import evidence_identity
from tests import test_multi_kb_structured_retrieval as structured_fixtures

indexes = structured_fixtures.indexes
auth_scope = structured_fixtures.auth_scope


def _runtime(resolver, snapshot, documents, kb_name="A"):
    source = next(source for source in snapshot.sources if source.kb_name == kb_name)
    ctx = resolver.context_for_source(snapshot.actor_user_id, source)
    return ToolRuntime(kb_name=kb_name, ctx=ctx, document_store=documents, evidence_budget=10,
                       question="U1 voltage"), source


def test_legacy_spreadsheet_row_has_complete_identity_and_citation_readback(indexes):
    resolver, snapshot, documents, spreadsheets, add = indexes
    record = add("A", "5V")
    rt, source = _runtime(resolver, snapshot, documents)
    tools = {tool.__name__: tool for tool in make_spreadsheet_tools(rt, spreadsheets)}

    tools["spreadsheet_row_search"]("U1 voltage", top_k=5)

    assert len(rt.evidence) == 1
    item = rt.evidence[0]
    metadata = item.metadata
    assert metadata["local_record_id"] == record.id
    assert metadata["kb_id"] == record.kb_id
    assert metadata["source_content_hash"] == record.content_hash
    assert metadata["index_revision"] == int(getattr(record, "index_revision", 0) or 0)
    assert metadata["evidence_text_hash"]
    assert metadata["authoritative_evidence_identity"] == evidence_identity(metadata)
    assert read_citation_content(
        __import__("types").SimpleNamespace(spreadsheet_service=spreadsheets),
        record, rt.ctx, {"sheet_name": "Power", "row_index": 2},
    ) == "Component: U1; Voltage: 5V"


def test_stale_index_hash_row_is_not_returned_as_pseudo_evidence(indexes):
    resolver, snapshot, documents, spreadsheets, add = indexes
    record = add("A", "5V")
    source = snapshot.sources[0]
    path = spreadsheets.db_path(source.department_id, "A")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE table_documents SET content_hash = 'stale' WHERE record_id = ?", (record.id,))
    rt, _ = _runtime(resolver, snapshot, documents)
    tools = {tool.__name__: tool for tool in make_spreadsheet_tools(rt, spreadsheets)}

    tools["spreadsheet_row_search"]("U1 voltage", top_k=5)

    assert rt.evidence == []
