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
    "generate_document_from_template": "document.authoring",
    "start_document_generation_session": "document.authoring",
    "answer_clarification": "document.authoring",
    "confirm_generation_session": "document.authoring",
    "create_document_work_order": "document.authoring",
    "propose_document_plan": "document.authoring",
    "confirm_document_plan": "document.authoring",
    "create_document_revision": "document.revise",
    "get_document_template_analysis": "document.task.read",
    "get_document_generation_status": "document.task.read",
    "get_document_task_status": "document.task.read",
    "pause_document_task": "document.task.control",
    "cancel_document_task": "document.task.control",
    "resume_document_task": "document.task.resume",
    "retry_document_task": "document.task.control",
    "resolve_icd_scope_exception": "document.scope.resolve",
    "request_document_release": "document.release.request",
}

# Memory is a read-only conversational aid whose service independently
# scopes results.  It is not a KB/document write capability and remains
# available in ordinary general chat.
_ALWAYS_AVAILABLE = frozenset({"memory_search"})

# Once an enabled action plan has been handed to the server executor, the
# deep-agent loop is only a narrator and an evidence reader.  This explicit
# allowlist is deliberately separate from capability filtering: an otherwise
# authorized writer must still be hidden here, because only the action-plan
# executor may dispatch mutations for the current turn.  Unknown tools fail
# closed so adding a future writer cannot silently reopen that bypass.
_ACTION_PLAN_NARRATION_TOOLS = frozenset({
    "list_kb_sources",
    "document_search",
    "document_search_batch",
    "circuit_search",
    "spreadsheet_row_search",
    "spreadsheet_cell_lookup",
    "spreadsheet_schema_lookup",
    "spreadsheet_sql_query",
    "conversation_search",
    "attachment_list",
    "attachment_search",
    "attachment_read",
    "attachment_table_query",
    "attachment_circuit_search",
    "attachment_visual_analyze",
    "get_document_template_analysis",
    "get_document_generation_status",
    "get_document_task_status",
    "memory_search",
})


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


def filter_tools_for_action_plan_narration(tools: Iterable[Any]) -> list[Any]:
    """Expose only scoped read tools to the post-plan explanation loop.

    The caller must first pass the full toolset to the governed executor.
    This function is intentionally a second, stricter boundary for the model
    that explains its receipts afterwards; it does not grant capabilities and
    cannot be used to make an unclassified tool visible.
    """

    return [
        tool
        for tool in tools
        if _tool_name(tool) in _ACTION_PLAN_NARRATION_TOOLS
    ]


__all__ = [
    "TOOL_CAPABILITIES",
    "filter_tools_by_capability",
    "filter_tools_for_action_plan_narration",
]
