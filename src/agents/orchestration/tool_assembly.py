"""Map governed conversation tools to the capabilities required to expose them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


# Tool names are the public LangChain boundary.  Keeping this mapping in one
# place means a new writer cannot accidentally appear in chat merely because a
# runner branch appended it to a list.
TOOL_CAPABILITIES: dict[str, str] = {
    "list_kb_sources": "knowledge_base.catalog",
    "document_search": "knowledge_base.search",
    "document_search_batch": "knowledge_base.search",
    "kb_grep": "knowledge_base.search",
    "session_recall": "knowledge_base.search",
    "evidence_read": "knowledge_base.search",
    "knowledge_search": "knowledge_base.search",
    "wiki_search": "knowledge_base.search",
    "list_knowledge_sources": "knowledge_base.catalog",
    "circuit_search": "knowledge_base.search",
    "spreadsheet_row_search": "knowledge_base.search",
    "spreadsheet_cell_lookup": "knowledge_base.search",
    "spreadsheet_schema_lookup": "knowledge_base.search",
    "spreadsheet_sql_query": "knowledge_base.search",
    "conversation_search": "knowledge_base.search",
    "attachment_list": "attachment.search",
    "attachment_search": "attachment.search",
    "attachment_read": "attachment.search",
    "attachment_table_query": "attachment.search",
    "attachment_circuit_search": "attachment.search",
    "attachment_visual_analyze": "attachment.search",
    "attachment_edit": "attachment.revise",
}

# Memory is a read-only conversational aid whose service independently
# scopes results.  It is not a KB/document write capability and remains
# available in ordinary general chat.
_ALWAYS_AVAILABLE = frozenset({"memory_search"})


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or getattr(tool, "__name__", "")).strip()


def filter_tools_by_capability(
    tools: Iterable[Any],
    *,
    available_capabilities: Iterable[str],
) -> list[Any]:
    """Return only named tools authorized by the resolved capability set.

    Unknown tool names fail closed.  This matters when a future developer
    adds a writer: it must deliberately be mapped here before the model can
    ever see it.
    """

    available = {str(capability).strip() for capability in available_capabilities}
    selected: list[Any] = []
    for tool in tools:
        name = _tool_name(tool)
        if name in _ALWAYS_AVAILABLE:
            selected.append(tool)
            continue
        required = TOOL_CAPABILITIES.get(name)
        if required and required in available:
            selected.append(tool)
    return selected


__all__ = [
    "TOOL_CAPABILITIES",
    "filter_tools_by_capability",
]
