"""Wiki page retrieval tool: distilled pages + one-hop link expansion.

Wiki 页是文档蒸馏产物(摘要/实体/概念), 把蒸馏成果回灌到问答链路:
命中页作为 Evidence 进入证据池, 并沿互链一跳扩展相关词条。
"""

from __future__ import annotations

import json
import os
from typing import Any

from src.agents.schemas import Evidence
from src.core.wiki import WikiService
from src.services.kb_scope import kb_scope_from_context

MAX_WIKI_EVIDENCE_CHARS = 2000


def _forced_expansion() -> bool | None:
    """实验开关: HDB_WIKI_SEARCH_EXPAND=0/1 时强制覆盖 include_linked(生产未设置→None)。"""
    raw = os.getenv("HDB_WIKI_SEARCH_EXPAND", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return None


def _page_evidence(row: dict[str, Any]) -> Evidence:
    """Wiki 行 → Evidence; hop=1 的邻居页在 locator 里标注来源页。"""
    slug = str(row.get("slug") or "")
    title = str(row.get("title") or slug)
    hop = int(row.get("hop") or 0)
    try:
        links = json.loads(row.get("out_links_json") or "[]")
    except Exception:  # noqa: BLE001
        links = []
    return Evidence(
        id=f"wiki:{slug}",
        content=str(row.get("content") or "")[:MAX_WIKI_EVIDENCE_CHARS],
        source_name=f"Wiki: {title}",
        content_kind="wiki_page",
        processor_kind="wiki",
        score=float(row.get("score") or 0.0),
        locator={"slug": slug, "hop": hop, "via": str(row.get("via") or "")},
        metadata={
            "page_type": str(row.get("page_type") or ""),
            "summary": str(row.get("summary") or "")[:300],
            "out_links": [str(item) for item in links[:10]],
            "version": row.get("version"),
        },
    )


def make_wiki_search(rt, wiki_service: WikiService | None = None):
    """Return a ``wiki_search(query, top_k, include_linked)`` tool closure."""

    def wiki_search(query: str, top_k: int = rt.top_k, include_linked: bool = True) -> str:
        """检索知识 Wiki（由知识库文档蒸馏出的摘要/实体/概念页），命中页会沿互链一跳扩展到相关词条。
        适合概念/器件/接口的解释、"是什么/有哪些"类问题、以及跨文档综合；关键数值仍应以原始文档、表格、网表证据为准。"""
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        forced = _forced_expansion()
        if forced is not None:
            include_linked = forced

        def run(query: str, top_k: int) -> list[Evidence]:
            scope = kb_scope_from_context(rt.kb_name, rt.ctx)
            if not scope.department_id or scope.kb_id is None:
                return []
            service = wiki_service or WikiService()
            rows = service.search_pages(
                kb_id=int(scope.kb_id),
                department_id=int(scope.department_id),
                query=query,
                limit=top_k,
                expand_links=bool(include_linked),
            )
            return [_page_evidence(row) for row in rows]

        items, adds_nothing = timed_tool_call(
            rt, "wiki_search", query, None,
            lambda: run(query, max(1, min(int(top_k), 15))),
        )
        return format_tool_result(rt, adds_nothing, items)

    return wiki_search
