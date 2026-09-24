"""Capability-driven unified conversational tool assembly tests."""

from __future__ import annotations

from src.agents.orchestration.tool_assembly import (
    filter_tools_by_capability,
    filter_tools_for_action_plan_narration,
)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_unified_assembly_keeps_authorized_retrieval_document_and_control_tools_together():
    tools = [
        _Tool("document_search"),
        _Tool("attachment_search"),
        _Tool("generate_document_from_template"),
        _Tool("resolve_icd_scope_exception"),
        _Tool("get_document_task_status"),
        _Tool("create_document_revision"),
        _Tool("memory_search"),
        _Tool("unclassified_write_tool"),
    ]

    selected = filter_tools_by_capability(
        tools,
        available_capabilities={
            "knowledge_base.search",
            "attachment.search",
            "document.authoring",
            "document.scope.resolve",
            "document.task.read",
        },
    )

    assert [tool.name for tool in selected] == [
        "document_search",
        "attachment_search",
        "generate_document_from_template",
        "resolve_icd_scope_exception",
        "get_document_task_status",
        "memory_search",
    ]


def test_missing_capability_hides_the_corresponding_write_tool():
    selected = filter_tools_by_capability(
        [_Tool("generate_document_from_template"), _Tool("create_document_revision")],
        available_capabilities={"document.authoring"},
    )

    assert [tool.name for tool in selected] == ["generate_document_from_template"]


def test_attachment_edit_is_visible_only_with_attachment_revision_capability():
    selected = filter_tools_by_capability(
        [_Tool("attachment_edit")],
        available_capabilities={"attachment.search"},
    )
    assert selected == []

    selected = filter_tools_by_capability(
        [_Tool("attachment_edit")],
        available_capabilities={"attachment.search", "attachment.revise"},
    )
    assert [tool.name for tool in selected] == ["attachment_edit"]


def test_unified_assembly_keeps_task_bound_control_tools_when_authorized():
    """Mounted control wrappers must survive the same capability filter as writes."""
    selected = filter_tools_by_capability(
        [
            _Tool("pause_document_task"),
            _Tool("cancel_document_task"),
            _Tool("resume_document_task"),
            _Tool("retry_document_task"),
        ],
        available_capabilities={"document.task.control", "document.task.resume"},
    )

    assert [tool.name for tool in selected] == [
        "pause_document_task",
        "cancel_document_task",
        "resume_document_task",
        "retry_document_task",
    ]


def test_action_plan_narration_toolset_is_read_only_and_fails_closed_for_unknown_tools():
    """A writer exposed after governed execution would let the model bypass the plan."""
    selected = filter_tools_for_action_plan_narration(
        [
            _Tool("document_search"),
            _Tool("get_document_task_status"),
            _Tool("memory_search"),
            _Tool("start_document_generation_session"),
            _Tool("resume_document_task"),
            _Tool("unclassified_future_tool"),
        ]
    )

    assert [tool.name for tool in selected] == [
        "document_search",
        "get_document_task_status",
        "memory_search",
    ]
