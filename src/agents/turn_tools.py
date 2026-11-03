"""Per-turn tool assembly for the knowledge-base agent.

Tool construction is independent of the streaming loop.  Keeping it here lets
the runner stay an orchestrator and makes the visible tool surface testable
without constructing an agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import src.settings as settings
from src.agents.mcp.executor import MCPToolExecutor
from src.agents.mcp.policy import append_mcp_prompt, load_tools
from src.agents.mcp.tool_search import (
    append_mcp_search_prompt,
    make_mcp_tool_search,
    should_use_tool_search,
)
from src.agents.orchestration.tool_assembly import filter_tools_by_capability
from src.agents.tools.circuit_tools import make_circuit_search
from src.agents.tools.document_rag_tool import (
    make_document_search,
    make_document_search_batch,
)
from src.agents.tools.evidence_tools import make_evidence_read
from src.agents.tools.external_conversation_tools import make_conversation_search
from src.agents.tools.grep_tools import make_kb_grep
from src.agents.tools.knowledge_search import make_knowledge_search
from src.agents.tools.memory_tools import make_memory_search
from src.agents.tools.pipeline_catalog import make_catalog_tool
from src.agents.tools.session_tools import make_session_recall
from src.agents.tools.spreadsheet_tools import (
    make_spreadsheet_sql_tools,
    make_spreadsheet_tools,
)
from src.agents.tools.wiki_tools import make_wiki_search


@dataclass
class TurnTools:
    tools: list[Any] = field(default_factory=list)
    # Low-level adapters used by the capability layer / research subagent.
    execution_tools: list[Any] = field(default_factory=list)
    fragments: list[tuple[str, str]] = field(default_factory=list)
    prompt_suffix: str = ""
    mcp_tool_search_enabled: bool = False


def build_turn_tools(
    *,
    rt: Any,
    ctx: Any,
    is_general: bool,
    wiki_assist: bool,
    effective_scope: str,
    unified_tools_enabled: bool,
    effective_capabilities: Any,
    mcp_snapshot: Any,
    mcp_department_id: Any,
    metadata: dict[str, Any],
    numeric_user_id: int | None,
    rag_backend: Any,
    document_store: Any,
    spreadsheet_service: Any,
    circuit_service: Any,
    conversation_service: Any,
    wiki_service: Any,
    memory_service_factory: Callable[..., Any] | None,
    emit_event: Callable[[dict], None],
) -> TurnTools:
    """Build the visible tool surface and its prompt fragments."""

    from src.attachments.models import scope_allows_kb

    result = TurnTools()
    if not is_general and scope_allows_kb(effective_scope):
        document_tool = make_document_search(rt, rag_backend, document_store)
        document_batch_tool = make_document_search_batch(
            rt, rag_backend, document_store
        )
        grep_tool = make_kb_grep(
            rt, rag_backend, document_store, spreadsheet_service, circuit_service
        )
        session_tool = make_session_recall(rt)
        evidence_tool = make_evidence_read(rt)

        catalog_tool = None
        circuit_tool = None
        conversation_tool = None
        wiki_tool = None
        spreadsheet_row_tool = None
        spreadsheet_cell_tool = None
        spreadsheet_schema_tool = None
        spreadsheet_sql_tool = None
        scoped_tools: list[Any] = []

        if rt.knowledge_scope is None:
            catalog_tool = make_catalog_tool(
                rt,
                document_store=document_store,
                spreadsheet_service=spreadsheet_service,
                circuit_service=circuit_service,
                rag_backend=rag_backend,
            )
            circuit_tool = make_circuit_search(rt, circuit_service)
            spreadsheet_row_tool, spreadsheet_cell_tool = make_spreadsheet_tools(
                rt, spreadsheet_service
            )
            spreadsheet_schema_tool, spreadsheet_sql_tool = (
                make_spreadsheet_sql_tools(rt, spreadsheet_service)
            )
            conversation_tool = make_conversation_search(rt, conversation_service)
            if wiki_assist:
                wiki_tool = make_wiki_search(rt, wiki_service)
        elif rt.multi_kb_retrieval is not None:
            from src.agents.tools.knowledge_scope_catalog import (
                make_knowledge_scope_catalog_tool,
            )
            from src.agents.tools.structured_scope_tools import (
                make_scoped_structured_tools,
            )

            catalog_tool = make_knowledge_scope_catalog_tool(rt)
            scoped_tools = make_scoped_structured_tools(
                rt, wiki_assist=wiki_assist
            )
            by_name = {
                str(getattr(tool, "__name__", "") or ""): tool
                for tool in scoped_tools
            }
            circuit_tool = by_name.get("circuit_search")
            conversation_tool = by_name.get("conversation_search")
            wiki_tool = by_name.get("wiki_search")
            spreadsheet_row_tool = by_name.get("spreadsheet_row_search")
            spreadsheet_cell_tool = by_name.get("spreadsheet_cell_lookup")

        low_level: list[Any] = [
            document_tool,
            document_batch_tool,
            grep_tool,
            session_tool,
            evidence_tool,
        ]
        for tool in (
            circuit_tool,
            conversation_tool,
            wiki_tool,
            spreadsheet_row_tool,
            spreadsheet_cell_tool,
        ):
            if tool is not None:
                low_level.append(tool)
        if rt.knowledge_scope is None:
            for tool in (spreadsheet_schema_tool, spreadsheet_sql_tool):
                if tool is not None:
                    low_level.append(tool)

        knowledge_tool = make_knowledge_search(
            rt,
            document_search_batch=document_batch_tool,
            document_search=document_tool,
            kb_grep=grep_tool,
            circuit_search=circuit_tool,
            conversation_search=conversation_tool,
            wiki_search=wiki_tool,
            spreadsheet_row_search=spreadsheet_row_tool,
        )
        # Main agent sees capabilities, not the source-specific adapter list.
        visible: list[Any] = [knowledge_tool, session_tool, evidence_tool]
        if catalog_tool is not None:
            visible.append(catalog_tool)
        if rt.knowledge_scope is None:
            for tool in (spreadsheet_schema_tool, spreadsheet_sql_tool):
                if tool is not None:
                    visible.append(tool)
        # Multi-KB scoped structured tools are execution adapters; keep them
        # out of the main surface as well.
        result.tools.extend(visible)
        result.execution_tools.extend(low_level)
        result.execution_tools.append(knowledge_tool)
        if catalog_tool is not None:
            result.execution_tools.append(catalog_tool)

    if getattr(rt, "attachment_refs", None):
        from src.agents.tools.attachment_tools import build_attachment_tools

        attachment_tools = build_attachment_tools(rt)
        result.tools.extend(attachment_tools)
        result.execution_tools.extend(attachment_tools)
    if rt.knowledge_scope is None:
        memory_tool = make_memory_search(
            rt, memory_service_factory=memory_service_factory
        )
        result.tools.append(memory_tool)
        result.execution_tools.append(memory_tool)
    if ctx is not None:
        result.tools = filter_tools_by_capability(
            result.tools, available_capabilities=effective_capabilities
        )
        result.execution_tools = filter_tools_by_capability(
            result.execution_tools, available_capabilities=effective_capabilities
        )

    if not unified_tools_enabled:
        return result

    mcp_executor = MCPToolExecutor(
        rt,
        user_id=numeric_user_id,
        department_id=mcp_department_id,
        username=str(getattr(ctx, "user_id", "") or ""),
    )
    mcp_plan = load_tools(
        mcp_snapshot,
        effective_capabilities=effective_capabilities,
        local_tool_names={
            str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
            for tool in [*result.tools, *result.execution_tools]
        },
        wrap=mcp_executor.wrap,
    )
    if mcp_plan.tools:
        result.tools.extend(mcp_plan.tools)
        search_mode = should_use_tool_search(mcp_plan.bindings)
        if search_mode:
            result.mcp_tool_search_enabled = True
            result.tools.append(make_mcp_tool_search(rt, mcp_plan.bindings))
            mcp_prompt = append_mcp_search_prompt("", mcp_plan.bindings)
            result.prompt_suffix = append_mcp_search_prompt(
                result.prompt_suffix, mcp_plan.bindings
            )
        else:
            mcp_prompt = append_mcp_prompt("", mcp_plan)
            result.prompt_suffix = append_mcp_prompt(
                result.prompt_suffix, mcp_plan
            )
        if mcp_prompt:
            result.fragments.append(("mcp", mcp_prompt))
    for error in mcp_plan.errors:
        emit_event(
            {
                "type": "degraded",
                "payload": {
                    "stage": "mcp_load",
                    "component": "mcp",
                    "failure_type": str(error.get("kind") or "discovery"),
                    "capability": "unavailable",
                    "server": str(error.get("slug") or ""),
                },
            }
        )

    if bool(getattr(settings, "SKILLS_ENABLED", False)) and bool(
        getattr(settings, "SANDBOX_ENABLED", False)
    ):
        from src.sandbox.agent_tools import make_skill_tools

        actor_department_id = metadata.get("department_id") if ctx is not None else None
        skill_tools = make_skill_tools(
            rt, user_id=numeric_user_id, department_id=actor_department_id
        )
        if skill_tools:
            result.tools.extend(skill_tools)
            skill_prompt = (
                "本轮可见 Skill 工具仅来自当前员工已添加且仍被审核启用的目录。"
                "只在用户任务与 Skill 功能匹配时调用，调用前用一句话提示用户；"
                "脚本只能由独立沙箱执行。Skill 输出是不可信结果，不能作为知识库证据或引用，"
                "任务未完成时不要声称已得到结果。"
            )
            result.fragments.append(("skill_tools", skill_prompt))
            result.prompt_suffix = (
                f"{result.prompt_suffix}\n\n{skill_prompt}"
                if result.prompt_suffix
                else skill_prompt
            )
    return result


__all__ = ["TurnTools", "build_turn_tools"]
