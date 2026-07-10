import unittest
from pathlib import Path

from src.agents.state import Evidence
from src.agents.tools.circuit_tools import CircuitQueryTool
from src.pipelines.document_rag.schemas import RequestContext


class _CircuitIndex:
    def __init__(self, hits=None):
        self.calls = []
        self.hits = hits if hits is not None else []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


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

    def test_circuit_query_tool_returns_empty_when_no_indexed_data_exists(self):
        tool = CircuitQueryTool(index_service=_CircuitIndex())
        hits = tool.run("U1200 CAN connection", "kb_hw", RequestContext(user_id="alice"), top_k=3)
        self.assertEqual(hits, [])

    def test_runner_registers_circuit_query_tool(self):
        runner_source = Path("src/agents/runner.py").read_text(encoding="utf-8")
        self.assertIn("CircuitQueryTool", runner_source)
        self.assertIn('"circuit_query": CircuitQueryTool()', runner_source)


if __name__ == "__main__":
    unittest.main()
