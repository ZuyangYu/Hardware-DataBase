"""Agent retrieval tool over the local external-conversation index."""

from __future__ import annotations

from src.core.query_tokens import tokenize_hardware_query
from src.agents.schemas import Evidence
from src.pipelines.document_rag.schemas import RequestContext
from src.services.kb_scope import kb_scope_from_context

PROCESSOR_KIND_EXTERNAL_CONVERSATION = "external_conversation"


class ExternalConversationSearchTool:
    name = "conversation_search"
    description = (
        "Search external AI conversation records (外部数据 txt/markdown) by keyword "
        "over message text, titles and source files."
    )

    def __init__(self, conversation_service, vector_index=None):
        self.conversation_service = conversation_service
        if vector_index is None:
            from src.external_conversations.vector_index import default_external_conversation_vector_index

            vector_index = default_external_conversation_vector_index
        self.vector_index = vector_index

    def run(
        self,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        scope = kb_scope_from_context(kb_name, ctx).require_department("search external conversations in")
        requested = max(1, int(top_k))
        try:
            rows = self.conversation_service.search_by_scope(
                scope.department_id, scope.kb_name, query, top_k=requested
            )
        except Exception:
            rows = []
        # semantic supplement: only active when an embedding model + chromadb
        # are configured; dedupe against keyword hits by conversation+turn.
        try:
            seen_keys = {f"{r['conversation_id']}:{r['kind'] if 'kind' in r else 'm'}{r['turn_index']}" for r in rows}
            for row in self.vector_index.semantic_search(scope.department_id, scope.kb_name, query, top_k=requested):
                key = f"{row['conversation_id']}:{row.get('kind', 'm')}{row['turn_index']}"
                if key not in seen_keys:
                    rows.append(row)
                    seen_keys.add(key)
        except Exception:
            pass
        rows = rows[:requested]

        tokens = {t.casefold() for t in tokenize_hardware_query(query, max_tokens=16)}
        evidences: list[Evidence] = []
        for position, row in enumerate(rows):
            content = str(row.get("content") or "")
            searchable = f"{content}\n{row.get('title') or ''}".casefold()
            matched = sum(1 for token in tokens if token and token in searchable)
            evidences.append(
                Evidence(
                    id=f"conv:{row['conversation_id']}:{row['message_id']}",
                    content=content,
                    source_name=str(row.get("source_file") or row.get("title") or row["conversation_id"]),
                    content_kind=PROCESSOR_KIND_EXTERNAL_CONVERSATION,
                    processor_kind=PROCESSOR_KIND_EXTERNAL_CONVERSATION,
                    score=1.0 + 0.1 * matched - (0.05 if row.get("vector_distance") is not None else 0.0) + (0.001 * position),
                    locator={
                        "conversation_id": row["conversation_id"],
                        "message_id": row["message_id"],
                        "turn_index": row.get("turn_index"),
                        "start_offset": row.get("start_offset"),
                    },
                    metadata={
                        "tool": self.name,
                        "query": query,
                        "role": row.get("role"),
                        "origin": row.get("origin"),
                        "source_group": row.get("source_group"),
                        "department_id": scope.department_id,
                        "title": row.get("title"),
                    },
                )
            )
        return evidences[: max(1, int(top_k))]


def make_conversation_search(rt, conversation_service):
    """Return a ``conversation_search(query, top_k)`` tool closure."""
    tool = ExternalConversationSearchTool(conversation_service)

    def conversation_search(query: str, top_k: int = rt.top_k) -> str:
        """在知识库中检索外部 AI 对话记录（外部数据 txt/markdown）：历史问答、经验结论、参数与踩坑细节。"""
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        items, adds_nothing = timed_tool_call(
            rt,
            "conversation_search",
            query,
            None,
            lambda: tool.run(query, rt.kb_name, rt.ctx, max(1, min(int(top_k), 20))),
        )
        return format_tool_result(rt, adds_nothing, items)

    return conversation_search
