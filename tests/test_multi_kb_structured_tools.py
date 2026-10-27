"""Agent structured tools route through the shared authorized scope."""

from types import SimpleNamespace

from src.agents.tools.runtime import ToolRuntime
from src.pipelines.document_rag.schemas import Evidence
from src.services.knowledge_scope_models import (
    KnowledgeSourceRetrievalOutcome, MultiKnowledgeRetrievalOutcome,
)
from tests.test_multi_kb_evidence_fusion import evidence as fusion_evidence
from tests.test_multi_kb_store_contract import snapshot
from tests import test_knowledge_scope as scope_fixtures

auth_scope = scope_fixtures.auth_scope


def test_structured_tools_keep_sheet_locator_and_namespaced_citation_identity():
    from src.agents.tools.structured_scope_tools import make_scoped_structured_tools

    calls = []

    class Service:
        def search(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(source_outcomes=[], evidence=[Evidence(
                id="xlsx:7:Power:2:semantic", content="U1 12V", source_name="same.xlsx",
                metadata={"kb_id": 205, "local_record_id": 7, "processor_kind": "spreadsheet_table",
                          "content_kind": "spreadsheet_table", "chunk_or_structured_locator": {
                              "record_id": 7, "sheet_name": "Power", "row_index": 2,
                          }},
            )])

    budget = object()
    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=snapshot(1),
                     multi_kb_retrieval=Service(), retrieval_budget=budget)
    tools = {tool.__name__: tool for tool in make_scoped_structured_tools(rt)}
    tools["spreadsheet_row_search"]("U1", source_ref="kb:205")
    assert calls[0]["source_ref"] == "kb:205"
    assert calls[0]["budget"] is budget
    assert calls[0]["snapshot"] is rt.knowledge_scope
    assert calls[0]["retriever_kind"] == "spreadsheet_semantic"
    assert rt.evidence[0].id == "structured:205:xlsx:7:Power:2:semantic"
    assert rt.evidence[0].locator == {"record_id": 7, "sheet_name": "Power", "row_index": 2}


def test_unsupported_frozen_source_is_reported_instead_of_claimed_zero_hits():
    from src.agents.tools.structured_scope_tools import make_scoped_structured_tools

    scope = snapshot(1)

    class Service:
        def search(self, **kwargs):
            return MultiKnowledgeRetrievalOutcome([], [KnowledgeSourceRetrievalOutcome(
                kb_id=205, query=kwargs["queries"][0], retriever_kind=kwargs["retriever_kind"],
                status="filter_unsupported", evidence=[], error_code="frozen_unsupported",
            )], scope.selection_hash)

    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=scope, multi_kb_retrieval=Service(), retrieval_budget=object())
    tools = {tool.__name__: tool for tool in make_scoped_structured_tools(rt, wiki_assist=True)}
    rendered = tools["wiki_search"]("U1")
    assert "filter_unsupported" in rendered
    assert "frozen_unsupported" in rendered
    assert "filter_unsupported" in tools["spreadsheet_sql_query"]("SELECT * FROM data")
    assert rt.evidence == []
    assert rt.diagnostics[0]["error"] == ""
    assert rt.multi_kb_source_outcomes == [{
        "kb_id": 205, "query": "U1", "retriever_kind": "wiki",
        "status": "filter_unsupported", "error_code": "frozen_unsupported", "diagnostics": {},
    }]

    from src.agents.runner import MultiSourceAgentRunner

    summary = MultiSourceAgentRunner(rag_backend=object())._build_retrieval_summary(rt, [], {})
    assert summary["status"] == "failed"
    assert summary["error_message"] == "frozen_unsupported"


def test_structured_multi_kb_hits_pass_real_fusion_and_keep_source_audit():
    from src.agents.runner import MultiSourceAgentRunner
    from src.agents.tools.structured_scope_tools import make_scoped_structured_tools

    scope = snapshot(1)
    items = []
    for source in scope.sources:
        item = fusion_evidence(source.kb_id, chunk="xlsx:7:Power:2:semantic", content="U1 12V",
                               record_id=7, role=source.role)
        item.source_name = "same.xlsx"
        item.metadata.update({
            "department_id": source.department_id,
            "knowledge_source": source.model_dump(mode="json"),
            "processor_kind": "spreadsheet_table", "content_kind": "spreadsheet_table",
            "remote_document_id": "",
            "chunk_or_structured_locator": {"record_id": 7, "sheet_name": "Power", "row_index": 2},
        })
        items.append(item)

    class Service:
        def search(self, **kwargs):
            return MultiKnowledgeRetrievalOutcome(items, [KnowledgeSourceRetrievalOutcome(
                kb_id=item.metadata["kb_id"], query=kwargs["queries"][0],
                retriever_kind=kwargs["retriever_kind"], status="success_with_hits", evidence=[item],
            ) for item in items], scope.selection_hash)

    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=scope,
                     multi_kb_retrieval=Service(), retrieval_budget=object())
    tools = {tool.__name__: tool for tool in make_scoped_structured_tools(rt)}
    rendered = tools["spreadsheet_row_search"]("U1")

    assert "KB101" in rendered and "KB205" in rendered
    assert "sheet_name=Power" in rendered and "row_index=2" in rendered
    assert {item.metadata["kb_id"] for item in rt.evidence} == {101, 205}
    assert len({item.id for item in rt.evidence}) == 2
    assert {item.metadata["fusion_rank"] for item in rt.evidence} == {1, 2}
    for item in rt.evidence:
        assert item.id.startswith(f"structured:{item.metadata['kb_id']}:knowledge:")
        assert item.locator == {"record_id": 7, "sheet_name": "Power", "row_index": 2}
    assert rt.diagnostics[0]["error"] == ""
    assert {item["kb_id"] for item in rt.multi_kb_source_outcomes} == {101, 205}
    audit = rt.multi_kb_fusions[0]
    assert audit["selection_hash"] == scope.selection_hash
    assert audit["coverage"] == {101: True, 205: True}
    assert audit["complete"] is True
    assert {item["kb_id"] for item in audit["candidate_refs"]} == {101, 205}
    assert {item["retriever_kind"] for item in audit["query_coverage"]} == {"spreadsheet_semantic"}
    summary = MultiSourceAgentRunner(rag_backend=object())._build_retrieval_summary(rt, [], {})
    assert summary["status"] == "success"
    assert summary["multi_kb_fusions"][0]["coverage"] == {101: True, 205: True}


def test_typed_circuit_queries_cannot_drop_record_filter(tmp_path):
    from tests.test_circuit_index_service import _service
    from src.pipelines.document_rag.schemas import RequestContext

    service = _service(str(tmp_path))
    for number in (7, 8):
        path = tmp_path / f"board{number}.edf"
        path.write_text(f"(edif board{number})")
        service.index_file(kb_name="A", record_id=number, file_path=str(path), original_name=path.name, department_id="7")
    items = service.typed_query(kb_name="A", operation="structure_overview", query="overview",
        ctx=RequestContext(user_id="reader", metadata={"department_id": "7"}), top_k=8, filters={"record_id": 8})
    assert items
    assert {item.locator.get("record_id") for item in items} == {8}


def test_authorized_runner_mounts_controlled_structured_tools(auth_scope, tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    from src.agents import runner as runner_mod
    from src.pipelines.document_store import PipelineDocumentStore
    from tests.test_knowledge_scope import resolve_scope

    captured = {}

    class Agent:
        def stream(self, *_args, **_kwargs):
            yield AIMessage(content="ok"), {"langgraph_node": "model"}

    def create_agent(**kwargs):
        captured.update(kwargs)
        return Agent()

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", create_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **_: None)
    resolver, scope = resolve_scope(auth_scope)
    runner = runner_mod.MultiSourceAgentRunner(rag_backend=object(),
        document_store=PipelineDocumentStore(db_path=str(tmp_path / "runner-documents.db")))
    list(runner.stream(query="question", kb_name="A", history=[], knowledge_scope=scope,
                      knowledge_scope_resolver=resolver, thread_id="structured-multi-kb"))
    names = {str(getattr(tool, "name", None) or getattr(tool, "__name__", "")) for tool in captured["tools"]}
    assert {"circuit_search", "spreadsheet_row_search", "spreadsheet_cell_lookup", "conversation_search"} <= names
    assert "memory_search" not in names
