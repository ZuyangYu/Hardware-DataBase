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

    def test_concurrent_queries_get_unique_paired_call_ids(self):
        backend = _BarrierRAGBackend()
        events: list[dict] = []
        rt = ToolRuntime(kb_name="kb", ctx=None, on_event=events.append)
        batch = make_document_search_batch(rt, backend, document_store=None)

        batch(["LP87702 中文耐压", "LP87702 absolute maximum voltage"])

        starts = [event["payload"] for event in events if event["type"] == "tool_started"]
        results = [event["payload"] for event in events if event["type"] == "tool_result"]
        start_ids = [payload["call_id"] for payload in starts]
        result_ids = [payload["call_id"] for payload in results]

        self.assertEqual(len(start_ids), 2)
        self.assertEqual(len(result_ids), 2)
        self.assertEqual(len(set(start_ids)), 2)
        self.assertEqual(set(start_ids), set(result_ids))
        self.assertTrue(all(payload["item_id"] == f"tool:{payload['call_id']}" for payload in starts + results))

        # The barrier backend only returns after both workers enter retrieve(),
        # so both starts must be emitted before either result.
        last_start = max(index for index, event in enumerate(events) if event["type"] == "tool_started")
        first_result = min(index for index, event in enumerate(events) if event["type"] == "tool_result")
        self.assertLess(last_start, first_result)
        self.assertTrue(all("candidate_count" in payload for payload in results))


if __name__ == "__main__":
    unittest.main()
