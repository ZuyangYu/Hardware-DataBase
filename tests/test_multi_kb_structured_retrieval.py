"""Structured evidence must stay inside the selected KB and frozen records."""

from dataclasses import replace
from hashlib import sha256

import pytest

import src.settings as settings

from src.agents.tools import retrieval_executor as pools
from src.external_conversations.models import ConversationTurn, ExternalConversation
from src.external_conversations.query_engine import ExternalConversationQueryEngine
from src.pipelines.document_rag.schemas import DocumentRetrievalContract, DocumentSourceStamp
from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.spreadsheet.table_store import TableIndexStore
from src.services.spreadsheet_index_service import SpreadsheetIndexService
from tests import test_knowledge_scope as scope_fixtures

auth_scope = scope_fixtures.auth_scope
resolve_scope = scope_fixtures.resolve_scope


@pytest.fixture
def indexes(tmp_path, auth_scope):
    resolver, snapshot = resolve_scope(auth_scope)
    documents = PipelineDocumentStore(db_path=str(tmp_path / "documents.db"))
    spreadsheets = SpreadsheetIndexService(storage_root=str(tmp_path / "indexes"))

    def add(kb, value, name="same.xlsx"):
        source = next(source for source in snapshot.sources if source.kb_name == kb)
        path = tmp_path / f"{kb}-{name}"
        path.write_text(f"Component,Voltage\nU1,{value}\n")
        digest = sha256(path.read_bytes()).hexdigest()
        documents.upsert_document(
            kb_name=kb, document_name=name, original_file_name=name,
            dataset_kind="design", dataset_id="local", document_id=name,
            source_group="design", department_id=source.department_id,
            uploaded_by="reader", kb_id=source.kb_id, status="indexed",
            content_kind="spreadsheet_table", processor_kind="spreadsheet_table",
            content_hash=digest,
        )
        record = next(record for record in documents.list_documents(kb, source.department_id) if record.document_name == name)
        index = TableIndexStore(spreadsheets.db_path(source.department_id, kb))
        with index._connect() as connection:
            connection.execute("INSERT INTO table_documents (record_id, kb_id, kb_name, department_id, document_name, content_hash, local_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (record.id, source.kb_id, kb, source.department_id, name, digest, str(path)))
            connection.execute("INSERT INTO table_semantic_rows (record_id, sheet_name, row_index, semantic_text, raw_text) VALUES (?, 'Power', 2, ?, ?)", (record.id, f"Component: U1; Voltage: {value}", f"U1 {value}"))
            connection.execute("INSERT INTO table_cells (record_id, sheet_name, row_index, col_index, cell_ref, value, header) VALUES (?, 'Power', 2, 1, 'A2', 'U1', 'Component')", (record.id,))
        return record

    return resolver, snapshot, documents, spreadsheets, add


def frozen(record):
    return DocumentRetrievalContract(mode="frozen", allowed_record_ids=(record.id,), expected_source_stamps={
        record.id: DocumentSourceStamp(record.kb_id, record.id, record.content_hash, record.document_id, 0),
    })


def test_same_filename_and_component_in_two_kbs_keep_actual_record_provenance(indexes, monkeypatch):
    from src.services.structured_retrieval import build_structured_retrieval_adapters
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget

    resolver, snapshot, documents, spreadsheets, add = indexes
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True)
    a, b = add("A", "5V"), add("B", "12V")
    adapters = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)
    pools.reset_retrieval_executor(deadline_seconds=1)
    pools.initialize_retrieval_executor()
    try:
        outcome = MultiKnowledgeRetrievalService(resolver, object(), adapters=adapters).search(
            snapshot=snapshot, queries=["U1"], retriever_kind="spreadsheet_semantic",
            per_source_top_k=1, budget=RetrievalBudget(max_calls=2, timeout_seconds=3),
        )
    finally:
        pools.reset_retrieval_executor(deadline_seconds=1)
    assert {item.metadata["local_record_id"] for item in outcome.evidence} == {a.id, b.id}
    content = {item.metadata["kb_id"]: item.content for item in outcome.evidence}
    assert "5V" in content[a.kb_id]
    assert "12V" in content[b.kb_id]
    assert all(item.metadata["chunk_or_structured_locator"]["sheet_name"] == "Power" for item in outcome.evidence)
    from src.services.evidence_fusion import fuse_evidence
    fused = fuse_evidence(outcome, purpose="compare", max_items=4, token_budget=10000, min_score=0)
    assert len(fused.evidence) == 2
    assert fused.discarded["identity"] == 0


def test_frozen_spreadsheet_filters_before_ranking_and_never_reads_other_record(indexes):
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, spreadsheets, add = indexes
    allowed = add("A", "5V")
    add("A", "99V", "unselected.xlsx")
    adapter = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)["spreadsheet_semantic"]
    outcome = adapter.retrieve_with_scope("A", "U1", ctx=resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0]), contract=frozen(allowed), top_k=1)
    assert outcome.status == "success_with_hits"
    assert len(outcome.evidence) == 1
    assert "5V" in outcome.evidence[0].content
    assert "99V" not in outcome.evidence[0].content
    assert outcome.evidence[0].metadata["source_content_hash"] == allowed.content_hash


def test_changed_frozen_record_fails_before_any_index_read(indexes):
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, spreadsheets, add = indexes
    record = add("A", "5V")
    contract = frozen(replace(record, content_hash="old-hash"))
    adapter = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)["spreadsheet_cell"]
    outcome = adapter.retrieve_with_scope("A", "U1", ctx=resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0]), contract=contract, top_k=2)
    assert outcome.status == "source_changed"
    assert outcome.evidence == []


def test_structured_index_hash_must_match_pipeline_even_with_current_contract(indexes):
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, spreadsheets, add = indexes
    record = add("A", "5V")
    from src.pipelines.spreadsheet.table_store import TableIndexStore
    index = TableIndexStore(spreadsheets.db_path(record.department_id, "A"))
    with index._connect() as connection:
        connection.execute("UPDATE table_documents SET content_hash = 'stale' WHERE record_id = ?", (record.id,))
    adapter = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)["spreadsheet_semantic"]
    outcome = adapter.retrieve_with_scope("A", "U1", ctx=resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0]), contract=frozen(record), top_k=2)
    assert outcome.status == "source_changed"
    assert outcome.evidence == []


def test_wiki_without_frozen_record_provenance_refuses_to_search(indexes):
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, _, add = indexes
    record = add("A", "5V")

    class Wiki:
        def search_pages(self, **kwargs):
            pytest.fail("Wiki cannot widen a frozen document selection")

    adapter = build_structured_retrieval_adapters(document_store=documents, wiki_service=Wiki())["wiki"]
    outcome = adapter.retrieve_with_scope("A", "power", ctx=resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0]), contract=frozen(record), top_k=2)
    assert outcome.status == "filter_unsupported"
    assert outcome.evidence == []


def test_chat_wiki_without_original_source_stamps_reports_unsupported_before_fusion(indexes, auth_scope, monkeypatch):
    import json

    from src.core.wiki import WikiService
    from src.services.evidence_fusion import fuse_evidence
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, _, add = indexes
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True)
    record = add("A", "5V")
    wiki = WikiService(db_path=auth_scope[0].db_path)
    with wiki._connect() as connection:
        connection.execute(
            """INSERT INTO wiki_pages
               (department_id, kb_id, kb_name, slug, title, content, source_refs_json,
                chunk_refs_json, version, created_at, updated_at)
               VALUES (?, ?, 'A', 'entity/power', 'U1 power', 'U1 supply 5V', ?, ?, 7, 'now', 'now')""",
            (record.department_id, record.kb_id, json.dumps([f"{record.document_id}|{record.document_name}"]),
             json.dumps([f"{record.document_id}#chunk-1"])),
        )
    assert wiki.search_pages(kb_id=record.kb_id, department_id=int(record.department_id), query="U1")
    adapters = build_structured_retrieval_adapters(document_store=documents, wiki_service=wiki)
    pools.reset_retrieval_executor(deadline_seconds=1)
    pools.initialize_retrieval_executor()
    try:
        outcome = MultiKnowledgeRetrievalService(resolver, object(), adapters=adapters).search(
            snapshot=snapshot, queries=["U1"], retriever_kind="wiki",
            budget=RetrievalBudget(max_calls=2, timeout_seconds=3),
        )
    finally:
        pools.reset_retrieval_executor(deadline_seconds=1)

    assert {item.status for item in outcome.source_outcomes} == {"filter_unsupported"}
    assert {item.error_code for item in outcome.source_outcomes} == {"wiki_source_provenance_unsupported"}
    assert outcome.partial is True
    assert outcome.evidence == []
    fused = fuse_evidence(outcome, purpose="answer", max_items=4, token_budget=10000, min_score=0)
    assert fused.evidence == []
    assert fused.discarded["identity"] == 0


def test_external_conversation_whitelist_applies_before_top_k(tmp_path):
    engine = ExternalConversationQueryEngine(root=str(tmp_path))
    for number in range(6):
        engine.index_conversation(ExternalConversation(
            conversation_id=f"c{number}", kb_name="A", department_id="7",
            source_file="same.md", content_hash=f"hash{number}",
            turns=[ConversationTurn(role="assistant", content=f"U1 power {number}V")],
        ))
    rows = engine.search_by_scope("7", "A", "U1", top_k=1, conversation_ids=("c5",))
    assert [row["conversation_id"] for row in rows] == ["c5"]
    assert rows[0]["source_content_hash"] == "hash5"
    assert engine.search_by_scope("7", "A", "U1", conversation_ids=()) == []


def test_circuit_index_persists_full_source_hash_for_frozen_validation(tmp_path):
    from tests.test_circuit_index_service import _service

    path = tmp_path / "board.edf"
    path.write_text("(edif source_generation_one)")
    service = _service(str(tmp_path))
    result = service.index_file(kb_name="A", record_id=7, file_path=str(path), original_name="board.edf", department_id="7")
    assert result.ok
    assert service._read_metadata("A", result.design_id).get("source_content_hash") == sha256(path.read_bytes()).hexdigest()


def test_record_changed_during_query_discards_all_structured_evidence(indexes):
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, spreadsheets, add = indexes
    record = add("A", "5V")
    adapter = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)["spreadsheet_semantic"]
    original_query = adapter.query

    def change_after_read(*args):
        result = original_query(*args)
        with documents._connect() as connection:
            connection.execute("UPDATE pipeline_documents SET content_hash = 'new' WHERE id = ?", (record.id,))
        return result

    adapter.query = change_after_read
    result = adapter.retrieve_with_scope("A", "U1", ctx=resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0]), contract=frozen(record), top_k=2)
    assert result.status == "source_changed"
    assert result.evidence == []


def test_frozen_external_conversation_returns_only_selected_message_and_source_hash(indexes, tmp_path):
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, _, _ = indexes
    source = snapshot.sources[1]
    conversations = ExternalConversationQueryEngine(root=str(tmp_path / "conversations"))
    for index, value in enumerate(("5V", "12V")):
        conversation = ExternalConversation(conversation_id=f"c{index}", kb_name="B", department_id=source.department_id,
            source_file=f"power{index}.md", kb_id=source.kb_id, content_hash=sha256(value.encode()).hexdigest(),
            turns=[ConversationTurn(role="assistant", content=f"U1 supply {value}")])
        conversations.index_conversation(conversation)
        documents.upsert_document(kb_name="B", document_name=conversation.source_file,
            dataset_kind="conversation", dataset_id="", document_id=conversation.conversation_id,
            department_id=source.department_id, uploaded_by="owner", source_group="external",
            kb_id=source.kb_id, status="indexed", processor_kind="external_conversation", content_hash=conversation.content_hash)
    record = documents.list_documents("B", source.department_id)[1]
    adapter = build_structured_retrieval_adapters(document_store=documents, conversation_service=conversations)["conversation"]
    result = adapter.retrieve_with_scope("B", "U1", ctx=resolver.context_for_source(snapshot.actor_user_id, source), contract=frozen(record), top_k=5)
    assert result.status == "success_with_hits"
    assert len(result.evidence) == 1
    assert result.evidence[0].metadata["chunk_or_structured_locator"]["conversation_id"] == record.document_id
    assert result.evidence[0].metadata["source_content_hash"] == record.content_hash
    assert result.evidence[0].content == "U1 supply 12V"


def test_frozen_circuit_returns_only_selected_design_with_same_component_names(indexes, tmp_path):
    from src.services.structured_retrieval import build_structured_retrieval_adapters
    from tests.test_circuit_index_service import _service

    resolver, snapshot, documents, _, _ = indexes
    source = snapshot.sources[0]
    circuits = _service(str(tmp_path))
    selected = None
    for index in (7, 8):
        path = tmp_path / f"board{index}.edf"
        path.write_text(f"(edif board{index})")
        documents.upsert_document(kb_name="A", document_name=path.name, dataset_kind="circuit", dataset_id="",
            document_id=path.stem, department_id=source.department_id, uploaded_by="reader", source_group="design",
            kb_id=source.kb_id, status="indexed", processor_kind="circuit_design", content_hash=sha256(path.read_bytes()).hexdigest())
        record = next(item for item in documents.list_documents("A", source.department_id) if item.document_name == path.name)
        circuits.index_file(kb_name="A", record_id=record.id, file_path=str(path), original_name=path.name, department_id=source.department_id)
        selected = record
    adapter = build_structured_retrieval_adapters(document_store=documents, circuit_service=circuits)["circuit_resolved_connections"]
    result = adapter.retrieve_with_scope("A", "U1200 CAN0", ctx=resolver.context_for_source(snapshot.actor_user_id, source), contract=frozen(selected), top_k=5)
    assert result.status == "success_with_hits"
    assert {item.metadata["local_record_id"] for item in result.evidence} == {selected.id}
    assert {item.source_name for item in result.evidence} == {"board8.edf"}


def test_changed_structured_source_version_gets_distinct_evidence_identity(indexes):
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    resolver, snapshot, documents, spreadsheets, add = indexes
    record = add("A", "5V")
    adapter = build_structured_retrieval_adapters(document_store=documents, spreadsheet_service=spreadsheets)["spreadsheet_semantic"]
    ctx = resolver.context_for_source(snapshot.actor_user_id, snapshot.sources[0])
    old = adapter.retrieve_with_scope("A", "U1", ctx=ctx, contract=DocumentRetrievalContract(), top_k=2)
    replacement_hash = sha256(b"updated source").hexdigest()
    with documents._connect() as connection:
        connection.execute("UPDATE pipeline_documents SET content_hash = ? WHERE id = ?", (replacement_hash, record.id))
    with TableIndexStore(spreadsheets.db_path(record.department_id, "A"))._connect() as connection:
        connection.execute("UPDATE table_documents SET content_hash = ? WHERE record_id = ?", (replacement_hash, record.id))
        connection.execute("UPDATE table_semantic_rows SET semantic_text = 'Component: U1; Voltage: 12V' WHERE record_id = ?", (record.id,))
    new = adapter.retrieve_with_scope("A", "U1", ctx=ctx, contract=DocumentRetrievalContract(), top_k=2)
    assert old.status == new.status == "success_with_hits"
    assert old.evidence[0].id != new.evidence[0].id
    assert "5V" in old.evidence[0].content
    assert "12V" in new.evidence[0].content
