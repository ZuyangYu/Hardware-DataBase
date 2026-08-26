import unittest

from src.agents.runner import MultiSourceAgentRunner
from src.agents.state import Evidence
from src.agents.tools.circuit_tools import CircuitQueryTool
from src.pipelines.document_rag.schemas import BackendHealth, BackendResult, IngestResult, RequestContext


class _CircuitIndex:
    def __init__(self, hits=None):
        self.calls = []
        self.hits = hits if hits is not None else []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


class _FakeRAGBackend:
    name = "fake_ragflow"

    def list_knowledge_bases(self):
        return ["kb_hw"]

    def upload_files(self, kb_name, files, ctx=None, source_group=None, progress_callback=None):
        return IngestResult(success_count=len(files), total_count=len(files), backend=self.name)

    def retrieve(self, kb_name, query, top_k=None, ctx=None, filters=None):
        return []

    def delete_document(self, kb_name, document_id, ctx=None):
        return BackendResult(ok=True, message="deleted", backend=self.name)

    def list_documents(self, kb_name, ctx=None):
        return []

    def health_check(self):
        return BackendHealth(ok=True, backend=self.name)


class _FakeDocumentStore:
    def list_documents(self, kb_name, department_id=None):
        return []


class _FakeSpreadsheetService:
    def get_document_profile(self, record):
        return {}


class CircuitAgentToolTests(unittest.TestCase):
    def test_circuit_query_tool_returns_indexed_evidence(self):
        evidence = Evidence(
            id="circuit:7:net:CAN0",
            content="CAN0 connects U1200.1 and J3.2",
            source_name="main_board.edf",
            content_kind="circuit_design",
            processor_kind="circuit_design",
            score=0.91,
            locator={"record_id": 7, "entity_type": "net", "entity_id": "CAN0"},
            metadata={"kb_name": "kb_hw", "department_id": "dept_hw"},
        )
        index = _CircuitIndex([evidence])
        ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})
        tool = CircuitQueryTool(index_service=index)

        hits = tool.run(
            "U1200 CAN connection",
            "kb_hw",
            ctx,
            top_k=3,
            filters={"source_name": "main_board.edf"},
        )

        self.assertEqual(hits, [evidence])
        self.assertEqual(index.calls[0]["query"], "U1200 CAN connection")
        self.assertEqual(index.calls[0]["kb_name"], "kb_hw")
        self.assertEqual(index.calls[0]["ctx"], ctx)
        self.assertEqual(index.calls[0]["top_k"], 3)
        self.assertEqual(index.calls[0]["filters"], {"source_name": "main_board.edf"})

    def test_circuit_query_tool_fails_closed_without_department_context(self):
        import pytest as _pytest

        from src.pipelines.document_rag.schemas import RequestContext as _RequestContext

        tool = CircuitQueryTool(index_service=_CircuitIndex())
        with _pytest.raises(PermissionError):
            tool.run("U1200 CAN connection", "kb_hw", _RequestContext(user_id="alice"), top_k=3)

    def test_circuit_query_tool_returns_empty_for_authorized_empty_index(self):
        ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})
        tool = CircuitQueryTool(index_service=_CircuitIndex())
        hits = tool.run("U1200 CAN connection", "kb_hw", ctx, top_k=3)
        self.assertEqual(hits, [])

    def test_runner_uses_injected_circuit_service(self):
        circuit_index = _CircuitIndex()

        runner = MultiSourceAgentRunner(
            rag_backend=_FakeRAGBackend(),
            document_store=_FakeDocumentStore(),
            spreadsheet_service=_FakeSpreadsheetService(),
            circuit_service=circuit_index,
        )

        self.assertIs(runner.tools["circuit_query"].index_service, circuit_index)


if __name__ == "__main__":
    unittest.main()
