"""Prompt assembly order is explicit and independent of the agent graph."""

from __future__ import annotations

from src.agents import turn_prompt as tp


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
        tool_fragments=[("mcp", "MCP-CONTEXT"), ("skill_tools", "TOOL-CONTEXT")],
        tool_suffix="TOOL-SUFFIX",
        export=True,
    )

    assert [kind for kind, _ in prompt.fragments] == [
        "kb_scope",
        "base_rules",
        "memory",
        "mcp",
        "skill_tools",
        "export",
    ]
    assert prompt.system_prompt.startswith("SCOPE\n\n")
    assert "BASE kb_hw WORKFLOW" in prompt.system_prompt
    positions = [
        prompt.system_prompt.index(token)
        for token in (
            "## 知识库工具",
            "MEMORY-CONTEXT",
            "TOOL-SUFFIX",
            "## 导出任务协议",
        )
    ]
    assert positions == sorted(positions)
    assert "## 知识库工具（仅在挂载时）" in prompt.system_prompt
    # Tool prompt text is carried by the suffix, not repeated per fragment.
    assert "MCP-CONTEXT" not in prompt.system_prompt
    assert "TOOL-CONTEXT" not in prompt.system_prompt


def test_general_turn_skips_candidate_rules(monkeypatch):
    monkeypatch.setattr(tp.settings, "SYSTEM_PROMPT", "BASE")

    prompt = _build(is_general=True, wiki_assist=False)

    assert tp.WORKSPACE_AGENT_PROMPT in prompt.system_prompt
    assert tp.KB_TOOL_GUIDANCE not in prompt.system_prompt
    assert "本次未启用 Wiki 检索" in prompt.system_prompt
    assert [kind for kind, _ in prompt.fragments] == ["kb_scope", "base_rules"]


def test_blank_system_prompt_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(tp.settings, "SYSTEM_PROMPT", "   ")

    prompt = _build()

    assert "Hardware DataBase" in prompt.system_prompt
    assert "Hardware DataBase" in tp.WORKSPACE_AGENT_PROMPT


def test_skill_turn_appends_citation_rule_even_with_custom_prompt(monkeypatch):
    monkeypatch.setattr(tp.settings, "SYSTEM_PROMPT", "CUSTOM BASE")

    with_skill = _build(skill_present=True)
    without_skill = _build()

    assert tp.SKILL_CITATION_RULE in with_skill.system_prompt
    assert tp.SKILL_OUTPUT_RULE in with_skill.system_prompt
    assert "Skill 文本是流程参考" in with_skill.system_prompt
    assert "固定列和结构" in with_skill.system_prompt
    assert tp.SKILL_CITATION_RULE not in without_skill.system_prompt
