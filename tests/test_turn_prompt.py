"""Prompt assembly order is explicit and independent of the agent graph."""

from __future__ import annotations

from src.agents import turn_prompt as tp
from src.agents.tools.evidence_tools import CANDIDATE_PROMPT


def _build(**overrides):
    kwargs = dict(
        kb_name="kb_hw",
        workflow="WORKFLOW",
        kb_scope_line="SCOPE",
        is_general=False,
        wiki_assist=True,
    )
    kwargs.update(overrides)
    return tp.build_turn_prompt(**kwargs)


def test_fragment_order_and_prompt_sections(monkeypatch):
    monkeypatch.setattr(tp.settings, "SYSTEM_PROMPT", "BASE {kb_name} {workflow}")

    prompt = _build(
        memory_context="MEMORY-CONTEXT",
        skill_context="SKILL-CONTEXT",
        tool_fragments=[("mcp", "MCP-CONTEXT"), ("skill_tools", "TOOL-CONTEXT")],
        tool_suffix="TOOL-SUFFIX",
        export=True,
    )

    assert [kind for kind, _ in prompt.fragments] == [
        "kb_scope",
        "base_rules",
        "memory",
        "skill",
        "mcp",
        "skill_tools",
        "export",
    ]
    assert prompt.system_prompt.startswith("SCOPE\n\nBASE kb_hw WORKFLOW")
    positions = [
        prompt.system_prompt.index(token)
        for token in (
            CANDIDATE_PROMPT,
            "MEMORY-CONTEXT",
            "SKILL-CONTEXT",
            "TOOL-SUFFIX",
            "## 导出任务协议",
        )
    ]
    assert positions == sorted(positions)
    # Tool prompt text is carried by the suffix, not repeated per fragment.
    assert "MCP-CONTEXT" not in prompt.system_prompt
    assert "TOOL-CONTEXT" not in prompt.system_prompt


def test_general_turn_skips_candidate_rules(monkeypatch):
    monkeypatch.setattr(tp.settings, "SYSTEM_PROMPT", "BASE")

    prompt = _build(is_general=True, wiki_assist=False)

    assert CANDIDATE_PROMPT not in prompt.system_prompt
    assert "本次未启用 Wiki 检索" in prompt.system_prompt
    assert [kind for kind, _ in prompt.fragments] == ["kb_scope", "base_rules"]


def test_blank_system_prompt_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(tp.settings, "SYSTEM_PROMPT", "   ")

    prompt = _build()

    assert "Hardware DataBase" in prompt.system_prompt
    assert "Hardware DataBase" in tp.SYSTEM_PROMPT
