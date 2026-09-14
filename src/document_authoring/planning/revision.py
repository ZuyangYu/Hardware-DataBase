"""Revision patch compilation for plan-bound document revisions.

The revision boundary turns a natural-language change into a small,
server-owned execution input: a strict patch, a child ``DocumentPlan`` whose
affected semantic units carry the instruction, and a deterministic diff of
the affected scope.  The revision record and work-order binding remain in
``document_authoring.revisions`` and ``document_authoring.service``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .diff import compile_affected_subgraph, diff_document_plans
from .models import (
    AffectedSubgraph,
    DocumentPlan,
    OutputSpec,
    PlanDiff,
    planning_content_hash,
)

RevisionOperation = Literal["field_update", "section_update", "full_regeneration"]


class RevisionNotExecutable(ValueError):
    """Raised when a revision cannot be compiled into executable plan input."""


_ORDINAL_PATTERN = re.compile(
    r"第\s*([0-9一二三四五六七八九十百两]+)\s*(?:章|节|部分|篇|条|个?章节)",
    re.IGNORECASE,
)
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_STRUCTURE_CHANGE_PATTERN = re.compile(
    r"(?:表格|列表|清单|table|list)\s*化|"
    r"(?:改成|改为|换成|变成|转成)[^，。；!？,.;!?]{0,12}?(?:表格|列表|清单|table|list)",
    re.IGNORECASE,
)
_VISUAL_PRESERVE_PATTERN = re.compile(
    r"分页|版式|排版|页码|视觉|页面布局|layout|pagination|page\s*break",
    re.IGNORECASE,
)
_TEXT_REPLACEMENT_PATTERNS = (
    re.compile(
        r"(?:将|把)\s*(?:[^\"“「『]{0,80}?)?[\"“「『](?P<before>.+?)[\"”」』]\s*"
        r"(?:替换为|替换成|改为|改成|换成)\s*"
        r"[\"“「『](?P<after>.+?)[\"”」』]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breplace\s+[\"‘](?P<before>.+?)[\"’]\s+"
        r"(?:with|by)\s+[\"‘](?P<after>.+?)[\"’]",
        re.IGNORECASE,
    ),
)


class RevisionTextEdit(BaseModel):
    """A narrow, exact text edit that the server can execute deterministically."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["replace_text"] = "replace_text"
    before: str = Field(min_length=1, max_length=2_000)
    after: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_change(self) -> "RevisionTextEdit":
        self.before = self.before.strip()
        self.after = self.after.strip()
        if not self.before or not self.after:
            raise ValueError("text replacement requires non-empty before and after values")
        if self.before == self.after:
            raise ValueError("text replacement must change the value")
        return self


def parse_revision_text_edit(instruction: str) -> RevisionTextEdit | None:
    """Parse only unambiguous quoted replacement requests.

    Other natural-language edits remain available to a managed model writer,
    but are not silently interpreted by the deterministic provider.
    """
    text = str(instruction or "").strip()
    if not text:
        return None
    for pattern in _TEXT_REPLACEMENT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        try:
            return RevisionTextEdit(
                before=match.group("before"),
                after=match.group("after"),
            )
        except ValueError:
            return None
    return None


class RevisionPatch(BaseModel):
    """Strict, serializable description of one executable revision."""

    model_config = ConfigDict(extra="forbid")

    patch_id: str
    revision_id: str
    parent_artifact_id: str
    parent_artifact_content_hash: str = ""
    parent_plan_id: str
    parent_plan_version: int = Field(ge=1)
    parent_plan_hash: str
    operation: RevisionOperation
    instruction: str = Field(min_length=1, max_length=4000)
    target_unit_ids: list[str] = Field(default_factory=list, max_length=512)
    changed_fields: list[str] = Field(default_factory=list, max_length=512)
    changed_sections: list[str] = Field(default_factory=list, max_length=512)
    preserve_unit_ids: list[str] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def validate_patch(self) -> "RevisionPatch":
        for name in (
            "patch_id", "revision_id", "parent_artifact_id",
            "parent_plan_id", "parent_plan_hash", "instruction",
        ):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"revision patch {name} is required")
            setattr(self, name, value)
        if self.operation == "field_update" and not self.changed_fields and not self.target_unit_ids:
            raise ValueError("field_update requires changed_fields or target_unit_ids")
        if self.operation == "section_update" and not self.changed_sections and not self.target_unit_ids:
            raise ValueError("section_update requires changed_sections or target_unit_ids")
        for name in ("target_unit_ids", "changed_fields", "changed_sections", "preserve_unit_ids"):
            normalized = [str(item).strip() for item in getattr(self, name)]
            if any(not item for item in normalized):
                raise ValueError(f"revision patch {name} entries must be non-empty")
            setattr(self, name, normalized)
        return self


class RevisionPlanCompilation(BaseModel):
    """Child plan plus the diff/scope that proves what actually changed."""

    model_config = ConfigDict(extra="forbid")

    patch_id: str
    child_plan: DocumentPlan
    plan_diff: PlanDiff
    affected_scope: AffectedSubgraph
    target_unit_ids: list[str]
    preserved_unit_ids: list[str]


def make_revision_patch_id(
    *,
    parent_artifact_id: str,
    parent_artifact_content_hash: str,
    parent_plan_id: str,
    parent_plan_version: int,
    parent_plan_hash: str,
    operation: str,
    instruction: str,
    changed_fields: list[str] | None = None,
    changed_sections: list[str] | None = None,
) -> str:
    """Deterministic patch identity so retries reuse one child plan."""

    return "revision-patch:" + planning_content_hash({
        "parent_artifact_id": str(parent_artifact_id),
        "parent_artifact_content_hash": str(parent_artifact_content_hash),
        "parent_plan_id": str(parent_plan_id),
        "parent_plan_version": int(parent_plan_version),
        "parent_plan_hash": str(parent_plan_hash),
        "operation": str(operation),
        "instruction": str(instruction).strip(),
        "changed_fields": [str(item).strip() for item in (changed_fields or [])],
        "changed_sections": [str(item).strip() for item in (changed_sections or [])],
    })


def _normalize_unit_id(value: str) -> str:
    text = str(value or "").strip()
    for prefix in ("field:", "review:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _chinese_number(raw: str) -> int | None:
    if not raw:
        return None
    if "十" not in raw:
        total = 0
        for char in raw:
            digit = _CN_DIGITS.get(char)
            if digit is None:
                return None
            total = total * 10 + digit
        return total or None
    parts = raw.split("十", 1)
    tens = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
    ones = _CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
    return tens * 10 + ones


def _ordinal_index(text: str) -> int | None:
    match = _ORDINAL_PATTERN.search(str(text or ""))
    if match is None:
        return None
    raw = match.group(1)
    index = int(raw) if raw.isdigit() else _chinese_number(raw)
    return index if index and index > 0 else None


def _outline_title_by_unit(spec: OutputSpec | None) -> dict[str, str]:
    if spec is None:
        return {}
    return {
        str(unit.unit_id).strip(): str(getattr(unit, "title", "") or "").strip()
        for unit in spec.outline
    }


def _resolve_one_token(
    token: str,
    *,
    plan: DocumentPlan,
    spec: OutputSpec | None,
) -> list[str]:
    text = str(token or "").strip()
    if not text:
        return []
    normalized = _normalize_unit_id(text)
    unit_by_normalized = {
        _normalize_unit_id(unit.unit_id): unit.unit_id
        for unit in plan.semantic_units
    }
    if normalized in unit_by_normalized:
        return [unit_by_normalized[normalized]]
    label_matches = [
        unit.unit_id for unit in plan.semantic_units
        if _normalize_unit_id(str(unit.output_schema.get("label") or "")) == normalized
    ]
    if label_matches:
        return label_matches
    titles = _outline_title_by_unit(spec)
    title_matches = [
        unit_id for unit_id, title in titles.items()
        if title and _normalize_unit_id(title) == normalized
    ]
    if title_matches:
        return title_matches
    # Fuzzy containment is restricted to meaningful text tokens.  It avoids
    # guessing from one-character labels while still accepting natural prose.
    if len(text) >= 2:
        contained = [
            unit_id for unit_id, title in titles.items()
            if title and (text in title or title in text)
        ]
        if contained:
            return contained
    index = _ordinal_index(text)
    if index is not None and spec is not None:
        outline = list(spec.outline)
        if 1 <= index <= len(outline):
            return [str(outline[index - 1].unit_id).strip()]
    return []


def resolve_revision_targets(
    plan: DocumentPlan,
    spec: OutputSpec | None,
    patch: RevisionPatch,
) -> list[str]:
    """Resolve patch targets to stable plan unit ids, failing closed."""

    if patch.operation == "full_regeneration":
        return [unit.unit_id for unit in plan.semantic_units]
    tokens = (
        [*patch.target_unit_ids, *patch.changed_fields]
        if patch.operation == "field_update"
        else [*patch.target_unit_ids, *patch.changed_sections]
    )
    resolved: list[str] = []
    unresolved: list[str] = []
    for token in tokens:
        matches = _resolve_one_token(token, plan=plan, spec=spec)
        if not matches:
            unresolved.append(str(token))
            continue
        for unit_id in matches:
            if unit_id not in resolved:
                resolved.append(unit_id)
    if unresolved:
        raise RevisionNotExecutable(
            "无法把修订范围绑定到稳定单元: " + ", ".join(unresolved)
        )
    if not resolved:
        raise RevisionNotExecutable("修订请求没有可执行的单元范围")
    return resolved


def compile_revision_plan(
    plan: DocumentPlan,
    spec: OutputSpec | None,
    patch: RevisionPatch,
) -> RevisionPlanCompilation:
    """Compile a child plan whose affected units carry the revision input."""

    if not isinstance(plan, DocumentPlan):
        plan = DocumentPlan.model_validate(plan)
    if plan.status != "accepted":
        raise RevisionNotExecutable("父文档计划不是已接受状态，不能冻结修订基线")
    if plan.plan_hash != patch.parent_plan_hash:
        raise RevisionNotExecutable("修订基线计划 hash 与当前计划不一致")
    target_unit_ids = resolve_revision_targets(plan, spec, patch)
    if _STRUCTURE_CHANGE_PATTERN.search(patch.instruction):
        # Structural changes alter coverage/layout contracts.  This bounded
        # slice only supports content changes on already registered units.
        unsupported = [
            unit.unit_id for unit in plan.semantic_units
            if unit.unit_id in set(target_unit_ids) and unit.kind not in {"table", "list"}
        ]
        if unsupported:
            raise RevisionNotExecutable(
                "结构变更修订暂不支持（目标单元: "
                + ", ".join(unsupported)
                + "）；请转为新建文档或拆分章节"
            )
    if _VISUAL_PRESERVE_PATTERN.search(patch.instruction):
        raise RevisionNotExecutable(
            "视觉/版式保留能力未开放；内容修订不承诺分页与版式不变，"
            "请转为新建文档或走独立排版流程"
        )
    child_payload: dict[str, Any] = plan.model_dump(mode="json")
    child_payload.pop("plan_hash", None)
    child_payload["document_plan_id"] = f"{plan.document_plan_id}:{patch.patch_id}"
    child_payload["version"] = plan.version + 1
    child_payload["status"] = "proposed"
    instruction = patch.instruction.strip()
    target_set = set(target_unit_ids)
    text_edit = parse_revision_text_edit(instruction)
    for unit in child_payload.get("semantic_units") or []:
        if str(unit.get("unit_id")) not in target_set:
            continue
        output_schema = dict(unit.get("output_schema") or {})
        output_schema["revision_instruction"] = instruction
        output_schema["revision_id"] = patch.revision_id
        output_schema["revision_operation"] = patch.operation
        if text_edit is not None:
            output_schema["revision_edit"] = text_edit.model_dump(mode="json")
        unit["output_schema"] = output_schema
    for task in child_payload.get("unit_tasks") or []:
        task["plan_version"] = child_payload["version"]
        task_unit_id = str(task.get("unit_id") or "")
        # Child receipts must be plan-scoped, so a parent receipt cannot make
        # the revised unit appear complete without executing the child input.
        task["action_key"] = (
            f"document-plan:{child_payload['document_plan_id']}:"
            f"{task_unit_id}:v{child_payload['version']}"
        )
    child_plan = DocumentPlan.model_validate(child_payload)
    plan_diff = diff_document_plans(plan, child_plan)
    affected_scope = compile_affected_subgraph(plan, child_plan, plan_diff)
    preserved = [
        unit.unit_id for unit in plan.semantic_units
        if unit.unit_id not in target_set
    ]
    return RevisionPlanCompilation(
        patch_id=patch.patch_id,
        child_plan=child_plan,
        plan_diff=plan_diff,
        affected_scope=affected_scope,
        target_unit_ids=target_unit_ids,
        preserved_unit_ids=preserved,
    )


__all__ = [
    "RevisionNotExecutable",
    "RevisionOperation",
    "RevisionPatch",
    "RevisionTextEdit",
    "RevisionPlanCompilation",
    "compile_revision_plan",
    "make_revision_patch_id",
    "parse_revision_text_edit",
    "resolve_revision_targets",
]
