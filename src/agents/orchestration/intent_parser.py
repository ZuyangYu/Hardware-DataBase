"""Strict model-proposed action-plan parsing with a safe deterministic fallback."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from src.agents.orchestration.contracts import (
    ConversationActionPlan,
    compile_deterministic_action_plan,
    extract_explicit_constraints,
    validate_action_plan,
)


_JSON_FENCE = re.compile(r"^```(?:json)?\s*(\{[\s\S]*\})\s*```$", re.IGNORECASE)
_ACTION_ENUM = (
    "search, inspect, compare, summarize, create_document, revise_document, revise_attachment, "
    "export_result, get_status, pause, cancel, resume, retry, rebase_sources, "
    "request_release, resolve_decision"
)
_PARSER_SYSTEM_PROMPT = f"""你是受控对话任务的动作计划解析器。只输出一个 JSON 对象，不能输出 Markdown 或解释。
模型只可以提议计划；服务端将独立校验权限、来源范围、版本和用户禁止约束。
JSON 结构：
{{
  "plan_id": "string",
  "goal": "string",
  "context_revision": "string",
  "actions": [{{
    "action_id": "string",
    "kind": "one of: {_ACTION_ENUM}",
    "target_ref": "string or null",
    "input_refs": ["trusted or user-visible reference"],
    "depends_on": ["action_id"],
    "parameters": {{}},
    "required_capabilities": ["capability"],
    "completion_contract": "string"
  }}],
  "constraints": [{{
    "constraint_id": "string",
    "kind": "forbid_action | source_scope | content_requirement | preserve | delivery",
    "value": "any",
    "source_text_span": "用户原文片段",
    "explicitness": "explicit | inferred"
  }}],
  "unresolved_refs": ["ambiguous object reference"]
}}
规则：保留复合动作的依赖顺序、来源范围、附件/文件/章节/位号引用和保留要求；
不确定的写入对象放到 unresolved_refs，不得猜测；用户明确禁止的动作不得出现在 actions。
revise_attachment 的两种参数形态：
- 引号精确替换：parameters = {{"before": "原文", "after": "新文"}}（服务端仍会重新解析用户原话）。
- 章节提案（不会直接写入，需用户确认）：parameters = {{
    "operation": "replace_section",
    "heading_path": ["章节标题"],
    "after": "新的整节正文（可多行）",
    "evidence_refs": ["本轮证据编号 [n] 或 evidence id"],
    "attachment_id": "目标附件 id"
  }}；必须至少引用一条本轮证据，否则服务端会拒绝。"""


def build_action_plan_messages(
    query: str,
    *,
    context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build a prompt whose context is explicitly identified as server-owned."""

    parts = [f"用户当前消息：{str(query or '').strip()}"]
    if context:
        parts.append("可信服务端上下文：" + json.dumps(context, ensure_ascii=False, sort_keys=True))
    parts.append("输出 JSON：")
    return [
        {"role": "system", "content": _PARSER_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def _json_payload(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    fence = _JSON_FENCE.fullmatch(text)
    if fence:
        text = fence.group(1)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_action_plan_payload(raw: str) -> ConversationActionPlan | None:
    """Parse exactly one strict JSON plan; reject prose and extra fields."""

    payload = _json_payload(raw)
    if payload is None:
        return None
    try:
        return ConversationActionPlan.model_validate(payload)
    except ValueError:
        return None


def _merge_explicit_constraints(
    plan: ConversationActionPlan,
    query: str,
) -> ConversationActionPlan:
    """Ensure a model cannot omit a prohibition expressed in the user turn."""

    existing = {constraint.constraint_id for constraint in plan.constraints}
    constraints = [*plan.constraints]
    constraints.extend(
        constraint
        for constraint in extract_explicit_constraints(query)
        if constraint.constraint_id not in existing
    )
    return plan.model_copy(update={"constraints": constraints})


class StructuredActionPlanParser:
    """Use a model only as a candidate planner; fail closed before execution."""

    def __init__(self, complete: Callable[[list[dict[str, str]]], str] | None = None) -> None:
        self._complete = complete

    def parse(
        self,
        query: str,
        *,
        context: dict[str, Any] | None = None,
        available_capabilities: Iterable[str] | None = None,
        fallback: bool = True,
    ) -> ConversationActionPlan | None:
        candidate: ConversationActionPlan | None = None
        if self._complete is not None:
            try:
                candidate = parse_action_plan_payload(
                    self._complete(build_action_plan_messages(query, context=context)),
                )
            except Exception:
                candidate = None
        if candidate is not None:
            candidate = _merge_explicit_constraints(candidate, query)
            expected_revision = str((context or {}).get("context_revision", ""))
            revision_matches = not expected_revision or candidate.context_revision in {
                "", expected_revision,
            }
            issues = validate_action_plan(
                candidate,
                available_capabilities=available_capabilities,
            )
            # Deterministic high-risk guards are authoritative even when the
            # model emitted valid JSON and listed the right capability.  In
            # particular, an explicit multi-format/unsupported-format request
            # must not become a DOCX write merely because the model guessed a
            # default.  Other valid model plans may still use richer actions.
            deterministic_guard = compile_deterministic_action_plan(
                query,
                has_attachments=bool((context or {}).get("has_attachments", False)),
                has_kb=bool((context or {}).get("has_kb", True)),
                context_revision=str((context or {}).get("context_revision", "")),
            )
            section_proposal_only = (
                deterministic_guard.unresolved_refs
                and set(deterministic_guard.unresolved_refs) == {"attachment_section_proposal"}
            )
            if deterministic_guard.unresolved_refs and not section_proposal_only:
                return deterministic_guard
            if any(action.kind == "revise_attachment" for action in deterministic_guard.actions):
                return deterministic_guard
            if revision_matches and not any(issue.blocking for issue in issues):
                return candidate
        if not fallback:
            return None
        return compile_deterministic_action_plan(
            query,
            has_attachments=bool((context or {}).get("has_attachments", False)),
            has_kb=bool((context or {}).get("has_kb", True)),
            context_revision=str((context or {}).get("context_revision", "")),
        )


__all__ = [
    "StructuredActionPlanParser",
    "build_action_plan_messages",
    "parse_action_plan_payload",
]
