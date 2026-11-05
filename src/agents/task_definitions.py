"""Optional external task definitions.

The agent is general by default.  A task definition is *optional* metadata
loaded from an external file (the dcode/skill model): it may describe the
goal, the expected deliverable and acceptance checks, but it never
prescribes a rigid workflow.  When no definition is supplied the agent is
free to plan and adapt on its own.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

import src.settings

try:  # PyYAML is already a project dependency; keep a JSON-only fallback.
    import yaml
except Exception:  # pragma: no cover - defensive
    yaml = None  # type: ignore[assignment]


@dataclass(frozen=True)
class TaskDefinition:
    id: str
    title: str = ""
    goal: str = ""
    deliverable: str = ""
    coverage: tuple[str, ...] = ()
    instructions: str = ""
    skill: str = ""
    applies_when: tuple[str, ...] = ()
    source_path: str = ""


def _definition_dir() -> str:
    return str(
        getattr(
            src.settings,
            "TASK_DEFINITION_DIR",
            os.path.join(src.settings.STORAGE_DIR, "task_definitions"),
        )
    )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item or "").strip())
    return ()


def _from_mapping(data: dict[str, Any], *, default_id: str, source_path: str) -> TaskDefinition:
    return TaskDefinition(
        id=str(data.get("id") or default_id).strip(),
        title=str(data.get("title") or "").strip(),
        goal=str(data.get("goal") or "").strip(),
        deliverable=str(data.get("deliverable") or "").strip(),
        coverage=_as_tuple(data.get("coverage")),
        instructions=str(data.get("instructions") or "").strip(),
        skill=str(data.get("skill") or "").strip(),
        applies_when=_as_tuple(data.get("applies_when")),
        source_path=source_path,
    )


def _parse_front_matter(text: str) -> dict[str, Any] | None:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    end = stripped.find("\n---", 3)
    if end < 0:
        return None
    block = stripped[3:end].strip()
    if yaml is None:
        return None
    try:
        data = yaml.safe_load(block) or {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_definitions() -> dict[str, TaskDefinition]:
    """Load external definitions; a missing directory simply means none."""

    root = _definition_dir()
    if not os.path.isdir(root):
        return {}
    definitions: dict[str, TaskDefinition] = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        stem, suffix = os.path.splitext(name)
        suffix = suffix.lower()
        data: dict[str, Any] | None = None
        try:
            if suffix in {".yaml", ".yml"} and yaml is not None:
                with open(path, "r", encoding="utf-8") as handle:
                    parsed = yaml.safe_load(handle) or {}
                data = parsed if isinstance(parsed, dict) else None
            elif suffix == ".json":
                with open(path, "r", encoding="utf-8") as handle:
                    parsed = json.load(handle)
                data = parsed if isinstance(parsed, dict) else None
            elif suffix == ".md":
                with open(path, "r", encoding="utf-8") as handle:
                    data = _parse_front_matter(handle.read())
        except OSError:
            continue
        if not data:
            continue
        definition = _from_mapping(data, default_id=stem, source_path=path)
        if definition.id:
            definitions[definition.id] = definition
    return definitions


def resolve_definition(definition_id: str | None) -> TaskDefinition | None:
    """Return the external definition when requested; ``None`` means freeform."""

    key = str(definition_id or "").strip()
    if not key:
        return None
    return load_definitions().get(key)


def build_task_prompt(
    task: Any,
    definition: TaskDefinition | None = None,
    artifacts: Iterable[Any] = (),
) -> str:
    """Prompt fragment for a durable task attempt.

    Deliberately contains no fixed steps or domain workflow: the agent plans.
    """

    artifact_lines = [
        f"  - {getattr(item, 'name', '')}（{getattr(item, 'kind', '')}）"
        for item in list(artifacts or ())
    ] or ["  （暂无）"]
    kb_name = str(getattr(task, "kb_name", "") or "")
    kb_mounted = bool(kb_name) and kb_name != "__general__"
    lines = [
        "\n\n## 工作区模式（通用 agent 会话）",
        "你是一个通用 agent，正在一个持久工作区里完成用户的目标；不是单轮问答。",
        "工作区规则：",
        "  - workspace_* 工具用于内部草稿（读写/编辑/查找）；用户看不到工作区路径。",
        "  - 最终交付物用 task_publish_artifact(name, content, kind) 发布；",
        "    发布后会出现在当前会话，用户可直接预览/下载。",
        "  - 不要在回答里提 out/、磁盘路径或下载 URL；只描述产物名称和内容摘要。",
    ]
    if kb_mounted:
        lines.extend(
            [
                "知识库规则（本项目已挂载知识库，它是项目事实的一等公民）：",
                "  - 涉及项目事实（型号、位号、网络、参数、需求、测试结果、用量）时，"
                "知识库是权威来源；先用知识库查证，并在结论中标注证据编号。",
                "  - 工作区文件、计算和脚本用于加工与交付，不能替代知识库证据，"
                "也不能用来编造项目事实。",
                "  - 知识库确实查不到时写“待确认”，并说明已查来源/关键词。",
            ]
        )
    else:
        lines.append(
            "  - 本轮未挂载知识库：基于工作区文件和可用工具完成任务，事实性结论注明来源或标待确认。"
        )
    lines.extend(
        [
            "  - 如果本轮只是简单问答、不需要交付物，直接回答即可，不要为了走流程而写文件。",
        "执行方式：",
            "  - 自行规划步骤并根据证据调整；不要套用固定流程。",
            "  - 先查再问：知识库里能查到的不要向用户索要；确实缺的写“待确认”。",
            "  - 本轮未完成时说明已完成/还差什么；下一轮会接着做。",
            "  - 交付物发布后，在回答里给出简短摘要（不写路径）。",
        "  - 文件读写/交付物必须由你直接调用 workspace_* 完成；不要把文件操作委派给 research subagent（它只有检索工具）。",
        "  - 工具返回错误时引用具体错误信息；没有调用工具就不要声称工具失败。",
        ]
    )
    if definition is not None:
        if definition.title:
            lines.append(f"任务名称：{definition.title}")
        if definition.goal:
            lines.append(f"任务目标：{definition.goal}")
        if definition.deliverable:
            lines.append(f"期望交付物：{definition.deliverable}")
        if definition.coverage:
            lines.append("验收关注点：")
            lines.extend(f"  - {item}" for item in definition.coverage)
        if definition.skill:
            lines.append(
                f"相关 skill：{definition.skill}（如已物化，可用 read_file 读取其 SKILL.md 作为方法参考，但不要被其步骤绑死）。"
            )
        if definition.instructions:
            lines.append(f"补充要求：{definition.instructions}")
    lines.append("已有产物：")
    lines.extend(artifact_lines)
    return "\n".join(lines)


__all__ = [
    "TaskDefinition",
    "load_definitions",
    "resolve_definition",
    "build_task_prompt",
]
