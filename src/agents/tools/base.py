from __future__ import annotations

from typing import Protocol

from src.agents.state import Evidence
from src.pipelines.document_rag.schemas import RequestContext


class AgentTool(Protocol):
    name: str
    description: str

    def run(
        self,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        ...

