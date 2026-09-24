"""document_search_batch 的真实并发行为回归测试。"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from src.agents.tools.document_rag_tool import make_document_search_batch
from src.agents.tools.runtime import ToolRuntime


class _BarrierRAGBackend:
    name = "barrier-fake"

    def __init__(self):
        self.barrier = threading.Barrier(2)

    def retrieve(self, _kb_name, query, **_kwargs):
        # Sequential execution would time out/break the barrier. Two workers
        # reaching this point proves the batch fan-out is concurrent.
        self.barrier.wait(timeout=2.0)
        return [
            SimpleNamespace(
                id=f"doc:{query}",
                content=f"证据：{query}",
                source_name="测试文档",
                backend="ragflow",
                retriever="vector",
                score=1.0,
                metadata={},
            )
        ]


class DocumentSearchBatchTests(unittest.TestCase):
    def test_independent_queries_run_concurrently_and_merge(self):
        backend = _BarrierRAGBackend()
        rt = ToolRuntime(kb_name="kb", ctx=None)
        batch = make_document_search_batch(rt, backend, document_store=None)

        result = batch(["LP87702 中文耐压", "LP87702 absolute maximum voltage"])

        self.assertIn("LP87702 中文耐压", result)
        self.assertIn("LP87702 absolute maximum voltage", result)
        self.assertEqual(len(rt.diagnostics), 2)
        self.assertEqual(len(rt.evidence), 2)


if __name__ == "__main__":
    unittest.main()
