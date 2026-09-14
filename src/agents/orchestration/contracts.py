"""Stable contracts for planning a governed conversational action.

The model may propose an action plan, but this module owns the validation
boundary that decides whether a plan is eligible to reach a domain adapter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.intent import extract_requested_formats


ActionKind = Literal[
    "search",
    "inspect",
    "compare",
    "summarize",
    "create_document",
    "revise_document",
    "revise_attachment",
    "export_result",
    "get_status",
    "pause",
    "cancel",
    "resume",
    "retry",
    "rebase_sources",
    "request_release",
    "resolve_decision",
]
ConstraintKind = Literal[
    "forbid_action",
    "source_scope",
    "content_requirement",
    "preserve",
    "delivery",
]


class ActionPlanIssue(BaseModel):
    """One server-detected reason an action plan cannot run as proposed."""

    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "unknown_action",
        "unknown_dependency",
        "dependency_cycle",
        "forbidden_action",
        "unsupported_capability",
        "unresolved_reference",
    ]
    action_id: str = ""
    detail: str = ""
    blocking: bool = True


class UserConstraint(BaseModel):
    """One explicit user instruction the executor must enforce."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(min_length=1)
    kind: ConstraintKind
    value: Any
    source_text_span: str = ""
    explicitness: Literal["explicit", "inferred"] = "explicit"


class ActionSpec(BaseModel):
    """A bounded, independently auditable domain action."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    kind: ActionKind
    target_ref: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    completion_contract: str = ""


class ConversationActionPlan(BaseModel):
    """A model-proposed plan that is validated before any domain operation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    plan_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    context_revision: str = ""
    actions: list[ActionSpec] = Field(default_factory=list)
    constraints: list[UserConstraint] = Field(default_factory=list)
    unresolved_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_dependencies(self) -> "ConversationActionPlan":
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action IDs must be unique")
        known = set(action_ids)
        for action in self.actions:
            unknown = set(action.depends_on) - known
            if unknown:
                raise ValueError(f"unknown action dependencies: {sorted(unknown)}")
            if action.action_id in action.depends_on:
                raise ValueError("an action cannot depend on itself")
        return self


class ActionReceipt(BaseModel):
    """Immutable outcome of one validated action attempt."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    status: Literal["accepted", "completed", "blocked", "failed", "skipped", "rejected"]
    result_refs: list[str] = Field(default_factory=list)
    error_code: str = ""
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ActionExecutionResult(BaseModel):
    """Ordered receipts for one action plan execution attempt."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    receipts: list[ActionReceipt] = Field(default_factory=list)


_SEARCH_PATTERN = re.compile(r"查|检索|搜索|分析|比较|对比|读取|提取|search|inspect|compare", re.I)
_CREATE_PATTERN = re.compile(
    r"生成|创建|制作|撰写|起草|做成|整理成|汇总成|编成|汇编成|"
    r"输出.*(?:文档|报告|word|docx)|generate|create|draft|turn into|organize into",
    re.I,
)
_UNSUPPORTED_OUTPUT_FORMAT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:txt|csv|odt|rtf|html?|json|xml)(?![A-Za-z0-9])|"
    r"纯文本|网页文件|文本文件",
    re.I,
)
_NO_RELEASE_PATTERN = re.compile(r"(?:先)?(?:别|不|不要|无需|禁止).{0,8}(?:发布|release)", re.I)
_NO_CREATE_PATTERN = re.compile(
    r"(?:先)?(?:别|不|不要|无需|禁止).{0,8}(?:生成|创建|制作|撰写|起草|输出).{0,8}(?:文档|报告|word|docx)?",
    re.I,
)
_REFDES_PATTERN = re.compile(r"\b([XJ]\d{1,12})\b", re.I)
_INCLUDE_ALL_PATTERN = re.compile(r"(?:全部|所有|都).{0,6}(?:包含|纳入|include)", re.I)
_EXCLUDE_ALL_PATTERN = re.compile(r"(?:全部|所有|都).{0,6}(?:排除|不包含|exclude)", re.I)
_PROCESS_ALL_PATTERN = re.compile(
    r"(?:全部|所有|都).{0,6}(?:处理|解决|按默认|照默认|process|resolve)",
    re.I,
)
_RESUME_PATTERN = re.compile(r"继续(?:生成|执行)?|恢复|resume", re.I)
_CANCEL_PATTERN = re.compile(r"取消|撤销|终止(?:任务|生成|执行)?|cancel", re.I)
_RETRY_PATTERN = re.compile(r"重试|再试一次|重新提交(?:任务|生成|执行)?|retry", re.I)
_STATUS_PATTERN = re.compile(
    r"(?:查看|查询|获取|显示|告诉我|检查).{0,12}(?:文档)?(?:任务|生成)?(?:状态|进度)"
    r"|(?:当前|刚才).{0,12}(?:任务|文档).{0,8}(?:状态|进度)|status",
    re.I,
)
_RELEASE_REQUEST_PATTERN = re.compile(
    r"申请(?:正式)?(?:发布|上线)|请求(?:正式)?发布|提交(?:正式)?发布|request(?:\s+a)?\s+release",
    re.I,
)
_REVISION_PATTERN = re.compile(
    r"修改|修订|调整|更新|替换|改成|改为|换成|revise|modify|update|replace|change",
    re.I,
)
_REVISION_SECTION_PATTERN = re.compile(
    r"第\s*[0-9一二三四五六七八九十百两]+\s*(?:章|节|部分|篇|条|个?章节)"
    r"|(?:chapter|section)\s*[0-9]+",
    re.I,
)
_REVISION_FIELD_PATTERN = re.compile(
    r"(?:字段|field)\s*[:：]?\s*([A-Za-z][A-Za-z0-9_.-]*)",
    re.I,
)
_REVISION_TARGET_PATTERN = re.compile(
    r"第\s*[0-9一二三四五六七八九十百两]+\s*(?:章|节|部分|篇|条|个?章节)"
    r"|(?:chapter|section)\s*[0-9]+|字段|章节|文档|文件|报告|候选|上一版|当前版本|"
    r"artifact|section|field",
    re.I,
)
_ATTACHMENT_TARGET_PATTERN = re.compile(r"附件|attachment", re.I)
_REVISION_QUESTION_PATTERN = re.compile(
    r"是什么|为何|为什么|原因|是否|怎么|如何|吗[？?]?|"
    r"what|why|whether|how",
    re.I,
)
_PRESERVE_UNCHANGED_PATTERN = re.compile(
    r"其余|其他|其它|保留(?:其余|其他|其它)?(?:内容|章节|部分)?不变|"
    r"保持(?:其余|其他|其它)?(?:内容|章节|部分)?不变|keep\s+(?:the\s+)?rest\s+unchanged",
    re.I,
)
_PAUSE_PATTERN = re.compile(r"(?:暂停(?:一下)?|停一下|先停|pause)", re.I)
_CANDIDATE_PATTERN = re.compile(r"候选(?:稿|文档)?|candidate", re.I)
_INCLUDE_PATTERN = re.compile(r"包含|纳入|包括|放进(?:去)?|include", re.I)
_EXCLUDE_PATTERN = re.compile(r"排除|不包含|剔除|exclude|不要(?:了)?", re.I)
_AMBIGUOUS_PIN_REFERENCE_PATTERN = re.compile(
    r"(?:那个|这个|该|这)\s*(?:脚|引脚|管脚|pin)\s*(?:不要|不需要|删掉|移除|排除)?",
    re.I,
)
_DECISION_CLAUSE_SEPARATOR = re.compile(r"[，,；;。！？!?]+")


def _requested_action_format(text: str) -> str:
    """Return one explicit format in the vocabulary accepted by doc tools.

    The canonical intent planner uses ``md`` as its compact wire value while
    the document service calls that format ``markdown``.  Normalize only that
    spelling here; all other explicit formats are passed through so the
    service can reject an unavailable conversion instead of silently falling
    back to DOCX.
    """

    formats = extract_requested_formats(text)
    if len(formats) != 1:
        return ""
    return "markdown" if formats[0] == "md" else formats[0]


def _revision_section_refs(text: str) -> list[str]:
    """Normalize explicit Chinese/English section references for the domain tool."""

    refs: list[str] = []
    for match in _REVISION_SECTION_PATTERN.finditer(text):
        value = re.sub(r"\s+", "", match.group(0)).strip()
        english = re.fullmatch(r"(?:chapter|section)(\d+)", value, re.IGNORECASE)
        if english is not None:
            value = f"第{english.group(1)}章"
        if value and value not in refs:
            refs.append(value)
    return refs


def _source_scope(*, has_attachments: bool, has_kb: bool) -> str:
    if has_attachments and has_kb:
        return "attachment_and_knowledge_base"
    if has_attachments:
        return "attachment_only"
    return "knowledge_base_only"


def _bulk_refdes_decisions(text: str) -> list[tuple[str, list[str]]]:
    """Extract explicit group decisions without needing an LLM.

    A clause is intentionally the unit of interpretation: ``X1900 包含，
    X1902 排除`` must remain two operations, while ``X1900、X1902 都包含``
    is one operation over two groups.  This keeps the resulting audit trail
    faithful to the user's scope rather than flattening contradictory choices.
    """

    decisions: list[tuple[str, list[str]]] = []
    for clause in _DECISION_CLAUSE_SEPARATOR.split(text):
        refs = list(dict.fromkeys(match.upper() for match in _REFDES_PATTERN.findall(clause)))
        if not refs:
            continue
        # Check exclusion first because ``不包含`` includes the include token.
        if _EXCLUDE_ALL_PATTERN.search(clause) or _EXCLUDE_PATTERN.search(clause):
            action = "exclude"
        elif _INCLUDE_ALL_PATTERN.search(clause) or _INCLUDE_PATTERN.search(clause):
            action = "include"
        elif _PROCESS_ALL_PATTERN.search(clause):
            # ``全部处理`` is intentionally not guessed as include/exclude.
            # The adapter resolves ``default`` from the authenticated task
            # projection, preserving the document-type policy decision.
            action = "default"
        else:
            continue
        candidate = (action, refs)
        if candidate not in decisions:
            decisions.append(candidate)
    return decisions


def extract_explicit_constraints(query: str) -> list[UserConstraint]:
    """Extract unambiguous prohibitions independently of a model proposal."""

    text = str(query or "").strip()
    constraints: list[UserConstraint] = []
    if _NO_RELEASE_PATTERN.search(text):
        constraints.append(UserConstraint(
            constraint_id="constraint-forbid-release",
            kind="forbid_action",
            value="release",
            source_text_span=text,
        ))
    if _NO_CREATE_PATTERN.search(text):
        constraints.append(UserConstraint(
            constraint_id="constraint-forbid-create-document",
            kind="forbid_action",
            value="create_document",
            source_text_span=text,
        ))
    return constraints


def compile_deterministic_action_plan(
    query: str,
    *,
    has_attachments: bool = False,
    has_kb: bool = True,
    plan_id: str = "action-plan:deterministic",
    context_revision: str = "",
) -> ConversationActionPlan:
    """Compile explicit read/create language into a safe action DAG.

    This intentionally handles only unambiguous, high-value commands.  A
    model-backed parser may propose richer plans later; unsupported ambiguity
    remains visible through ``unresolved_refs`` instead of becoming a write.
    """

    text = str(query or "").strip()
    if not text:
        raise ValueError("query is required")

    actions: list[ActionSpec] = []
    constraints = extract_explicit_constraints(text)
    forbidden = {
        _canonical_action_name(str(constraint.value))
        for constraint in constraints
        if constraint.kind == "forbid_action"
    }

    # Stopping an in-flight task is intentionally handled before any scope or
    # creation shortcut.  It is a task-bound mutation, followed by a read of
    # the authoritative projection so the chat card can retain/refresh the
    # already-created candidate without issuing a new generation or release.
    if _PAUSE_PATTERN.search(text):
        actions.extend([
            ActionSpec(
                action_id="a1",
                kind="pause",
                required_capabilities=["document.task.control"],
                completion_contract="pause the current document task without deleting existing artifacts",
            ),
            ActionSpec(
                action_id="a2",
                kind="get_status",
                depends_on=["a1"],
                required_capabilities=["document.task.read"],
                completion_contract="return the paused task projection and any downloadable candidate",
            ),
        ])
        return ConversationActionPlan(
            plan_id=plan_id,
            goal=text,
            context_revision=context_revision,
            actions=actions,
            constraints=constraints,
        )

    def _task_control_plan(
        kind: ActionKind,
        capability: str,
        completion_contract: str,
        *,
        refresh_status: bool = True,
    ) -> ConversationActionPlan:
        """Compile a task command without allowing it to become a write shortcut."""

        control = ActionSpec(
            action_id="a1",
            kind=kind,
            required_capabilities=[capability],
            completion_contract=completion_contract,
        )
        actions = [control]
        if refresh_status:
            actions.append(ActionSpec(
                action_id="a2",
                kind="get_status",
                depends_on=[control.action_id],
                required_capabilities=["document.task.read"],
                completion_contract="return the authoritative task projection after the command",
            ))
        return ConversationActionPlan(
            plan_id=plan_id,
            goal=text,
            context_revision=context_revision,
            actions=actions,
            constraints=constraints,
        )

    # Explicit task commands are checked before the generic generation
    # patterns.  In particular, “继续生成” must resume the current task when
    # one exists; it must never create a second document because the fallback
    # parser happened to see the word “生成”.
    if _CANCEL_PATTERN.search(text):
        return _task_control_plan(
            "cancel",
            "document.task.control",
            "cancel only the current authorized document task",
        )
    if _RETRY_PATTERN.search(text):
        return _task_control_plan(
            "retry",
            "document.task.control",
            "retry the current task using its frozen plan and sources",
        )
    if _RELEASE_REQUEST_PATTERN.search(text):
        return _task_control_plan(
            "request_release",
            "document.release.request",
            "record a release-review request without approving or publishing the candidate",
            refresh_status=False,
        )
    if _STATUS_PATTERN.search(text):
        return _task_control_plan(
            "get_status",
            "document.task.read",
            "return the authoritative task projection and candidate state",
            refresh_status=False,
        )

    if (
        has_attachments
        and _REVISION_PATTERN.search(text)
        and _ATTACHMENT_TARGET_PATTERN.search(text)
        and not _REVISION_QUESTION_PATTERN.search(text)
    ):
        # Attachment editing has a deliberately narrower first-version
        # contract than governed Office/document-plan revision.  Only a
        # quoted, exact replacement is executable; all other wording stays a
        # clarification instead of being guessed by the model.
        from src.document_authoring.planning.revision import parse_revision_text_edit

        edit = parse_revision_text_edit(text)
        if edit is None:
            # Not an exact quoted replacement: a section rewrite may only run
            # as a model-proposed, evidence-cited proposal that the user must
            # confirm.  The marker lets the plan parser fall through to the
            # model plan instead of blocking the request outright.
            return ConversationActionPlan(
                plan_id=plan_id,
                goal=text,
                context_revision=context_revision,
                actions=[],
                constraints=constraints,
                unresolved_refs=["attachment_section_proposal"],
            )
        return ConversationActionPlan(
            plan_id=plan_id,
            goal=text,
            context_revision=context_revision,
            actions=[ActionSpec(
                action_id="a1",
                kind="revise_attachment",
                parameters={
                    "request": text,
                    "before": edit.before,
                    "after": edit.after,
                },
                required_capabilities=["attachment.revise"],
                completion_contract=(
                    "create an immutable attachment candidate from one exact text replacement; "
                    "the parent bytes remain unchanged"
                ),
            )],
            constraints=constraints,
        )
    if (
        _REVISION_PATTERN.search(text)
        and _REVISION_TARGET_PATTERN.search(text)
        and not _REVISION_QUESTION_PATTERN.search(text)
    ):
        changed_sections = _revision_section_refs(text)
        changed_fields = list(dict.fromkeys(
            match.group(1).strip()
            for match in _REVISION_FIELD_PATTERN.finditer(text)
            if match.group(1).strip()
        ))
        unresolved_refs: list[str] = []
        if not changed_sections and not changed_fields:
            unresolved_refs.append("revision_target")
        if changed_sections:
            request_type = "section_update"
        elif changed_fields:
            request_type = "field_update"
        else:
            request_type = "section_update"
        revision_constraints = list(constraints)
        if _PRESERVE_UNCHANGED_PATTERN.search(text):
            revision_constraints.append(UserConstraint(
                constraint_id="constraint-preserve-non-target-units",
                kind="preserve",
                value="all_non_target_units",
                source_text_span=text,
            ))
        return ConversationActionPlan(
            plan_id=plan_id,
            goal=text,
            context_revision=context_revision,
            actions=[ActionSpec(
                action_id="a1",
                kind="revise_document",
                parameters={
                    "request_type": request_type,
                    "request": text,
                    "changed_sections": changed_sections,
                    "changed_fields": changed_fields,
                },
                required_capabilities=["document.revise"],
                completion_contract=(
                    "create a new candidate revision with the requested scope changed "
                    "and all unspecified semantic units preserved"
                ),
            )],
            constraints=revision_constraints,
            unresolved_refs=unresolved_refs,
        )

    bulk_decisions = _bulk_refdes_decisions(text)
    if bulk_decisions:
        continue_generation = bool(_RESUME_PATTERN.search(text) or _CANDIDATE_PATTERN.search(text))
        defer_resume_until_all = continue_generation and len(bulk_decisions) > 1
        for index, (bulk_action, refdes) in enumerate(bulk_decisions, start=1):
            parameters: dict[str, Any] = {
                "action": bulk_action,
                "refdes": refdes,
                "scope": "all_pending_for_refdes",
            }
            if defer_resume_until_all:
                parameters["continue_generation"] = False
            actions.append(ActionSpec(
                action_id=f"a{index}",
                kind="resolve_decision",
                parameters=parameters,
                required_capabilities=["document.scope.resolve"],
                completion_contract="expand the group into auditable item decisions",
            ))
        if continue_generation and "create_document" not in forbidden:
            actions.append(ActionSpec(
                action_id=f"a{len(actions) + 1}",
                kind="resume",
                depends_on=[action.action_id for action in actions],
                required_capabilities=["document.task.resume"],
                completion_contract="resume the affected document task once",
            ))
        return ConversationActionPlan(
            plan_id=plan_id,
            goal=text,
            context_revision=context_revision,
            actions=actions,
            constraints=constraints,
        )

    if _RESUME_PATTERN.search(text):
        return _task_control_plan(
            "resume",
            "document.task.resume",
            "resume the current authorized document task once",
        )

    wants_create = bool(_CREATE_PATTERN.search(text)) and "create_document" not in forbidden
    wants_search = bool(_SEARCH_PATTERN.search(text))
    requested_formats = extract_requested_formats(text)
    if wants_create and (
        len(requested_formats) > 1
        or _UNSUPPORTED_OUTPUT_FORMAT_PATTERN.search(text)
    ):
        # Do not let the adapter's historical DOCX default hide an output
        # contract that the user did not actually choose or that the service
        # cannot render.  A later clarification can compile one supported
        # format into a real write action.
        return ConversationActionPlan(
            plan_id=plan_id,
            goal=text,
            context_revision=context_revision,
            actions=[],
            constraints=constraints,
            unresolved_refs=[
                "multiple_output_formats"
                if len(requested_formats) > 1
                else "unsupported_output_format"
            ],
        )
    if wants_search or (wants_create and (has_attachments or has_kb)):
        actions.append(ActionSpec(
            action_id="a1",
            kind="search",
            parameters={"source_scope": _source_scope(
                has_attachments=has_attachments,
                has_kb=has_kb,
            )},
            required_capabilities=["evidence.search"],
            completion_contract="return scoped evidence with source locators",
        ))
    if wants_create:
        depends_on = [actions[-1].action_id] if actions else []
        requested_format = _requested_action_format(text)
        actions.append(ActionSpec(
            action_id=f"a{len(actions) + 1}",
            kind="create_document",
            depends_on=depends_on,
            parameters={
                "format": requested_format or "docx",
                "delivery": "candidate",
            },
            required_capabilities=["document.authoring"],
            completion_contract="create a validated, downloadable candidate artifact",
        ))

    unresolved_refs = ["ambiguous_pin_reference"] if _AMBIGUOUS_PIN_REFERENCE_PATTERN.search(text) else []
    return ConversationActionPlan(
        plan_id=plan_id,
        goal=text,
        context_revision=context_revision,
        actions=actions,
        constraints=constraints,
        unresolved_refs=unresolved_refs,
    )


_ACTION_ALIASES = {
    "generate": "create_document",
    "create": "create_document",
    "revise": "revise_document",
    "modify": "revise_document",
    "export": "export_result",
    "release": "request_release",
}

# These actions can mutate durable state or create an externally consumable
# artifact.  If a plan still contains an unresolved object reference, the
# server must not let a model guess its target, even when the model supplied
# all of the expected capabilities.
MUTATING_ACTION_KINDS = frozenset({
    "create_document",
    "revise_document",
    "revise_attachment",
    "export_result",
    "pause",
    "cancel",
    "resume",
    "retry",
    "rebase_sources",
    "request_release",
    "resolve_decision",
})


def _canonical_action_name(action: str) -> str:
    return _ACTION_ALIASES.get(str(action or "").strip(), str(action or "").strip())


def action_requires_resolved_reference(action: ActionKind | str) -> bool:
    """Return whether an action must not run while a plan is ambiguous."""

    return _canonical_action_name(str(action)) in MUTATING_ACTION_KINDS


def _has_dependency_cycle(actions: Iterable[ActionSpec]) -> bool:
    """Return whether a valid-reference action graph contains a cycle."""

    action_list = list(actions)
    remaining = {action.action_id: set(action.depends_on) for action in action_list}
    while remaining:
        ready = [action_id for action_id, dependencies in remaining.items() if not dependencies]
        if not ready:
            return True
        for action_id in ready:
            remaining.pop(action_id)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return False


def validate_action_plan(
    plan: ConversationActionPlan,
    *,
    available_capabilities: Iterable[str] | None = None,
) -> list[ActionPlanIssue]:
    """Validate a model-proposed plan without executing it.

    ``available_capabilities`` is deliberately supplied by the caller after
    resolving deployment, authorization, source scope, object state, and
    explicit user constraints.  A ``None`` value performs structural and
    constraint validation only, which is useful before a request context is
    available.
    """

    # Imported lazily to keep the contracts module free from an import cycle:
    # the capability resolver consumes ``ActionKind`` from this module.
    from src.agents.orchestration.capabilities import capability_for_action

    issues: list[ActionPlanIssue] = []
    actions = list(plan.actions)
    known_ids = {action.action_id for action in actions}
    forbidden_actions = {
        _canonical_action_name(str(constraint.value))
        for constraint in plan.constraints
        if constraint.kind == "forbid_action"
    }
    available = (
        {str(capability).strip() for capability in available_capabilities if str(capability).strip()}
        if available_capabilities is not None
        else None
    )

    for action in actions:
        action_kind = _canonical_action_name(action.kind)
        capability = capability_for_action(action_kind)
        if not capability:
            issues.append(ActionPlanIssue(
                code="unknown_action",
                action_id=action.action_id,
                detail=f"unsupported action kind: {action.kind}",
            ))
            continue
        unknown_dependencies = sorted(set(action.depends_on) - known_ids)
        if unknown_dependencies:
            issues.append(ActionPlanIssue(
                code="unknown_dependency",
                action_id=action.action_id,
                detail=", ".join(unknown_dependencies),
            ))
        if action_kind in forbidden_actions:
            issues.append(ActionPlanIssue(
                code="forbidden_action",
                action_id=action.action_id,
                detail=f"forbidden by explicit user constraint: {action_kind}",
            ))
        if plan.unresolved_refs and action_requires_resolved_reference(action_kind):
            issues.append(ActionPlanIssue(
                code="unresolved_reference",
                action_id=action.action_id,
                detail="unresolved references: " + ", ".join(
                    str(item).strip() for item in plan.unresolved_refs if str(item).strip()
                ),
            ))
        if available is not None:
            required = {capability, *action.required_capabilities}
            if not required.issubset(available):
                issues.append(ActionPlanIssue(
                    code="unsupported_capability",
                    action_id=action.action_id,
                    detail=", ".join(sorted(required - available)),
                ))

    if _has_dependency_cycle(actions):
        issues.append(ActionPlanIssue(
            code="dependency_cycle",
            detail="action dependencies contain a cycle",
        ))
    return issues


__all__ = [
    "ActionKind",
    "ActionPlanIssue",
    "ActionExecutionResult",
    "ActionReceipt",
    "ActionSpec",
    "ConstraintKind",
    "ConversationActionPlan",
    "MUTATING_ACTION_KINDS",
    "UserConstraint",
    "action_requires_resolved_reference",
    "compile_deterministic_action_plan",
    "extract_explicit_constraints",
    "validate_action_plan",
]
