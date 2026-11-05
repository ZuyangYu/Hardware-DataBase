"""Official SkillsMiddleware reads SKILL.md through read_file."""

from __future__ import annotations

from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import src.agents.runner  # noqa: F401 - registers the harness profile
from src.agents.assembly import build_agent
from src.agents.middleware.skill_read import SkillReadGateMiddleware


class _ToolFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
        return self


def test_gate_hides_read_file_only_without_skills():
    tools = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="document_search"),
    ]
    request = ModelRequest(
        model=object(),
        messages=[],
        tools=tools,
        state={"messages": []},
    )

    hidden = SkillReadGateMiddleware(enabled=False).wrap_model_call(
        request, lambda current: current.tools
    )
    visible = SkillReadGateMiddleware(enabled=True).wrap_model_call(
        request, lambda current: current.tools
    )

    assert [tool.name for tool in hidden] == ["document_search"]
    assert [tool.name for tool in visible] == ["read_file", "document_search"]


def test_read_file_reads_materialized_skill_in_real_agent_graph(tmp_path):
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "skill-hw-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: skill-hw-review\ndescription: Review hardware\n---\n\n"
        "PROBE_SKILL_BODY: check power, clock and storage.\n",
        encoding="utf-8",
    )
    model = _ToolFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/skill-hw-review/SKILL.md"},
                            "id": "read-skill-1",
                        }
                    ],
                ),
                AIMessage(content="skill read complete"),
            ]
        )
    )
    agent = build_agent(
        model=model,
        tools=[],
        system_prompt="You are a hardware assistant.",
        skill_root=str(skill_root),
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "read the skill"}]}
    )

    tool_messages = [
        message
        for message in result["messages"]
        if getattr(message, "name", "") == "read_file"
    ]
    assert tool_messages
    assert "PROBE_SKILL_BODY" in str(tool_messages[0].content)
    assert result["messages"][-1].content == "skill read complete"


def test_read_file_observer_reports_skill_body_read(tmp_path):
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "skill-hw-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: skill-hw-review\ndescription: Review hardware\n---\n\n"
        "PROBE_SKILL_BODY: check power, clock and storage.\n",
        encoding="utf-8",
    )
    model = _ToolFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/skill-hw-review/SKILL.md"},
                            "id": "read-skill-1",
                        }
                    ],
                ),
                AIMessage(content="skill read complete"),
            ]
        )
    )
    events: list[dict] = []
    agent = build_agent(
        model=model,
        tools=[],
        system_prompt="You are a hardware assistant.",
        skill_root=str(skill_root),
        skill_read_observer=lambda **event: events.append(event),
    )

    agent.invoke({"messages": [{"role": "user", "content": "read the skill"}]})

    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[0]["call_id"] == "read-skill-1"
    assert events[0]["path"] == "/skill-hw-review/SKILL.md"
    assert events[1]["status"] == "done"
    assert events[1]["result_bytes"] > 0
