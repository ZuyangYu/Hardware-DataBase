import asyncio
import time
import unittest
from unittest.mock import patch

import requests

from src.agents.graph import retrieve_evidence
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
    def test_retrieval_retries_transient_failures_and_reuses_session(self):
        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"chunks": [{"id": "chunk-1"}]}}

        class _Session:
            def __init__(self):
                self.calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise requests.ConnectionError("temporary outage")
                return _Response()

        client = RAGFlowClient(base_url="http://ragflow.test", api_key="test-key", timeout=1)
        session = _Session()
        client._session = session

        with patch("src.pipelines.document_rag.ragflow_backend.time.sleep"):
            chunks = client.retrieve("find regulator", dataset_ids=["dataset-1"], top_k=5)

        self.assertEqual(chunks, [{"id": "chunk-1"}])
        self.assertEqual(session.calls, 3)
        self.assertIs(client._session, session)

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

    def test_graph_propagates_cancellation_instead_of_recording_tool_failure(self):
        class _CancelledDocumentTool:
            name = "document_rag"
            supports_cancellation = True

            def run(self, *_args, **_kwargs):
                raise QueryCancelled()

        state = {
            "kb_name": "kb",
            "user_query": "find regulator",
            "source_plan": {
                "source_plan": [
                    {"tool_calls": [{"tool_name": "document_rag", "query": "find regulator", "top_k": 5}]}
                ]
            },
            "retrieval_round": 0,
            "evidence": [],
            "trace": [],
            "_cancel_check": lambda: True,
        }

        with self.assertRaises(QueryCancelled):
            retrieve_evidence(state, {"document_rag": _CancelledDocumentTool()})


if __name__ == "__main__":
    unittest.main()
