import asyncio
import time
import unittest

from src.agents.tools.runtime import ToolRuntime, timed_tool_call
from src.core.cancellation import QueryCancelled
from src.pipelines.document_rag.ragflow_backend import RAGFlowClient


class _SlowAsyncClient:
    def __init__(self, **_kwargs):
        self.cancelled = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def request(self, *_args, **_kwargs):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class CancellableRAGFlowRetrievalTests(unittest.TestCase):
    def test_client_cancels_slow_http_request_promptly(self):
        slow_client = _SlowAsyncClient()
        client = RAGFlowClient(
            base_url="http://ragflow.test",
            api_key="test-key",
            timeout=30,
            async_client_factory=lambda **_kwargs: slow_client,
        )
        deadline = time.monotonic() + 0.12
        started = time.monotonic()

        with self.assertRaises(QueryCancelled):
            client.retrieve(
                "find regulator",
                dataset_ids=["dataset-1"],
                top_k=5,
                should_cancel=lambda: time.monotonic() >= deadline,
            )

        self.assertTrue(slow_client.cancelled)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_tool_runtime_propagates_cancellation_instead_of_recording_tool_failure(self):
        rt = ToolRuntime(kb_name="kb", ctx=None, should_cancel=lambda: True)

        def _cancelled_tool(*_args, **_kwargs):
            raise QueryCancelled()

        with self.assertRaises(QueryCancelled):
            timed_tool_call(rt, "document_search", "find regulator", None, _cancelled_tool)


if __name__ == "__main__":
    unittest.main()
