"""Lazy MCP tool discovery and schema filtering."""

from __future__ import annotations

from types import SimpleNamespace

from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from src.agents.mcp.policy import McpRunPlan, McpToolBinding
from src.agents.mcp.tool_search import (
    MAX_SEARCH_CALLS,
    SEARCH_TOOL_NAME,
    append_mcp_search_prompt,
    make_mcp_tool_search,
    search_bindings,
    should_use_tool_search,
)
from src.agents.middleware.mcp_tool_search import McpToolSearchMiddleware
from src.agents.turn_tools import build_turn_tools
from src.agents.tools.runtime import ToolRuntime


def _binding(index: int, *, server: str = "stub") -> McpToolBinding:
    return McpToolBinding(
        server=SimpleNamespace(name="桩服务", slug=server),
        raw_tool=SimpleNamespace(
            description=f"Extra tool {index} for power-rail review"
        ),
        tool_name=f"extra_{index}",
        safe_name=f"mcp__{server}__extra_{index}",
        read_only=True,
        transport="streamable_http",
    )


def _bindings(count: int = 13) -> list[McpToolBinding]:
    return [_binding(index) for index in range(count)]


def _runtime() -> ToolRuntime:
    return ToolRuntime(kb_name="kb_hw", ctx=None, question="review rails")


class _ToolFakeModel(GenericFakeChatModel):
    """Fake model that accepts create_agent's bind_tools call."""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
        return self


def test_tool_search_threshold():
    assert should_use_tool_search(_bindings(2), min_tools=2)
    assert not should_use_tool_search(_bindings(2), min_tools=3)


def test_search_ranks_exact_tool_name_first():
    hits = search_bindings(_bindings(), "extra_7")
    assert hits
    assert hits[0].safe_name == "mcp__stub__extra_7"


def test_empty_query_browses_a_bounded_catalogue():
    hits = search_bindings(_bindings(), "", limit=3)
    assert [item.safe_name for item in hits] == sorted(
        item.safe_name for item in _bindings()
    )[:3]


def test_search_tool_selects_tools_without_consuming_mcp_quota():
    rt = _runtime()
    tool = make_mcp_tool_search(rt, _bindings())

    rendered = tool.invoke({"query": "extra_3"})

    assert "mcp__stub__extra_3" in rendered
    assert rt.selected_mcp_tools == {"mcp__stub__extra_3"}
    assert rt.mcp_call_count == 0
    assert rt.mcp_diagnostics[-1]["tool_name"] == SEARCH_TOOL_NAME


def test_search_tool_stops_after_call_cap():
    rt = _runtime()
    rt.mcp_search_count = MAX_SEARCH_CALLS
    tool = make_mcp_tool_search(rt, _bindings())

    rendered = tool.invoke({"query": "extra_1"})

    assert "搜索次数已达上限" in rendered
    assert not rt.selected_mcp_tools


def test_search_prompt_lists_servers_not_every_tool():
    rendered = append_mcp_search_prompt("BASE", _bindings())

    assert rendered.startswith("BASE")
    assert SEARCH_TOOL_NAME in rendered
    assert "桩服务（stub）" in rendered
    assert "mcp__stub__extra_0" not in rendered


def test_middleware_hides_unselected_mcp_tools():
    rt = _runtime()
    rt.selected_mcp_tools = {"mcp__stub__a"}
    middleware = McpToolSearchMiddleware(rt)
    tools = [
        SimpleNamespace(name="document_search"),
        SimpleNamespace(name="mcp__stub__a"),
        SimpleNamespace(name="mcp__stub__b"),
        SimpleNamespace(name=SEARCH_TOOL_NAME),
    ]
    request = ModelRequest(
        model=object(),
        messages=[],
        tools=tools,
        state={"messages": []},
    )

    visible = middleware.wrap_model_call(request, lambda current: current.tools)

    assert [tool.name for tool in visible] == [
        "document_search",
        "mcp__stub__a",
        SEARCH_TOOL_NAME,
    ]


def test_search_then_call_works_inside_a_real_agent_graph():
    rt = _runtime()
    calls: list[str] = []

    def _alpha(text: str = "") -> str:
        """Return alpha's guarded result."""
        calls.append("alpha")
        return f"alpha-result:{text}"

    def _beta(text: str = "") -> str:
        """Return beta's guarded result."""
        calls.append("beta")
        return f"beta-result:{text}"

    alpha = StructuredTool.from_function(
        func=_alpha, name="mcp__stub__alpha", description="alpha tool"
    )
    beta = StructuredTool.from_function(
        func=_beta, name="mcp__stub__beta", description="beta tool"
    )
    bindings = [
        McpToolBinding(
            server=SimpleNamespace(name="桩服务", slug="stub"),
            raw_tool=SimpleNamespace(description="alpha tool"),
            tool_name="alpha",
            safe_name="mcp__stub__alpha",
            read_only=True,
            transport="streamable_http",
        ),
        McpToolBinding(
            server=SimpleNamespace(name="桩服务", slug="stub"),
            raw_tool=SimpleNamespace(description="beta tool"),
            tool_name="beta",
            safe_name="mcp__stub__beta",
            read_only=True,
            transport="streamable_http",
        ),
    ]
    search = make_mcp_tool_search(rt, bindings)
    model = _ToolFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": SEARCH_TOOL_NAME,
                            "args": {"query": "alpha"},
                            "id": "search-1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "mcp__stub__alpha",
                            "args": {"text": "payload"},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(content="完成"),
            ]
        )
    )
    agent = create_agent(
        model=model,
        tools=[alpha, beta, search],
        middleware=[McpToolSearchMiddleware(rt)],
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "review the rail"}]}
    )

    assert calls == ["alpha"]
    assert "mcp__stub__alpha" in rt.selected_mcp_tools
    assert any(
        "alpha-result:payload" in str(message.content)
        for message in result["messages"]
    )


def _assemble(monkeypatch, bindings):
    from src.agents import turn_tools as turn_tools_mod

    tools = [
        StructuredTool.from_function(
            func=lambda text="": "ok",
            name=binding.safe_name,
            description=binding.tool_name,
        )
        for binding in bindings
    ]
    plan = McpRunPlan(bindings=list(bindings), tools=tools)
    monkeypatch.setattr(turn_tools_mod, "load_tools", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        turn_tools_mod,
        "MCPToolExecutor",
        lambda *args, **kwargs: SimpleNamespace(wrap=lambda binding: None),
    )
    return build_turn_tools(
        rt=_runtime(),
        ctx=SimpleNamespace(user_id="u", metadata={}),
        is_general=True,
        wiki_assist=False,
        effective_scope="auto",
        unified_tools_enabled=True,
        effective_capabilities=["external_tool.invoke"],
        mcp_snapshot=SimpleNamespace(servers=(object(),)),
        mcp_department_id=None,
        metadata={},
        numeric_user_id=1,
        rag_backend=None,
        document_store=None,
        spreadsheet_service=None,
        circuit_service=None,
        conversation_service=None,
        wiki_service=None,
        memory_service_factory=None,
        emit_event=lambda event: None,
    )


def test_turn_tools_enables_search_only_above_threshold(monkeypatch):
    many = _assemble(monkeypatch, _bindings(13))
    assert many.mcp_tool_search_enabled is True
    many_names = {getattr(tool, "name", "") for tool in many.tools}
    assert SEARCH_TOOL_NAME in many_names
    # Every guarded wrapper stays bound so the middleware can reveal it later.
    assert "mcp__stub__extra_0" in many_names
    assert "先调用 mcp_tool_search" in many.prompt_suffix

    few = _assemble(monkeypatch, _bindings(2))
    assert few.mcp_tool_search_enabled is False
    assert SEARCH_TOOL_NAME not in {getattr(tool, "name", "") for tool in few.tools}
    assert "本轮可用外部插件只读工具" in few.prompt_suffix
