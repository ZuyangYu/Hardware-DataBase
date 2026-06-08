# src/core/custom_reranker.py
import requests
from typing import List, Optional
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import NodeWithScore, QueryBundle
from src.core.logger import error
from pydantic import Field


class APIReranker(BaseNodePostprocessor):
    """API Reranker(OpenAI 兼容格式)"""

    def __init__(self, model: str, api_key: str, api_base: str, top_n: int = 5):
        super().__init__()
        self.model = model
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.top_n = top_n

    def _postprocess_nodes(
            self,
            nodes: List[NodeWithScore],
            query_bundle: Optional[QueryBundle] = None
    ) -> List[NodeWithScore]:
        if not query_bundle or not nodes:
            return nodes[:self.top_n]

        try:
            query = query_bundle.query_str
            documents = [node.node.get_content() for node in nodes]

            # 调用 Rerank API
            response = requests.post(
                f"{self.api_base}/rerank",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                    "top_n": self.top_n
                },
                timeout=60
            )

            if response.status_code != 200:
                error(f"API Reranker 失败: {response.text}")
                return nodes[:self.top_n]

            results = response.json().get("results", [])

            # 重新排序
            if results:
                reranked_nodes = []
                for result in results:
                    idx = result.get("index")
                    score = result.get("relevance_score", 0)
                    if idx < len(nodes):
                        node = nodes[idx]
                        node.score = float(score)
                        reranked_nodes.append(node)

                return reranked_nodes[:self.top_n]

            return nodes[:self.top_n]

        except Exception as e:
            error(f"API Reranker 错误: {e}")
            return nodes[:self.top_n]


class NoReranker(BaseNodePostprocessor):
    """不使用 Reranker,直接返回 Top-N 结果"""

    top_n: int = Field(default=5, description="返回的节点数量")

    def _postprocess_nodes(
            self,
            nodes: List[NodeWithScore],
            query_bundle: Optional[QueryBundle] = None
    ) -> List[NodeWithScore]:
        # 直接截取前 N 个
        return nodes[:self.top_n]
