"""P0-007: bounded shared retrieval executor, cancellation and context."""

from __future__ import annotations

import contextvars
import threading
import time
import unittest
from types import SimpleNamespace

import src.settings
from src.agents.tools.document_rag_tool import make_document_search_batch
from src.agents.tools.retrieval_executor import (
    RetrievalExecutor,
    get_retrieval_executor,
    reset_retrieval_executor,
)
from src.agents.tools.runtime import ToolRuntime


class RetrievalExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = RetrievalExecutor(
            max_workers=2,
            queue_max_depth=4,
            per_request_fanout=2,
            queue_max_wait_seconds=0.2,
        )
        self.addCleanup(self.executor.close)

    def test_results_keep_order_and_fanout_is_bounded(self):
        active = 0
        high_water = 0
        lock = threading.Lock()

        def work(item):
            nonlocal active, high_water
            with lock:
                active += 1
                high_water = max(high_water, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return item * 2

        results = self.executor.map_bounded(work, [1, 2, 3, 4], fanout=2)
        self.assertEqual([item.value for item in results], [2, 4, 6, 8])
        self.assertTrue(all(item.status == "ok" for item in results))
        self.assertLessEqual(high_water, 2)

    def test_saturated_queue_marks_remaining_calls(self):
        executor = RetrievalExecutor(
            max_workers=1,
            queue_max_depth=1,
            per_request_fanout=4,
            queue_max_wait_seconds=0.15,
        )
        self.addCleanup(executor.close)
        release = threading.Event()

        def work(item):
            release.wait(timeout=2)
            return item

        results: list = []
        thread = threading.Thread(target=lambda: results.extend(executor.map_bounded(work, [1, 2, 3, 4])))
        thread.start()
        # Keep the single worker busy past the queue deadline so the remaining
        # calls are shed instead of being admitted late.
        time.sleep(0.3)
        release.set()
        thread.join(timeout=3)
        statuses = [item.status for item in results]
        self.assertIn("saturated", statuses)
        self.assertEqual(len(results), 4)

    def test_cancel_discards_remaining_work(self):
        state = {"started": 0}

        def work(item):
            state["started"] += 1
            time.sleep(0.05)
            return item

        results = self.executor.map_bounded(
            work,
            [1, 2, 3, 4],
            cancel_check=lambda: state["started"] >= 1,
        )
        self.assertEqual(len(results), 4)
        self.assertIn("cancelled", [item.status for item in results])

    def test_contextvars_propagate_to_workers(self):
        probe = contextvars.ContextVar("retrieval_probe", default="missing")
        token = probe.set("ctx-value")
        try:
            results = self.executor.map_bounded(lambda _item: probe.get(), [1])
        finally:
            probe.reset(token)
        self.assertEqual(results[0].value, "ctx-value")

    def test_singleton_reset_rebuilds_executor(self):
        reset_retrieval_executor()
        self.addCleanup(reset_retrieval_executor)
        first = get_retrieval_executor()
        self.assertIs(first, get_retrieval_executor())
        reset_retrieval_executor()
        self.assertIsNot(first, get_retrieval_executor())


class _BarrierBackend:
    name = "barrier-fake"

    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)

    def retrieve(self, _kb_name, query, **_kwargs):
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


class DocumentSearchBatchSharedExecutorTests(unittest.TestCase):
    def setUp(self):
        reset_retrieval_executor()
        self.addCleanup(reset_retrieval_executor)
        self._original_enabled = src.settings.SHARED_RETRIEVAL_EXECUTOR_ENABLED
        src.settings.SHARED_RETRIEVAL_EXECUTOR_ENABLED = True
        self.addCleanup(
            setattr,
            src.settings,
            "SHARED_RETRIEVAL_EXECUTOR_ENABLED",
            self._original_enabled,
        )

    def test_batch_uses_shared_executor_and_merges(self):
        backend = _BarrierBackend()
        rt = ToolRuntime(kb_name="kb", ctx=None)
        batch = make_document_search_batch(rt, backend, document_store=None)
        result = batch(["LP87702 中文耐压", "LP87702 absolute maximum voltage"])
        self.assertIn("LP87702 中文耐压", result)
        self.assertIn("LP87702 absolute maximum voltage", result)
        self.assertEqual(get_retrieval_executor().stats()["alive"], 1)


class ToolCallEpochEvidenceTests(unittest.TestCase):
    """P0-007: evidence from a finished/revoked tool call is rejected."""

    def _evidence(self, item_id: str):
        from src.agents.schemas import Evidence

        return Evidence(
            id=item_id,
            content=f"content-{item_id}",
            source_name="doc",
            content_kind="document_text",
            processor_kind="ragflow",
        )

    def test_late_evidence_after_call_end_is_rejected(self):
        from src.agents.tools.runtime import ToolRuntime

        rt = ToolRuntime(kb_name="kb", ctx=None)
        epoch = rt.begin_tool_call()
        self.assertEqual(rt.add_evidence([self._evidence("e1")], epoch=epoch), [1])
        rt.end_tool_call(epoch)
        self.assertEqual(rt.add_evidence([self._evidence("e2")], epoch=epoch), [None])
        self.assertEqual([item.id for item in rt.evidence], ["e1"])

    def test_cancellation_revokes_active_call_epochs(self):
        from src.agents.tools.runtime import ToolRuntime

        cancelled = {"value": False}
        rt = ToolRuntime(kb_name="kb", ctx=None, should_cancel=lambda: cancelled["value"])
        epoch = rt.begin_tool_call()
        cancelled["value"] = True
        with self.assertRaises(Exception):
            rt.check_cancel()
        self.assertEqual(rt.add_evidence([self._evidence("e1")], epoch=epoch), [None])

    def test_concurrent_active_epochs_both_register(self):
        from src.agents.tools.runtime import ToolRuntime

        rt = ToolRuntime(kb_name="kb", ctx=None)
        first = rt.begin_tool_call()
        second = rt.begin_tool_call()
        self.assertEqual(rt.add_evidence([self._evidence("a")], epoch=first), [1])
        self.assertEqual(rt.add_evidence([self._evidence("b")], epoch=second), [2])
        rt.end_tool_call(first)
        rt.end_tool_call(second)


    def test_close_waits_for_running_calls_within_deadline(self):
        from src.agents.tools.retrieval_executor import RetrievalExecutor

        executor = RetrievalExecutor(max_workers=1)
        finished = threading.Event()

        def slow():
            time.sleep(0.15)
            finished.set()
            return "ok"

        executor.submit(slow)
        unconfirmed = executor.close(deadline_seconds=2.0)
        self.assertEqual(unconfirmed, 0)
        self.assertTrue(finished.is_set())

    def test_close_reports_unconfirmed_and_refuses_new_submissions(self):
        from src.agents.tools.retrieval_executor import (
            RetrievalExecutor,
            RetrievalSaturated,
        )

        executor = RetrievalExecutor(max_workers=1)
        started = threading.Event()

        def blocked():
            started.set()
            time.sleep(0.6)
            return "late"

        executor.submit(blocked)
        self.assertTrue(started.wait(timeout=2.0))
        unconfirmed = executor.close(deadline_seconds=0.05)
        self.assertGreaterEqual(unconfirmed, 1)
        with self.assertRaises(RetrievalSaturated):
            executor.submit(lambda: "x")

    def test_reset_retrieval_executor_forwards_shutdown_deadline(self):
        reset_retrieval_executor()
        executor = get_retrieval_executor()
        executor.submit(lambda: time.sleep(0.05))
        unconfirmed = reset_retrieval_executor(deadline_seconds=1.0)
        self.assertEqual(unconfirmed, 0)


if __name__ == "__main__":
    unittest.main()
