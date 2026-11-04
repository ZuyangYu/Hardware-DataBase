"""Optional external task definitions: no hard-coded workflow."""

from __future__ import annotations

from types import SimpleNamespace

import src.settings
from src.agents.task_definitions import (
    build_task_prompt,
    load_definitions,
    resolve_definition,
)


def test_missing_definition_means_freeform(monkeypatch, tmp_path):
    monkeypatch.setattr(src.settings, "TASK_DEFINITION_DIR", str(tmp_path / "none"))
    assert load_definitions() == {}
    assert resolve_definition(None) is None
    assert resolve_definition("component_list") is None

    prompt = build_task_prompt(SimpleNamespace(kb_name="__general__"), None, [])
    assert "工作区模式" in prompt
    # The generic prompt must let the agent plan; no fixed steps are injected.
    assert "自行规划" in prompt
    assert "固定流程" in prompt
    assert "未挂载知识库" in prompt


def test_mounted_kb_is_first_class_for_project_facts():
    prompt = build_task_prompt(SimpleNamespace(kb_name="AAA"), None, [])
    assert "一等公民" in prompt
    assert "权威来源" in prompt
    assert "不能替代知识库证据" in prompt


def test_external_yaml_definition_is_loaded(monkeypatch, tmp_path):
    root = tmp_path / "defs"
    root.mkdir()
    (root / "component_list.yaml").write_text(
        "\n".join(
            [
                "id: component_list",
                "title: 关键器件清单",
                "goal: 产出可追溯的清单",
                "deliverable: out/component_list.md",
                "coverage:",
                "  - 每行有证据",
                "  - 缺失写待确认",
                "skill: skill-key-device-list",
                "applies_when: [清单, BOM]",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(src.settings, "TASK_DEFINITION_DIR", str(root))

    definitions = load_definitions()
    assert "component_list" in definitions
    definition = resolve_definition("component_list")
    assert definition is not None
    assert definition.goal == "产出可追溯的清单"
    assert definition.coverage == ("每行有证据", "缺失写待确认")
    assert definition.skill == "skill-key-device-list"

    prompt = build_task_prompt(object(), definition, [])
    assert "产出可追溯的清单" in prompt
    assert "每行有证据" in prompt
    assert "不要套用固定流程" in prompt


def test_markdown_front_matter_definition(monkeypatch, tmp_path):
    root = tmp_path / "defs"
    root.mkdir()
    (root / "review.md").write_text(
        "\n".join(
            [
                "---",
                "id: review",
                "title: 审阅",
                "goal: 审阅并给出结论",
                "---",
                "# 审阅",
                "这里是可选的方法说明。",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(src.settings, "TASK_DEFINITION_DIR", str(root))
    definition = resolve_definition("review")
    assert definition is not None
    assert definition.title == "审阅"
