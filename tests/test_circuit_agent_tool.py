import unittest

from src.agents.schemas import Evidence
from src.agents.tools.circuit_tools import make_circuit_search
from src.agents.tools.runtime import ToolRuntime
from src.pipelines.document_rag.schemas import RequestContext


class _CircuitIndex:
    def __init__(self, hits=None):
        self.calls = []
        self.hits = hits if hits is not None else []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


def _runtime(index, kb_name="kb_hw", ctx=None):
    return ToolRuntime(kb_name=kb_name, ctx=ctx, top_k=3)


class CircuitAgentToolTests(unittest.TestCase):
    def test_circuit_search_returns_indexed_evidence_with_citation_numbers(self):
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
        rt = _runtime(index, ctx=ctx)
        circuit_search = make_circuit_search(rt, index)

        result = circuit_search("U1200 CAN connection", top_k=3)

        self.assertIn("CAN0 connects U1200.1 and J3.2", result)
        self.assertIn("[1]", result)
        self.assertEqual(len(rt.evidence), 1)
        call = index.calls[0]
        self.assertEqual(call["query"], "U1200 CAN connection")
        self.assertEqual(call["kb_name"], "kb_hw")
        self.assertEqual(call["ctx"], ctx)
        self.assertEqual(call["top_k"], 3)

    def test_circuit_search_returns_empty_marker_when_no_indexed_data_exists(self):
        index = _CircuitIndex()
        rt = _runtime(index)
        circuit_search = make_circuit_search(rt, index)

        result = circuit_search("U1200 CAN connection")

        self.assertIn("未找到相关内容", result)
        self.assertEqual(rt.evidence, [])
        self.assertEqual(rt.diagnostics[0]["hit_count"], 0)

    def test_runner_uses_injected_circuit_service(self):
        from src.agents.runner import MultiSourceAgentRunner

        class _FakeRAGBackend:
            name = "fake"

            def retrieve(self, *args, **kwargs):
                return []

        circuit_index = _CircuitIndex()
        runner = MultiSourceAgentRunner(
            rag_backend=_FakeRAGBackend(),
            circuit_service=circuit_index,
        )

        self.assertIs(runner.circuit_service, circuit_index)


if __name__ == "__main__":
    unittest.main()
