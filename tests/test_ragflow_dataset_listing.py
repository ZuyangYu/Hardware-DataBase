import unittest
from unittest import mock

import httpx

from src.core.app_pipeline import AppPipeline
from src.pipelines.document_rag import ragflow_backend
from src.pipelines.document_rag.ragflow_backend import RAGFlowAPIError, RAGFlowClient


def _client_with_handler(handler) -> RAGFlowClient:
    client = RAGFlowClient(base_url="http://ragflow.test", api_key="test-key", timeout=5)
    client._sync_client.close()
    client._sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


class DatasetListingTests(unittest.TestCase):
    def test_unfiltered_listing_pages_through_every_dataset(self):
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = request.url.params
            self.assertNotIn("name", params)
            page = int(params.get("page", "1"))
            requested_pages.append(page)
            if page == 1:
                data = [{"id": f"ds-{i}", "name": f"ds-{i:03d}"} for i in range(100)]
            elif page == 2:
                data = [{"id": f"ds-{i}", "name": f"ds-{i:03d}"} for i in range(100, 105)]
            else:
                data = []
            return httpx.Response(200, json={"code": 0, "data": data, "total": 105})

        client = _client_with_handler(handler)
        datasets = client.list_datasets()

        self.assertEqual(105, len(datasets))
        # 只请求需要的两页，total 到位后停止。
        self.assertEqual([1, 2], requested_pages)

    def test_single_page_listing_stops_after_total_is_reached(self):
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_pages.append(int(request.url.params.get("page", "1")))
            return httpx.Response(
                200,
                json={"code": 0, "data": [{"id": "kb-1", "name": "ADAS_new"}], "total": 1},
            )

        client = _client_with_handler(handler)
        self.assertEqual(["ADAS_new"], [d["name"] for d in client.list_datasets()])
        self.assertEqual([1], requested_pages)

    def test_exact_name_lookup_found_returns_dataset(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("AEHW Agent", request.url.params.get("name"))
            return httpx.Response(
                200,
                json={"code": 0, "data": [{"id": "kb-9", "name": "AEHW Agent"}], "total": 1},
            )

        client = _client_with_handler(handler)
        hits = client.list_datasets(name="AEHW Agent")
        self.assertEqual(["AEHW Agent"], [item["name"] for item in hits])

    def test_exact_name_lookup_not_found_returns_empty_list(self):
        """RAGFlow 对查不到/不可见的名字回 code=102 的 'lacks permission'，
        客户端应视为「无匹配」而不是抛错。"""

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.params.get("name")
            return httpx.Response(
                200,
                json={
                    "code": 102,
                    "message": f"User 'tenant' lacks permission for dataset '{name}'",
                },
            )

        client = _client_with_handler(handler)
        self.assertEqual([], client.list_datasets(name="AEHW Agent"))
        self.assertEqual([], client.list_datasets(name="ghost"))

    def test_other_api_errors_still_raise(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 102, "message": "Authentication error"})

        client = _client_with_handler(handler)
        with self.assertRaises(RAGFlowAPIError):
            client.list_datasets(name="whatever")

    def test_ensure_dataset_creates_when_name_is_not_found(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                name = request.url.params.get("name")
                calls.append(f"GET:{name}")
                return httpx.Response(
                    200,
                    json={
                        "code": 102,
                        "message": f"User 'tenant' lacks permission for dataset '{name}'",
                    },
                )
            calls.append("POST:create")
            return httpx.Response(200, json={"code": 0, "data": {"id": "kb-new"}})

        client = _client_with_handler(handler)
        dataset_id = client.ensure_dataset("AEHW Agent")
        self.assertEqual("kb-new", dataset_id)
        self.assertEqual(["GET:AEHW Agent", "POST:create"], calls)


class RagflowHealthCheckTests(unittest.TestCase):
    class _StubClient:
        def __init__(self, **_kwargs):
            self.name_probes: list[str] = []

        def list_datasets(self, name: str | None = None):
            if name is None:
                return [{"name": "ADAS_new"}]
            self.name_probes.append(name)
            if name == "AEHW Agent":
                return [{"name": "AEHW Agent"}]
            return []

    def test_health_check_probes_exact_name_before_reporting_missing(self):
        with mock.patch.object(ragflow_backend, "RAGFlowClient", self._StubClient):
            reachable, message, missing = AppPipeline.check_ragflow_connection(
                base_url="http://ragflow.test",
                api_key="test-key",
                dataset_names=["ADAS_new", "AEHW Agent", "AEHW Ghost", "ADAS_new"],
            )

        self.assertTrue(reachable)
        # 名字探测能命中的不算缺失；重复配置去重；真正的缺失才会被报出。
        self.assertEqual(["AEHW Ghost"], missing)
        self.assertIn("AEHW Ghost", message)


if __name__ == "__main__":
    unittest.main()
