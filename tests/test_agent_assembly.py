"""Agent assembly is the single create_deep_agent boundary."""

from __future__ import annotations

from src.agents.assembly import build_agent
from src.agents.middleware.session_context import SessionContextMiddleware


def test_build_agent_passes_checkpointer_and_session_middleware():
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return "agent"

    agent = build_agent(
        model="model",
        tools=["tool"],
        system_prompt="prompt",
        checkpointer="checkpoint",
        subagents=["subagent"],
        session_state_text="<session_state>x</session_state>",
        factory=factory,
    )

    assert agent == "agent"
    assert captured["model"] == "model"
    assert captured["tools"] == ["tool"]
    assert captured["checkpointer"] == "checkpoint"
    assert captured["subagents"] == ["subagent"]
    assert isinstance(captured["middleware"][-1], SessionContextMiddleware)


def test_build_agent_omits_middleware_without_session_state():
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return "agent"

    build_agent(
        model="model",
        tools=[],
        system_prompt="prompt",
        factory=factory,
    )

    assert "middleware" not in captured
    assert "subagents" not in captured


def test_build_agent_wires_official_skills_backend(tmp_path):
    captured = {}
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    def factory(**kwargs):
        captured.update(kwargs)
        return "agent"

    build_agent(
        model="model",
        tools=[],
        system_prompt="prompt",
        skill_root=str(skill_root),
        factory=factory,
    )

    assert captured["skills"] == ["/"]
    assert captured["backend"].__class__.__name__ == "FilesystemBackend"
    assert captured["permissions"][0].mode == "deny"
