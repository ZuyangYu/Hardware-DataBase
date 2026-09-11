"""Controlled chat tools for the document-authoring workflow.

These tools are intentionally a thin, permission-checked adapter around
``AppPipeline``.  The typed methods are used by tests/coordinators; the
LangChain ``StructuredTool`` wrappers serialize the result only at the outer
ToolMessage boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import src.settings
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from src.document_authoring.chat_context import DocumentAuthoringContext, DocumentContext
from src.document_authoring.icd_scope_decision import ICD_BLOCKING_SCOPE_EXCEPTION_KINDS
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.planning.intake import OutputSpecIntakeService


def _v2_enabled() -> bool:
    return bool(getattr(src.settings, "DOCUMENT_PLANNING_V2_ENABLED", False))


def _plan_confirmation_reasons(proposal: Any, session: Any) -> list[str]:
    """Return deterministic reasons that require a visible Gate 1 confirmation."""
    if not isinstance(proposal, dict):
        return ["proposal_unavailable"]
    reasons: list[str] = []
    if not bool(proposal.get("executable")):
        reasons.append("proposal_not_executable")
    if proposal.get("blockers"):
        reasons.append("proposal_has_blockers")
    if proposal.get("warnings"):
        reasons.append("proposal_has_warnings")
    deliverables = proposal.get("deliverables")
    if not isinstance(deliverables, list) or sum(
        1
        for item in deliverables
        if isinstance(item, dict) and item.get("role") == "primary"
    ) != 1:
        reasons.append("primary_deliverable_ambiguous")
    draft = getattr(session, "output_spec_draft", None)
    draft = draft if isinstance(draft, dict) else {}
    if str(draft.get("inference_policy") or "").strip() != "forbid":
        reasons.append("inference_requires_confirmation")
    source_summary = proposal.get("source_summary")
    source_summary = source_summary if isinstance(source_summary, dict) else {}
    source_count = sum(
        int(source_summary.get(key) or 0)
        for key in ("knowledge_base_count", "attachment_count", "project_count")
    )
    if source_count < 1:
        reasons.append("source_scope_unfixed")
    layout = proposal.get("layout_summary")
    layout = layout if isinstance(layout, dict) else {}
    if str(layout.get("mode") or "") not in {"provided_template", "generated_structure"}:
        reasons.append("layout_change_requires_confirmation")
    return list(dict.fromkeys(reasons))


class DocumentToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "rejected", "unavailable", "waiting_human"]
    operation: str
    error_code: str | None = None
    message: str = ""
    analysis_id: str | None = None
    template_version_id: str | None = None
    generation_session_id: str | None = None
    task_id: str | None = None
    work_order_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    next_actions: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class GetAnalysisArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(min_length=1, max_length=200)


class StartSessionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_version_id: str | None = Field(default=None, max_length=200)
    purpose: str = Field(default="", max_length=4000)


class AnswerClarificationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=200)
    question_id: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=4000)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)


class ConfirmSessionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=200)


class CreateWorkOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_version_id: str | None = Field(default=None, max_length=200)
    # Schema identity is owned by the activated template.  The fields remain
    # optional for backwards compatibility with older callers, but the
    # service derives and validates them before a work order is created.
    document_schema_id: str | None = Field(default=None, max_length=200)
    document_schema_version: str | None = Field(default=None, max_length=100)
    generation_session_id: str | None = Field(default=None, max_length=200)
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] = "internal_harness"


class GenerateDocumentArgs(BaseModel):
    """Arguments for the one-shot, template-backed chat generation path."""

    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(default="", max_length=4000)
    template_version_id: str | None = Field(default=None, max_length=200)
    generation_session_id: str | None = Field(default=None, max_length=200)
    document_schema_id: str | None = Field(default=None, max_length=200)
    document_schema_version: str | None = Field(default=None, max_length=100)
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] = "internal_harness"
    use_recommended_defaults: bool = True
    # ``native`` keeps the immutable template format.  PDF/PPTX are explicit
    # semantic conversion targets; unsupported cross-format requests remain
    # fail-closed instead of falling back to a generic Markdown export.
    output_format: Literal["native", "xlsm", "xlsx", "docx", "markdown", "pdf", "pptx"] = "native"


class GetStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_order_id: str = Field(min_length=1, max_length=200)


class ProposePlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=200)


class ConfirmPlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=200)
    expected_output_spec_hash: str = Field(min_length=1, max_length=256)
    expected_plan_hash: str = Field(min_length=1, max_length=256)
    client_request_id: str = Field(min_length=1, max_length=128)


class TaskStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=200)


class ResolveIcdScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["include", "exclude", "use_edf_connectors"] = Field(
        description=(
            "包含/排除待处理的 ICD 范围异常项；use_edf_connectors 表示用户确认"
            "用冻结 EDF 的实际位号替换模板示例位号并继续生成。"
        ),
    )
    comment: str = Field(min_length=1, max_length=2000)
    exception_ids: list[str] = Field(default_factory=list, max_length=50)
    refdes: list[str] = Field(default_factory=list, max_length=50)
    work_order_id: str | None = Field(default=None, max_length=200)


class CreateDocumentRevisionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=200)
    parent_artifact_id: str = Field(min_length=1, max_length=200)
    request_type: Literal["field_update", "section_update", "full_regeneration"]
    request: str = Field(min_length=1, max_length=4000)
    changed_fields: list[str] = Field(default_factory=list)
    changed_sections: list[str] = Field(default_factory=list)
    client_request_id: str = Field(min_length=1, max_length=128)


def _safe_analysis(analysis: Any) -> dict[str, Any]:
    """Project analysis to metadata useful to a chat agent, not file paths."""

    return {
        "analysis_id": analysis.analysis_id,
        "template_version_id": analysis.template_version_id,
        "format": analysis.format,
        "status": analysis.status,
        "units": [
            {
                "unit_id": unit.unit_id,
                "label": unit.label,
                "writable": unit.writable,
                "blocked_reason": unit.blocked_reason,
                "structural_role_hint": unit.structural_role_hint,
            }
            for unit in analysis.units
        ],
        "suggestions": [
            {
                "semantic_unit_id": suggestion.semantic_unit_id,
                "label": suggestion.label,
                "confidence": suggestion.confidence,
                "value_shape": suggestion.value_shape,
                "target_unit_ids": list(suggestion.target_unit_ids),
            }
            for suggestion in analysis.suggestions
        ],
        "reason_codes": list(
            getattr(getattr(analysis, "activation_decision", None), "reason_codes", []) or []
        ),
    }


def _pending_question_view(session: Any) -> dict[str, Any] | None:
    """Project the next unanswered intake question with its canonical options.

    The model must relay the server-published options verbatim; without this
    projection it invents its own option labels that the intake then rejects.
    """

    if str(getattr(session, "contract_version", "") or "") != "output_spec_v1":
        return None
    answered: set[str] = set()
    for message in reversed(getattr(session, "messages", None) or []):
        question_id = str(getattr(message, "question_id", "") or "").strip()
        if getattr(message, "role", "") == "user":
            if question_id and (
                str(getattr(message, "answer", "") or "").strip()
                or str(getattr(message, "content", "") or "").strip()
            ):
                answered.add(question_id)
            continue
        if (
            question_id
            and str(getattr(message, "content", "") or "").strip()
            and question_id not in answered
        ):
            return {
                "question_id": question_id,
                "content": str(message.content),
                "options": [str(item) for item in (getattr(message, "options", None) or [])],
                "reason": str(getattr(message, "reason", "") or ""),
            }
    return None


def _safe_session(session: Any) -> dict[str, Any]:
    result = {
        "session_id": session.session_id,
        "status": session.status,
        "template_version_id": session.template_version_id,
        "knowledge_base_name": session.knowledge_base_name,
        "document_task_id": getattr(session, "document_task_id", None),
        "conversation_id": getattr(session, "conversation_id", None),
        "initiating_turn_id": getattr(session, "initiating_turn_id", None),
        "last_question_id": getattr(session, "last_question_id", None),
        "clarification_revision": getattr(session, "clarification_revision", 0),
        "work_order_id": session.work_order_id,
        "brief": session.brief.model_dump(mode="json"),
        "next_actions": _session_next_actions(session),
    }
    if getattr(session, "contract_version", "legacy_brief_v1") == "output_spec_v1":
        result.update({
            "contract_version": "output_spec_v1",
            "output_spec_id": getattr(session, "output_spec_id", None),
            "output_spec_version": getattr(session, "output_spec_version", None),
            "output_spec_draft": getattr(session, "output_spec_draft", None),
            "pending_question": _pending_question_view(session),
        })
    return result


def _session_next_actions(session: Any) -> list[str]:
    status = getattr(session, "status", "")
    if getattr(session, "contract_version", "legacy_brief_v1") == "output_spec_v1":
        if status == "awaiting_plan":
            # The internal pre-proposal state projects as a draft action;
            # there is no user-visible awaiting_plan status.  A premature
            # proposal is rejected with an actionable error by the pipeline.
            return ["answer_clarification", "propose_document_plan"]
        if status == "awaiting_plan_confirmation":
            return ["propose_document_plan", "confirm_document_plan"]
        if status == "planned":
            return ["await_generation", "get_document_task_status"]
        if status == "blocked":
            return ["answer_clarification"]
        return []
    if status == "needs_clarification":
        return ["answer_clarification"]
    if status == "ready_to_generate":
        return ["create_document_work_order"]
    if status == "generating":
        return ["get_document_generation_status"]
    return []


def _safe_status(status: dict[str, Any]) -> dict[str, Any]:
    harness = status.get("harness_run") or {}
    safe_artifacts = []
    for artifact in (status.get("artifacts") or [])[:8]:
        if not isinstance(artifact, dict):
            continue
        item = {
            "artifact_id": str(artifact.get("artifact_id")),
            "stage": str(artifact.get("stage")),
        }
        if str(artifact.get("output_format") or "").strip():
            item["output_format"] = str(artifact.get("output_format"))
        if str(artifact.get("revision_id") or "").strip():
            item["revision_id"] = str(artifact.get("revision_id"))
        safe_artifacts.append(item)
    task = status.get("task") or {}
    safe_revisions = []
    for revision in (task.get("revisions") if isinstance(task, dict) else []) or []:
        if not isinstance(revision, dict) or not str(revision.get("revision_id") or "").strip():
            continue
        safe_revisions.append({
            "revision_id": str(revision["revision_id"]),
            "parent_artifact_id": str(revision.get("parent_artifact_id") or ""),
            "child_artifact_id": revision.get("child_artifact_id"),
            "status": str(revision.get("status") or ""),
            "revalidation_status": str(revision.get("revalidation_status") or "pending"),
            "revalidation_scope": list(revision.get("revalidation_scope") or [])[:32],
        })
    return {
        "work_order_id": status.get("work_order_id"),
        "task_id": status.get("task_id"),
        "status": status.get("status"),
        "phase": status.get("phase"),
        "scope_type": status.get("scope_type"),
        "knowledge_base_name": status.get("knowledge_base_name"),
        "target_format": status.get("target_format"),
        "unit_statuses": dict(status.get("unit_statuses") or {}),
        "run_id": harness.get("run_id"),
        "harness_status": harness.get("status"),
        "effective_executor": harness.get("effective_executor"),
        "degraded_reasons": list(harness.get("degraded_reasons") or []),
        "pending_human_event": harness.get("pending_human_event"),
        "job": dict(status.get("job") or {}),
        "next_actions": list(status.get("next_actions") or []),
        "validation": status.get("validation"),
        # 只保留不可变引用(artifact_id/stage/output_format),其余字段(校验/策略状态)不下发到对话侧。
        "artifacts": safe_artifacts,
        "revisions": safe_revisions,
    }


def _result_json(result: DocumentToolResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


@dataclass
class DocumentAuthoringToolset:
    pipeline: Any
    ctx: Any
    context: DocumentContext | DocumentAuthoringContext
    chat_session_id: str
    job_store: DocumentAuthoringJobStore
    event_sink: Callable[[dict], None] | None = None
    # These values are already server-resolved by the runner.  They are
    # carried into the work order so a worker can rebuild the evidence scope
    # without depending on the live chat turn.
    attachment_refs: list[Any] = field(default_factory=list)
    source_scope: str = "knowledge_base_only"

    def _uses_v2_planning(self) -> bool:
        """Enable the v2 tool surface only when the pipeline can execute it.

        The feature flag is process-wide, while tests, rolling upgrades and
        injected compatibility pipelines may briefly expose only the legacy
        methods.  Advertising plan tools in that state is misleading and a
        direct request would otherwise fail with an unhandled
        ``AttributeError``.  The production AppPipeline implements all three
        methods; older pipelines safely retain their legacy tool surface until
        they are upgraded.
        """
        if not _v2_enabled():
            return False
        # The router may construct a tool surface before a concrete document
        # service is installed (for example during a lazy startup or a
        # route-only compatibility probe).  Keep the advertised v2 names in
        # that case; an actual invocation will still fail closed at the
        # service boundary.  A pipeline that *does* expose the legacy service
        # but lacks v2 methods is different: it is an older executable and
        # must retain the legacy tool set.
        if not hasattr(self.pipeline, "document_generation"):
            return True
        required = (
            "create_document_plan_proposal",
            "confirm_document_plan",
            "get_document_task_projection",
        )
        return all(callable(getattr(self.pipeline, name, None)) for name in required)

    def _authorize(self, permission: Literal["read", "write"]) -> None:
        self.context.assert_scope(
            ctx=self.ctx,
            expected_kb=getattr(self.context, "knowledge_base_name", None),
            required_permission=permission,
        )
        # Every service call also receives the same server-derived KB scope.
        metadata = getattr(self.ctx, "metadata", None)
        if isinstance(metadata, dict):
            metadata["document_template_kb_name"] = getattr(self.context, "knowledge_base_name", None) or ""

    def _mark_task_queued(self, task_id: str | None) -> None:
        document_generation = getattr(self.pipeline, "document_generation", None)
        mark_task_queued = getattr(document_generation, "mark_document_task_queued", None)
        if callable(mark_task_queued):
            mark_task_queued(task_id)

    def _rejected(self, operation: str, code: str, message: str, **ids: Any) -> DocumentToolResult:
        return DocumentToolResult(
            status="rejected", operation=operation, error_code=code, message=message, **ids,
        )

    def _unavailable(self, operation: str, message: str, **ids: Any) -> DocumentToolResult:
        return DocumentToolResult(
            status="unavailable", operation=operation,
            error_code="document_authoring_unavailable", message=message, **ids,
        )

    def _emit_card(
        self,
        kind: Literal[
            "generation_session", "work_order_created", "work_order_status",
            "output_spec_confirmation",
        ],
        result: DocumentToolResult,
    ) -> None:
        if self.event_sink is None or result.status not in {"succeeded", "waiting_human"}:
            return
        card: dict[str, Any] = {
            "kind": kind,
            # Prefer the domain status carried by the tool result; fall back to
            # the bare tool status ("succeeded") only when none exists.
            "status": result.data.get("status") or result.status,
            "next_actions": list(result.next_actions),
            "kb_name": self.context.knowledge_base_name,
        }
        if result.work_order_id:
            card["work_order_id"] = result.work_order_id
        if result.task_id:
            card["task_id"] = result.task_id
        if result.generation_session_id:
            card["generation_session_id"] = result.generation_session_id
        # Clarification questions never cross the card channel: the popup is
        # progress-only and the conversation is the single input surface, so
        # the same question is never rendered twice.
        if kind == "work_order_status":
            # 产物只带不可变引用(artifact_id/stage)与格式枚举;URL 由前端拼装。
            if result.data.get("target_format"):
                card["target_format"] = result.data.get("target_format")
            if result.data.get("artifacts"):
                card["artifacts"] = result.data.get("artifacts")
        if kind == "output_spec_confirmation":
            # Gate 1 card: only the safe plan-proposal projection crosses the
            # wire (ids/versions/hashes/counts/policies), never sources.
            nested = result.data.get("proposal")
            proposal = nested if isinstance(nested, dict) else (
                result.data if result.data.get("plan_hash") else None
            )
            if isinstance(proposal, dict):
                card["proposal"] = proposal
            submission = result.data.get("submission")
            if isinstance(submission, dict):
                card["submission"] = submission
        try:
            self.event_sink({"type": "document_card", "card": card})
        except Exception:
            # Best-effort: a broken sink must never fail the tool result.
            return

    def _template_record(self, template_id: str) -> Any | None:
        """Read a template through the server-owned authoring store.

        The model may suggest a schema id, but it must never be able to make
        that id point at a different template.  Keeping this lookup inside the
        tool also makes the direct chat path work with the same immutable
        template version used by the document workbench.
        """

        service = getattr(self.pipeline, "document_generation", None)
        store = getattr(service, "store", None)
        getter = getattr(store, "get_template", None)
        if not callable(getter):
            return None
        return getter(template_id)

    def _resolve_template_schema(
        self,
        template_id: str,
        *,
        document_schema_id: str | None = None,
        document_schema_version: str | None = None,
        allow_legacy_unbound: bool = False,
    ) -> tuple[Any | None, str | None, str | None, DocumentToolResult | None]:
        """Resolve the approved schema bound to ``template_id``.

        Returns ``(template, schema_id, schema_version, error_result)``.  A
        supplied schema is accepted only when it exactly matches the binding
        persisted on the template; otherwise the request fails closed.
        """

        template = self._template_record(template_id)
        if template is None:
            # Older injected pipelines exposed only the work-order creation
            # method.  Keep that lower-level compatibility path working when
            # the caller explicitly supplied both ids; the one-shot direct
            # path never enables this fallback and therefore remains
            # fail-closed against guessed schema identities.
            legacy_id = str(document_schema_id or "").strip()
            legacy_version = str(document_schema_version or "").strip()
            if allow_legacy_unbound and legacy_id and legacy_version:
                return None, legacy_id, legacy_version, None
            return (
                None,
                None,
                None,
                self._rejected(
                    "generate_document_from_template",
                    "template_not_found",
                    "template version was not found",
                    template_version_id=template_id,
                ),
            )
        if str(getattr(template, "status", "")) != "approved":
            template_status = str(getattr(template, "status", "") or "unknown")
            return (
                template,
                None,
                None,
                DocumentToolResult(
                    status="rejected",
                    operation="generate_document_from_template",
                    error_code="template_not_approved",
                    message=(
                        "模板映射尚未完成人工确认，当前不能生成文档；"
                        "请先查看模板分析并由有权限的人员确认映射。"
                    ),
                    template_version_id=template_id,
                    next_actions=["get_document_template_analysis"],
                    data={
                        "mapping_status": template_status,
                        "mapping_explanation": (
                            "模板的字段映射仍处于待确认状态，生成工具不会自动批准或修改映射。"
                        ),
                    },
                ),
            )
        bound_id = str(getattr(template, "template_schema_id", "") or "").strip()
        bound_version = str(getattr(template, "template_schema_version", "") or "").strip()
        requested_id = str(document_schema_id or "").strip()
        requested_version = str(document_schema_version or "").strip()
        if not bound_id or not bound_version:
            return (
                template,
                None,
                None,
                self._rejected(
                    "generate_document_from_template",
                    "template_schema_missing",
                    "approved template has no bound document schema",
                    template_version_id=template_id,
                ),
            )
        if (requested_id and requested_id != bound_id) or (
            requested_version and requested_version != bound_version
        ):
            return (
                template,
                None,
                None,
                self._rejected(
                    "generate_document_from_template",
                    "document_schema_mismatch",
                    "document schema does not belong to the selected template",
                    template_version_id=template_id,
                ),
            )

        # An injected test/legacy pipeline may expose only get_template; the
        # real WorkOrderStore always provides get_document_schema.  When it is
        # available, require the matching schema to be approved as well.
        service = getattr(self.pipeline, "document_generation", None)
        store = getattr(service, "store", None)
        schema_getter = getattr(store, "get_document_schema", None)
        if callable(schema_getter):
            schema = schema_getter(bound_id, bound_version)
            if schema is None or str(getattr(schema, "status", "")) != "approved":
                return (
                    template,
                    None,
                    None,
                    self._rejected(
                        "generate_document_from_template",
                        "document_schema_not_approved",
                        "template schema is not approved for generation",
                        template_version_id=template_id,
                    ),
                )
        return template, bound_id, bound_version, None

    @staticmethod
    def _preflight_status(stage: str) -> str:
        normalized = str(stage or "").strip().lower()
        if "review" in normalized or "human" in normalized:
            return "needs_review"
        if normalized in {"ready", "queued", "running", "complete"}:
            return normalized
        return "blocked"

    def _bound_execution_mode(
        self,
        template_id: str,
        requested: Literal["internal_harness", "deterministic_only", "external_agent"],
    ) -> Literal["internal_harness", "deterministic_only", "external_agent"]:
        """Use the approved schema's executor policy, never a model guess."""

        service = getattr(self.pipeline, "document_generation", None)
        store = getattr(service, "store", None)
        template = self._template_record(template_id)
        schema_getter = getattr(store, "get_document_schema", None)
        if callable(schema_getter) and template is not None:
            schema = schema_getter(
                str(getattr(template, "template_schema_id", "")),
                str(getattr(template, "template_schema_version", "")),
            )
            mode = str(getattr(schema, "execution_mode", "") or "").strip()
            if mode in {"internal_harness", "deterministic_only", "external_agent"}:
                return mode  # type: ignore[return-value]
        return requested

    def _generation_client_request_id(self, session_id: str | None = None) -> str:
        """Return a per-generation idempotency key, not the upload key."""

        normalized_session = str(session_id or "").strip()
        if normalized_session:
            return f"document-generation:{normalized_session}"[:128]
        return str(self.context.client_request_id).strip()[:128]

    @staticmethod
    def _pending_output_spec_question(session: Any) -> dict[str, Any] | None:
        """Return the next intake question when a v2 draft is incomplete."""

        # ``needs_clarification`` is persisted state, and may represent a
        # server-published question (for example content_confirmation) that
        # is not derivable from the current draft.  Prefer that question while
        # retaining _pending_question_view's answer-aware ordering so an
        # already answered question is never revived.
        if str(getattr(session, "status", "") or "") == "needs_clarification":
            persisted = _pending_question_view(session)
            if persisted is not None:
                return persisted

        draft = getattr(session, "output_spec_draft", None)
        if not isinstance(draft, dict):
            return None
        question = OutputSpecIntakeService().next_question(draft)
        if question is None:
            return None
        return {
            "question_id": question.question_id,
            "content": question.prompt,
            "options": list(question.options),
            "reason": question.reason,
        }

    def _generation_session_for_direct_request(
        self,
        *,
        template_id: str,
        purpose: str,
        generation_session_id: str | None,
        use_recommended_defaults: bool,
        output_format: str,
    ) -> tuple[Any | None, DocumentToolResult | None]:
        """Reuse a confirmed session or create a safe one-shot session."""

        session_id = str(
            generation_session_id or self.context.generation_session_id or ""
        ).strip()
        if session_id:
            try:
                session = self.pipeline.get_document_generation_session(
                    self.ctx, session_id,
                )
            except PermissionError:
                raise
            except (KeyError, ValueError):
                return None, self._rejected(
                    "generate_document_from_template",
                    "generation_session_not_found",
                    "generation session was not found",
                    template_version_id=template_id,
                    generation_session_id=session_id,
                )
            if session.template_version_id != template_id:
                return None, self._rejected(
                    "generate_document_from_template",
                    "document_context_session_mismatch",
                    "generation session does not belong to the selected template",
                    template_version_id=template_id,
                    generation_session_id=session_id,
                )
            if session.status != "ready_to_generate" or not session.brief.confirmed:
                return None, self._rejected(
                    "generate_document_from_template",
                    "generation_session_not_confirmed",
                    "generation session still needs clarification; answer the pending questions or start a new request using recommended defaults",
                    template_version_id=template_id,
                    generation_session_id=session_id,
                    next_actions=["answer_clarification", "confirm_generation_session"],
                )
            return session, None

        try:
            session = self.pipeline.create_document_generation_session(
                self.ctx,
                knowledge_base_name=self.context.knowledge_base_name,
                template_version_id=template_id,
                purpose=str(purpose or "").strip(),
                output_policy={"format": output_format},
                auto_confirm_recommended=bool(use_recommended_defaults),
            )
        except TypeError:
            # Compatibility with injected pipelines from before the one-shot
            # flag was introduced.  Such a pipeline still gets the normal
            # session contract rather than an unhandled model/tool exception.
            session = self.pipeline.create_document_generation_session(
                self.ctx,
                knowledge_base_name=self.context.knowledge_base_name,
                template_version_id=template_id,
                purpose=str(purpose or "").strip(),
                output_policy={"format": output_format},
            )
        except PermissionError:
            raise
        except (ValueError, KeyError):
            return None, self._rejected(
                "generate_document_from_template",
                "generation_session_not_ready",
                "generation session could not be created",
                template_version_id=template_id,
            )
        if session.template_version_id != template_id:
            return None, self._rejected(
                "generate_document_from_template",
                "document_context_template_mismatch",
                "generation session does not belong to the selected template",
                template_version_id=template_id,
                generation_session_id=session.session_id,
            )
        if session.status != "ready_to_generate" or not session.brief.confirmed:
            return None, self._rejected(
                "generate_document_from_template",
                "generation_session_not_confirmed",
                "recommended defaults did not produce a confirmed generation session",
                template_version_id=template_id,
                generation_session_id=session.session_id,
                next_actions=["answer_clarification", "confirm_generation_session"],
            )
        return session, None

    def _reusable_v2_session(
        self,
        *,
        generation_session_id: str | None,
        template_id: str | None,
    ) -> Any | None:
        """Return the attached live intake session, or ``None`` to create one.

        The conversation owns one OutputSpec intake session per document
        request.  Recreating a session per tool call would fork the state and
        silently discard already-recorded clarification answers.
        """

        session = None
        session_id = str(
            generation_session_id or self.context.generation_session_id or ""
        ).strip()
        if session_id:
            try:
                session = self.pipeline.get_document_generation_session(self.ctx, session_id)
            except (PermissionError, KeyError, ValueError):
                # A stale or foreign pointer must not abort the turn; a fresh
                # server-owned session fails closed on its own scope checks.
                session = None
        if session is None:
            # No usable pointer: resume the conversation's own intake session
            # so recorded answers are never discarded by a fork.
            finder = getattr(self.pipeline, "find_reusable_document_generation_session", None)
            if callable(finder):
                session = finder(
                    self.ctx,
                    knowledge_base_name=self.context.knowledge_base_name,
                    template_version_id=template_id,
                )
        if session is None:
            return None
        if str(getattr(session, "contract_version", "") or "") != "output_spec_v1":
            return None
        if template_id and str(getattr(session, "template_version_id", "") or "") != template_id:
            return None
        if str(getattr(session, "status", "") or "") not in {
            "needs_clarification", "awaiting_plan", "awaiting_plan_confirmation", "blocked",
        }:
            return None
        return session

    def _resolve_attached_clarification_session(self, session_id: str | None) -> Any | None:
        """Resolve the live intake session even when the call lost its pointer.

        The session reference is server-owned: a model may still pass a stale
        or fabricated id after a browser refresh.  Prefer the requested id,
        then the attached context pointer, then the conversation's own active
        OutputSpec session, so a lost pointer resumes the recorded answers
        instead of forking a fresh draft.
        """

        candidates = (
            str(session_id or "").strip(),
            str(self.context.generation_session_id or "").strip(),
        )
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return self.pipeline.get_document_generation_session(self.ctx, candidate)
            except (PermissionError, KeyError, ValueError):
                continue
        finder = getattr(self.pipeline, "find_reusable_document_generation_session", None)
        if callable(finder):
            return finder(
                self.ctx,
                knowledge_base_name=self.context.knowledge_base_name,
                template_version_id=None,
            )
        return None

    @staticmethod
    def _has_recommended_policies(draft: Any) -> bool:
        if not isinstance(draft, dict):
            return False
        return bool(
            str(draft.get("missing_data_policy") or "").strip()
            and str(draft.get("inference_policy") or "").strip()
            and str(draft.get("approval_policy_id") or "").strip()
        )

    def _apply_v2_recommended_defaults(
        self,
        session: Any,
        *,
        use_recommended_defaults: bool,
    ) -> Any:
        """Accept the safe recommendation bundle at most once per session."""

        if not use_recommended_defaults or self._has_recommended_policies(
            getattr(session, "output_spec_draft", None)
        ):
            return session
        try:
            return self.pipeline.answer_document_generation_session(
                self.ctx,
                session.session_id,
                question_id="recommendations",
                answer="采用推荐方案",
                client_request_id=self._generation_client_request_id(session.session_id),
            )
        except (PermissionError, ValueError, KeyError):
            # Recommendation acceptance is advisory; a rejection must not
            # block the proposal from being compiled.
            return session

    def _v2_confirmation_for_direct_request(
        self,
        *,
        template_id: str | None,
        purpose: str,
        use_recommended_defaults: bool,
        generation_session_id: str | None = None,
    ) -> tuple[DocumentToolResult | None, DocumentToolResult | None]:
        """v2 direct path: prepare recommendations + proposal, never execute.

        Returns ``(result, error)``.  The result is always ``waiting_human``
        with ``confirm_document_plan`` as the next action; a Work Order or
        generation job can only appear after the explicit hash-bound
        confirmation, which this tool must never perform.
        """
        operation = "generate_document_from_template"
        session = self._reusable_v2_session(
            generation_session_id=generation_session_id,
            template_id=template_id,
        )
        if session is None:
            try:
                session = self.pipeline.create_document_generation_session(
                    self.ctx,
                    knowledge_base_name=self.context.knowledge_base_name,
                    template_version_id=template_id,
                    purpose=str(purpose or "").strip(),
                    contract_version="output_spec_v1",
                )
            except PermissionError:
                raise
            except (ValueError, KeyError):
                return None, self._rejected(
                    operation,
                    "generation_session_not_ready",
                    "generation session could not be created",
                    template_version_id=template_id,
                )
        if str(getattr(session, "status", "") or "") != "blocked":
            session = self._apply_v2_recommended_defaults(
                session,
                use_recommended_defaults=use_recommended_defaults,
            )
        # The advisory answer above may have advanced the draft version; the
        # proposal must bind the version that is current right now.
        try:
            session = self.pipeline.get_document_generation_session(
                self.ctx, session.session_id,
            )
        except (PermissionError, KeyError, ValueError):
            pass
        if str(getattr(session, "status", "") or "") == "blocked":
            explanation = "文档生成会话已被阻止，当前无法继续。"
            reason = "document_generation_blocked"
            for message in reversed(getattr(session, "messages", None) or []):
                if getattr(message, "role", "") != "assistant":
                    continue
                content = str(getattr(message, "content", "") or "").strip()
                if content:
                    explanation = content
                candidate_reason = str(getattr(message, "reason", "") or "").strip()
                if candidate_reason:
                    reason = candidate_reason
                break
            return DocumentToolResult(
                status="waiting_human",
                operation=operation,
                error_code=reason,
                message=explanation,
                template_version_id=template_id,
                generation_session_id=session.session_id,
                task_id=getattr(session, "document_task_id", None),
                data={"status": "blocked", "reason": reason},
                next_actions=[],
            ), None
        pending_question = self._pending_output_spec_question(session)
        if pending_question is not None:
            result = DocumentToolResult(
                status="waiting_human",
                operation=operation,
                message=pending_question["content"],
                template_version_id=template_id,
                generation_session_id=session.session_id,
                task_id=getattr(session, "document_task_id", None),
                data={"status": "needs_clarification", **pending_question},
                next_actions=["answer_clarification"],
            )
            self._emit_card("generation_session", result)
            return result, None
        try:
            proposal = self.pipeline.create_document_plan_proposal(
                self.ctx,
                session.session_id,
                client_request_id=self._generation_client_request_id(session.session_id),
                expected_output_spec_version=int(
                    getattr(session, "output_spec_version", 0) or 0
                ),
            )
        except PermissionError:
            raise
        except (ValueError, KeyError) as exc:
            reason = (
                str(exc).strip().strip("'").strip()
                or "document plan proposal could not be compiled"
            )
            retry = self._pending_output_spec_question(session)
            return None, self._rejected(
                operation,
                "plan_proposal_rejected",
                reason,
                generation_session_id=session.session_id,
                next_actions=["answer_clarification"] if retry else ["propose_document_plan"],
            )
        confirmation_reasons = _plan_confirmation_reasons(proposal, session)
        if (
            bool(getattr(src.settings, "DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED", False))
            and not confirmation_reasons
        ):
            try:
                submission = self.pipeline.confirm_document_plan(
                    self.ctx,
                    session.session_id,
                    expected_output_spec_hash=str(proposal.get("output_spec_hash") or ""),
                    expected_plan_hash=str(proposal.get("plan_hash") or ""),
                    client_request_id=(
                        f"risk-auto:{session.session_id}:{proposal.get('plan_hash') or ''}"
                    )[:128],
                )
            except (PermissionError, KeyError, ValueError):
                confirmation_reasons = ["automatic_confirmation_failed"]
            else:
                result = DocumentToolResult(
                    status="succeeded",
                    operation=operation,
                    message="低风险文档计划已自动确认，后台正在创建生成工单。",
                    template_version_id=template_id,
                    generation_session_id=session.session_id,
                    task_id=getattr(session, "document_task_id", None),
                    work_order_id=(
                        submission.get("work_order_id")
                        if isinstance(submission, dict) else None
                    ),
                    job_id=(submission.get("job_id") if isinstance(submission, dict) else None),
                    data={
                        "status": (
                            submission.get("status")
                            if isinstance(submission, dict) else "pending"
                        ),
                        "submission": submission,
                        "confirmation_required": False,
                        "confirmation_reasons": [],
                    },
                    next_actions=["await_generation"],
                )
                self._emit_card("work_order_status", result)
                return result, None

        result = DocumentToolResult(
            status="waiting_human",
            operation=operation,
            message=(
                "已生成文档计划提案；请确认将要生成的内容后再执行生成。"
                "确认前不会创建工单或后台任务。"
            ),
            template_version_id=template_id,
            generation_session_id=session.session_id,
            task_id=getattr(session, "document_task_id", None),
            data={
                "status": "awaiting_plan_confirmation",
                "proposal": proposal,
                "confirmation_required": True,
                "confirmation_reasons": confirmation_reasons,
            },
            next_actions=["confirm_document_plan"],
        )
        self._emit_card("output_spec_confirmation", result)
        return result, None

    def generate_document_from_template(
        self,
        purpose: str = "",
        template_version_id: str | None = None,
        generation_session_id: str | None = None,
        document_schema_id: str | None = None,
        document_schema_version: str | None = None,
        execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] = "internal_harness",
        use_recommended_defaults: bool = True,
        output_format: Literal["native", "xlsm", "xlsx", "docx", "markdown", "pdf", "pptx"] = "native",
    ) -> DocumentToolResult:
        """Create the real template-backed artifact job from a chat turn.

        This is deliberately one operation from the model's point of view:
        template approval, schema binding, safe default policy, preflight and
        durable queueing all remain server-owned.  The chat answer therefore
        cannot silently turn into a generic Markdown export.
        """

        operation = "generate_document_from_template"
        self._authorize("write")
        template_id = str(template_version_id or self.context.template_version_id).strip()
        if template_id != self.context.template_version_id:
            return self._rejected(
                operation,
                "document_context_template_mismatch",
                "template reference does not match the attached context",
                template_version_id=template_id,
            )
        if self._uses_v2_planning():
            # Planning/intake may begin from a draft template.  Only require
            # that the server can resolve the template; approval and schema
            # binding remain prerequisites for the later artifact path.
            template = self._template_record(template_id)
            if template is None:
                return self._rejected(
                    operation,
                    "template_not_found",
                    "template version was not found",
                    template_version_id=template_id,
                )
            bound_id = str(getattr(template, "template_schema_id", "") or "").strip()
            bound_version = str(getattr(template, "template_schema_version", "") or "").strip()
            if (document_schema_id and document_schema_id != bound_id) or (
                document_schema_version and document_schema_version != bound_version
            ):
                return self._rejected(
                    operation,
                    "document_schema_mismatch",
                    "document schema does not belong to the selected template",
                    template_version_id=template_id,
                )
            # Gate 1: a direct template request prepares recommendations and a
            # hash-bound proposal, but the explicit confirmation is the only
            # path into the Work Order/job pipeline.
            v2_template_id = template_id or None
            result, error = self._v2_confirmation_for_direct_request(
                template_id=v2_template_id,
                purpose=purpose,
                use_recommended_defaults=use_recommended_defaults,
                generation_session_id=generation_session_id,
            )
            if error is not None:
                return error
            return result

        template, schema_id, schema_version, schema_error = self._resolve_template_schema(
            template_id,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
        )
        if schema_error is not None:
            return schema_error

        try:
            analysis = self.pipeline.get_document_template_analysis_for_review(
                self.ctx, analysis_id=self.context.analysis_id,
            )
        except PermissionError:
            raise
        except (KeyError, ValueError):
            return self._rejected(
                operation,
                "analysis_not_found",
                "template analysis was not found or is no longer valid",
                template_version_id=template_id,
                analysis_id=self.context.analysis_id,
            )
        if analysis.template_version_id != template_id:
            return self._rejected(
                operation,
                "document_context_template_mismatch",
                "template analysis does not belong to the attached template",
                template_version_id=template_id,
                analysis_id=self.context.analysis_id,
            )
        decision = getattr(analysis, "activation_decision", None)
        if str(getattr(analysis, "status", "")) != "ready_for_confirmation":
            return self._rejected(
                operation,
                "template_analysis_not_ready",
                "template analysis is not ready for generation",
                template_version_id=template_id,
                analysis_id=self.context.analysis_id,
                next_actions=["review_template_mapping", "confirm_document_template"],
            )
        if decision is not None and str(getattr(decision, "status", "")) != "auto_accepted":
            return self._rejected(
                operation,
                "template_requires_human_review",
                "template mapping requires human review before generation",
                template_version_id=template_id,
                analysis_id=self.context.analysis_id,
                next_actions=["review_template_mapping", "confirm_document_template"],
            )

        # ``template`` is guaranteed non-None by _resolve_template_schema; the
        # local guard keeps type checkers and compatibility shims honest.
        native_output_format = str(getattr(template, "format", "") or "").strip().lower()
        requested_output_format = str(output_format or "native").strip().lower()
        if requested_output_format not in {"native", native_output_format, "pdf", "pptx"}:
            return DocumentToolResult(
                status="rejected",
                operation=operation,
                error_code="template_output_conversion_not_supported",
                message=(
                    f"模板已识别，但当前版本只支持模板原生格式 {native_output_format}；"
                    f"以及经过语义校验的 PDF/PPTX，无法直接转换为 {requested_output_format}。"
                ),
                template_version_id=template_id,
                next_actions=["generate_native_artifact"],
                data={
                    "output_contract": {
                        "native_format": native_output_format,
                        "requested_format": requested_output_format,
                        "conversion_status": "unsupported",
                    }
                },
            )
        if (
            requested_output_format in {"pdf", "pptx"}
            and requested_output_format != native_output_format
            and not callable(getattr(self.pipeline, "submit_document_artifact_conversion", None))
        ):
            # Compatibility/injected test pipelines may predate the durable
            # conversion worker.  Do not claim that a native artifact is the
            # requested final format when that second stage is unavailable.
            return DocumentToolResult(
                status="rejected",
                operation=operation,
                error_code="template_output_conversion_not_enabled",
                message="模板填充能力可用，但 PDF/PPTX 转换 worker 尚未配置。",
                template_version_id=template_id,
                next_actions=["generate_native_artifact"],
                data={
                    "output_contract": {
                        "native_format": native_output_format,
                        "requested_format": requested_output_format,
                        "conversion_status": "not_enabled",
                    }
                },
            )
        session, session_error = self._generation_session_for_direct_request(
            template_id=template_id,
            purpose=purpose,
            generation_session_id=generation_session_id,
            use_recommended_defaults=use_recommended_defaults,
            output_format=native_output_format,
        )
        if session_error is not None:
            return session_error
        if session is None or schema_id is None or schema_version is None:
            return self._unavailable(
                operation,
                "document generation dependencies are temporarily unavailable",
                template_version_id=template_id,
            )

        effective_execution_mode = self._bound_execution_mode(template_id, execution_mode)

        prepare = getattr(self.pipeline, "prepare_knowledge_base_document_generation", None)
        try:
            if callable(prepare):
                prepared = prepare(
                    self.ctx,
                    knowledge_base_name=self.context.knowledge_base_name,
                    template_version_id=template_id,
                    document_schema_id=schema_id,
                    document_schema_version=schema_version,
                    generation_session_id=session.session_id,
                    execution_mode=effective_execution_mode,
                    source_scope=self.source_scope,
                    attachment_refs=list(self.attachment_refs),
                )
            else:
                order = self.pipeline.create_knowledge_base_document_work_order(
                    self.ctx,
                    knowledge_base_name=self.context.knowledge_base_name,
                    template_version_id=template_id,
                    document_schema_id=schema_id,
                    document_schema_version=schema_version,
                    idempotency_key=self._generation_client_request_id(session.session_id),
                    generation_session_id=session.session_id,
                    execution_mode=effective_execution_mode,
                    source_scope=self.source_scope,
                    attachment_refs=list(self.attachment_refs),
                )
                prepared = {"stage": "ready", "work_order_id": order.work_order_id}
        except PermissionError:
            raise
        except (ValueError, KeyError):
            return self._rejected(
                operation,
                "work_order_rejected",
                "document work order could not be prepared",
                template_version_id=template_id,
                generation_session_id=session.session_id,
            )
        except Exception:
            return self._unavailable(
                operation,
                "document work order is temporarily unavailable",
                template_version_id=template_id,
                generation_session_id=session.session_id,
            )

        prepared = dict(prepared or {})
        work_order_id = str(prepared.get("work_order_id") or "").strip()
        stage = str(prepared.get("stage") or "").strip()
        if not work_order_id:
            return self._unavailable(
                operation,
                "document preflight did not return a work order",
                template_version_id=template_id,
                generation_session_id=session.session_id,
            )
        task_id = str(prepared.get("task_id") or "").strip() or None
        if task_id is None:
            authoring_store = getattr(
                getattr(self.pipeline, "document_generation", None), "store", None,
            )
            get_work_order = getattr(authoring_store, "get_work_order", None)
            if callable(get_work_order):
                persisted_order = get_work_order(work_order_id)
                task_id = str(getattr(persisted_order, "task_id", "") or "").strip() or None
        if stage != "ready":
            result = DocumentToolResult(
                status="waiting_human",
                operation=operation,
                message="模板已完成安全预检，但生成前仍需要人工处理待办",
                template_version_id=template_id,
                generation_session_id=session.session_id,
                task_id=task_id,
                work_order_id=work_order_id,
                data={
                    "status": self._preflight_status(stage),
                    "stage": stage,
                    "issues": list(prepared.get("issues") or prepared.get("exceptions") or []),
                },
                next_actions=["open_document_workbench", "resolve_document_review"],
            )
            self._emit_card("work_order_created", result)
            return result

        try:
            job = self.job_store.create_job(
                tenant_id=self.context.tenant_id,
                user_id=self.context.owner_user_id,
                session_id=self.chat_session_id,
                client_request_id=self._generation_client_request_id(session.session_id),
                operation="generate_work_order",
                work_order_id=work_order_id,
                task_id=task_id,
                payload={
                    "work_order_id": work_order_id,
                    "knowledge_base_name": self.context.knowledge_base_name,
                    "user_id": self.context.owner_user_id,
                    "template_version_id": template_id,
                    "document_schema_id": schema_id,
                    "document_schema_version": schema_version,
                    "generation_session_id": session.session_id,
                    "execution_mode": effective_execution_mode,
                    "requested_output_format": requested_output_format,
                },
            )
        except ValueError:
            return self._rejected(
                operation,
                "generation_job_rejected",
                "document generation job could not be queued",
                template_version_id=template_id,
                generation_session_id=session.session_id,
                work_order_id=work_order_id,
            )
        except Exception:
            return self._unavailable(
                operation,
                "document generation queue is temporarily unavailable",
                template_version_id=template_id,
                generation_session_id=session.session_id,
                work_order_id=work_order_id,
            )
        self._mark_task_queued(task_id)
        result = DocumentToolResult(
            status="succeeded",
            operation=operation,
            message="模板填充任务已提交，完成后可下载最终文档",
            template_version_id=template_id,
            generation_session_id=session.session_id,
            task_id=task_id,
            work_order_id=work_order_id,
            job_id=job.job_id,
            data={
                "status": job.status,
                "stage": "ready",
                "target_format": native_output_format,
                "artifact_kind": "document_generation",
                "execution_mode": effective_execution_mode,
                "output_contract": {
                    "native_format": native_output_format,
                    "requested_format": native_output_format if requested_output_format == "native" else requested_output_format,
                    "conversion_status": (
                        "not_required"
                        if requested_output_format in {"native", native_output_format}
                        else "pending_native_artifact"
                    ),
                },
            },
            next_actions=["get_document_generation_status"],
        )
        self._emit_card("work_order_created", result)
        return result

    def get_document_template_analysis(self, analysis_id: str) -> DocumentToolResult:
        operation = "get_document_template_analysis"
        self._authorize("read")
        if str(analysis_id).strip() != self.context.analysis_id:
            return self._rejected(operation, "document_context_analysis_mismatch", "analysis reference does not match the attached context", analysis_id=analysis_id)
        try:
            analysis = self.pipeline.get_document_template_analysis_for_review(
                self.ctx, analysis_id=self.context.analysis_id,
            )
        except PermissionError:
            raise
        except KeyError:
            return self._rejected(operation, "analysis_not_found", "template analysis was not found", analysis_id=analysis_id)
        except (ValueError, RuntimeError):
            return self._unavailable(operation, "template analysis is temporarily unavailable", analysis_id=analysis_id)
        if analysis.template_version_id != self.context.template_version_id:
            return self._rejected(operation, "document_context_template_mismatch", "analysis does not belong to the attached template", analysis_id=analysis_id)
        return DocumentToolResult(
            status="succeeded", operation=operation, message="template analysis loaded",
            analysis_id=analysis.analysis_id, template_version_id=analysis.template_version_id,
            data=_safe_analysis(analysis),
            next_actions=["start_document_generation_session"] if analysis.status == "ready_for_confirmation" else [],
        )

    def start_document_generation_session(
        self, template_version_id: str | None = None, purpose: str = "",
    ) -> DocumentToolResult:
        operation = "start_document_generation_session"
        self._authorize("write")
        raw_template = template_version_id or self.context.template_version_id
        template_id = str(raw_template).strip() if raw_template is not None else ""
        if template_id and template_id != self.context.template_version_id:
            return self._rejected(operation, "document_context_template_mismatch", "template reference does not match the attached context", template_version_id=template_id)
        if _v2_enabled():
            # The conversation owns one intake session.  A repeated start must
            # resume that session instead of forking a new draft that discards
            # recorded answers and re-asks questions already answered.
            reusable = self._reusable_v2_session(
                generation_session_id=None,
                template_id=template_id or None,
            )
            if reusable is not None:
                result = DocumentToolResult(
                    status="succeeded", operation=operation, message="generation session resumed",
                    template_version_id=reusable.template_version_id,
                    generation_session_id=reusable.session_id,
                    task_id=getattr(reusable, "document_task_id", None),
                    data=_safe_session(reusable), next_actions=_session_next_actions(reusable),
                )
                self._emit_card("generation_session", result)
                return result
        try:
            if _v2_enabled():
                # A v2 intake session may be template-free; the layout source
                # binding stays server-owned in the OutputSpec draft.
                session = self.pipeline.create_document_generation_session(
                    self.ctx,
                    knowledge_base_name=self.context.knowledge_base_name,
                    template_version_id=template_id or None,
                    purpose=str(purpose or "").strip(),
                    contract_version="output_spec_v1",
                )
            else:
                session = self.pipeline.create_document_generation_session(
                    self.ctx,
                    knowledge_base_name=self.context.knowledge_base_name,
                    template_version_id=template_id,
                    purpose=str(purpose or "").strip(),
                )
        except PermissionError:
            raise
        except (ValueError, KeyError):
            return self._rejected(operation, "generation_session_not_ready", "generation session could not be created", template_version_id=template_id)
        result = DocumentToolResult(
            status="succeeded", operation=operation, message="generation session started",
            template_version_id=template_id or None, generation_session_id=session.session_id,
            task_id=getattr(session, "document_task_id", None),
            data=_safe_session(session), next_actions=_session_next_actions(session),
        )
        self._emit_card("generation_session", result)
        return result

    def answer_clarification(
        self,
        session_id: str,
        question_id: str,
        answer: str,
        client_request_id: str | None = None,
    ) -> DocumentToolResult:
        operation = "answer_clarification"
        self._authorize("write")
        resolved = self._resolve_attached_clarification_session(session_id)
        if resolved is None:
            return self._rejected(
                operation,
                "clarification_session_unresolved",
                "no active requirement session was found for this conversation",
                generation_session_id=session_id,
            )
        session_id = resolved.session_id
        try:
            session = self.pipeline.answer_document_generation_session(
                self.ctx,
                session_id,
                question_id=question_id,
                answer=answer,
                client_request_id=client_request_id,
            )
        except PermissionError:
            raise
        except (ValueError, KeyError) as exc:
            reason = str(exc).strip() or "validation failed"
            return self._rejected(
                operation,
                "clarification_rejected",
                f"clarification answer was rejected: {reason}",
                generation_session_id=session_id,
            )
        if session.template_version_id != self.context.template_version_id:
            return self._rejected(operation, "document_context_template_mismatch", "session does not belong to the attached template", generation_session_id=session_id)
        result = DocumentToolResult(
            status="succeeded", operation=operation, message="clarification recorded",
            template_version_id=session.template_version_id, generation_session_id=session.session_id,
            task_id=getattr(session, "document_task_id", None),
            data=_safe_session(session), next_actions=_session_next_actions(session),
        )
        card_kind = (
            "requirement_clarification"
            if getattr(session, "contract_version", "legacy_brief_v1") == "output_spec_v1"
            else "generation_session"
        )
        self._emit_card(card_kind, result)  # type: ignore[arg-type]
        return result

    def confirm_generation_session(self, session_id: str) -> DocumentToolResult:
        operation = "confirm_generation_session"
        self._authorize("write")
        if self.context.generation_session_id and session_id != self.context.generation_session_id:
            return self._rejected(operation, "document_context_session_mismatch", "session reference does not match the attached context", generation_session_id=session_id)
        try:
            session = self.pipeline.confirm_document_generation_session(self.ctx, session_id)
        except PermissionError:
            raise
        except (ValueError, KeyError):
            return self._rejected(operation, "generation_session_not_ready", "generation session is not ready for confirmation", generation_session_id=session_id)
        if session.template_version_id != self.context.template_version_id:
            return self._rejected(operation, "document_context_template_mismatch", "session does not belong to the attached template", generation_session_id=session_id)
        result = DocumentToolResult(
            status="succeeded", operation=operation, message="generation session confirmed",
            template_version_id=session.template_version_id, generation_session_id=session.session_id,
            task_id=getattr(session, "document_task_id", None),
            data=_safe_session(session), next_actions=["create_document_work_order"],
        )
        self._emit_card("generation_session", result)
        return result

    def create_document_work_order(
        self,
        document_schema_id: str | None = None,
        document_schema_version: str | None = None,
        generation_session_id: str | None = None,
        execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] = "internal_harness",
        template_version_id: str | None = None,
    ) -> DocumentToolResult:
        operation = "create_document_work_order"
        self._authorize("write")
        template_id = str(template_version_id or self.context.template_version_id).strip()
        session_id = str(generation_session_id or self.context.generation_session_id or "").strip() or None
        if template_id != self.context.template_version_id:
            return self._rejected(operation, "document_context_template_mismatch", "template reference does not match the attached context", template_version_id=template_id)
        if self.context.generation_session_id and session_id != self.context.generation_session_id:
            return self._rejected(operation, "document_context_session_mismatch", "session reference does not match the attached context", generation_session_id=session_id)
        _template, resolved_schema_id, resolved_schema_version, schema_error = self._resolve_template_schema(
            template_id,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
            allow_legacy_unbound=True,
        )
        if schema_error is not None:
            # Preserve the historical operation name for callers of this
            # lower-level tool while returning the same fail-closed reason.
            return schema_error.model_copy(update={"operation": operation})
        if resolved_schema_id is None or resolved_schema_version is None:
            return self._rejected(
                operation,
                "document_schema_missing",
                "document schema is not bound to the selected template",
                template_version_id=template_id,
            )
        document_schema_id = resolved_schema_id
        document_schema_version = resolved_schema_version
        execution_mode = self._bound_execution_mode(template_id, execution_mode)
        generation_brief: dict[str, Any] | None = None
        if session_id:
            try:
                session = self.pipeline.get_document_generation_session(self.ctx, session_id)
            except PermissionError:
                raise
            except KeyError:
                return self._rejected(operation, "generation_session_not_found", "generation session was not found", generation_session_id=session_id)
            if session.template_version_id != template_id:
                return self._rejected(operation, "document_context_template_mismatch", "session does not belong to the attached template", generation_session_id=session_id)
            if session.status != "ready_to_generate" or not session.brief.confirmed:
                return self._rejected(operation, "generation_session_not_confirmed", "generation session must be confirmed first", generation_session_id=session_id)
            generation_brief = session.brief.model_dump(mode="json")
        try:
            order = self.pipeline.create_knowledge_base_document_work_order(
                self.ctx,
                knowledge_base_name=self.context.knowledge_base_name,
                template_version_id=template_id,
                document_schema_id=document_schema_id,
                document_schema_version=document_schema_version,
                idempotency_key=self._generation_client_request_id(session_id),
                generation_session_id=session_id,
                generation_brief=generation_brief,
                execution_mode=execution_mode,
                source_scope=self.source_scope,
                attachment_refs=list(self.attachment_refs),
            )
            job = self.job_store.create_job(
                tenant_id=self.context.tenant_id,
                user_id=self.context.owner_user_id,
                session_id=self.chat_session_id,
                client_request_id=self.context.client_request_id,
                operation="generate_work_order",
                work_order_id=order.work_order_id,
                task_id=getattr(order, "task_id", None),
                payload={
                    "work_order_id": order.work_order_id,
                    "knowledge_base_name": self.context.knowledge_base_name,
                    "user_id": self.context.owner_user_id,
                    "execution_mode": execution_mode,
                },
            )
        except PermissionError:
            raise
        except (ValueError, KeyError):
            return self._rejected(operation, "work_order_rejected", "document work order could not be created", template_version_id=template_id)
        except Exception:
            return self._unavailable(operation, "document work order is temporarily unavailable", template_version_id=template_id)
        self._mark_task_queued(getattr(order, "task_id", None))
        result = DocumentToolResult(
            status="succeeded", operation=operation, message="document work order queued",
            template_version_id=template_id, generation_session_id=session_id,
            task_id=getattr(order, "task_id", None),
            work_order_id=order.work_order_id, job_id=job.job_id,
            data={"status": job.status, "execution_mode": order.execution_mode},
            next_actions=["get_document_generation_status"],
        )
        self._emit_card("work_order_created", result)
        return result

    def get_document_generation_status(self, work_order_id: str) -> DocumentToolResult:
        operation = "get_document_generation_status"
        self._authorize("read")
        try:
            status = self.pipeline.get_document_run_status(work_order_id, self.ctx)
        except PermissionError:
            raise
        except KeyError:
            return self._rejected(operation, "work_order_not_found", "document work order was not found", work_order_id=work_order_id)
        if status is None:
            return self._rejected(operation, "work_order_not_found", "document work order was not found", work_order_id=work_order_id)
        if status.get("knowledge_base_name") != self.context.knowledge_base_name:
            return self._rejected(operation, "document_context_kb_mismatch", "work order does not belong to the attached knowledge base", work_order_id=work_order_id)
        job = self.job_store.get_by_work_order(
            work_order_id,
            tenant_id=self.context.tenant_id,
            user_id=self.context.owner_user_id,
        )
        result = _safe_status(status)
        if job is not None:
            result["job"] = {
                "job_id": job.job_id, "status": job.status, "attempt": job.attempt,
                "last_error": job.last_error,
            }
        harness = status.get("harness_run") or {}
        result = DocumentToolResult(
            status="succeeded", operation=operation, message="document generation status loaded",
            task_id=status.get("task_id"), work_order_id=work_order_id,
            run_id=harness.get("run_id"), data=result,
            next_actions=list(result.get("next_actions") or []),
        )
        self._emit_card("work_order_status", result)
        return result

    def propose_document_plan(self, session_id: str) -> DocumentToolResult:
        """Compile and return the safe, hash-bound plan proposal (v2)."""
        operation = "propose_document_plan"
        self._authorize("write")
        if self.context.generation_session_id and session_id != self.context.generation_session_id:
            return self._rejected(operation, "document_context_session_mismatch", "session reference does not match the attached context", generation_session_id=session_id)
        expected_version = 0
        try:
            session = self.pipeline.get_document_generation_session(self.ctx, session_id)
            expected_version = int(getattr(session, "output_spec_version", 0) or 0)
        except PermissionError:
            raise
        except (KeyError, ValueError):
            return self._rejected(operation, "generation_session_not_found", "generation session was not found", generation_session_id=session_id)
        try:
            proposal = self.pipeline.create_document_plan_proposal(
                self.ctx,
                session_id,
                client_request_id=self._generation_client_request_id(session_id),
                expected_output_spec_version=expected_version,
            )
        except PermissionError:
            raise
        except KeyError:
            return self._rejected(operation, "generation_session_not_found", "generation session was not found", generation_session_id=session_id)
        except ValueError as exc:
            return self._rejected(
                operation,
                "plan_proposal_rejected",
                str(exc) or "document plan proposal was rejected",
                generation_session_id=session_id,
                next_actions=["answer_clarification"],
            )
        executable = bool(proposal.get("executable")) if isinstance(proposal, dict) else False
        safe_proposal = dict(proposal) if isinstance(proposal, dict) else {}
        result = DocumentToolResult(
            status="succeeded",
            operation=operation,
            message="document plan proposal compiled",
            generation_session_id=session_id,
            task_id=safe_proposal.get("task_id"),
            data={
                **safe_proposal,
                "executable": executable,
            },
            next_actions=(
                ["confirm_document_plan"] if executable else ["answer_clarification"]
            ),
        )
        self._emit_card("output_spec_confirmation", result)
        return result

    def confirm_document_plan(
        self,
        session_id: str,
        expected_output_spec_hash: str,
        expected_plan_hash: str,
        client_request_id: str,
    ) -> DocumentToolResult:
        """Explicit Gate 1 confirmation bound to the visible plan/spec hashes."""
        operation = "confirm_document_plan"
        self._authorize("write")
        if self.context.generation_session_id and session_id != self.context.generation_session_id:
            return self._rejected(operation, "document_context_session_mismatch", "session reference does not match the attached context", generation_session_id=session_id)
        spec_hash = str(expected_output_spec_hash or "").strip()
        plan_hash = str(expected_plan_hash or "").strip()
        request_key = str(client_request_id or "").strip()
        if not spec_hash or not plan_hash or not request_key:
            return self._rejected(
                operation,
                "confirmation_hashes_missing",
                "confirming a plan requires the exact output spec hash, plan hash and a client request id",
                generation_session_id=session_id,
                next_actions=["propose_document_plan", "confirm_document_plan"],
            )
        try:
            submission = self.pipeline.confirm_document_plan(
                self.ctx,
                session_id,
                expected_output_spec_hash=spec_hash,
                expected_plan_hash=plan_hash,
                client_request_id=request_key,
            )
        except PermissionError:
            raise
        except KeyError:
            return self._rejected(operation, "generation_session_not_found", "generation session was not found", generation_session_id=session_id)
        except ValueError as exc:
            return self._rejected(
                operation,
                "plan_confirmation_rejected",
                str(exc) or "document plan confirmation was rejected",
                generation_session_id=session_id,
                next_actions=["propose_document_plan"],
            )
        result = DocumentToolResult(
            status="succeeded",
            operation=operation,
            message="document plan confirmed; the submission worker will materialize the work order",
            generation_session_id=session_id,
            task_id=(submission.get("task_id") if isinstance(submission, dict) else None),
            work_order_id=(submission.get("work_order_id") if isinstance(submission, dict) else None),
            job_id=(submission.get("job_id") if isinstance(submission, dict) else None),
            data={
                "submission": submission,
                "status": submission.get("status") if isinstance(submission, dict) else None,
            },
            next_actions=["await_generation", "get_document_task_status"],
        )
        self._emit_card("output_spec_confirmation", result)
        return result

    def resolve_icd_scope_exception(
        self,
        action: Literal["include", "exclude"],
        comment: str,
        exception_ids: list[str] | None = None,
        refdes: list[str] | None = None,
        work_order_id: str | None = None,
    ) -> DocumentToolResult:
        """Resolve a non-blocking ICD scope gate from the conversation.

        Blocking exceptions (missing connector scope or EDF pin mapping) are
        refused with the operator instruction: they can only be fixed by
        completing the frozen sources or by restarting with an explicit target.
        """

        operation = "resolve_icd_scope_exception"
        self._authorize("write")
        order_id = str(work_order_id or "").strip() or self._context_work_order_id()
        if not order_id:
            return self._rejected(
                operation,
                "work_order_not_found",
                "当前会话没有可处理的文档工单",
                next_actions=["open_document_workbench"],
            )
        getter = getattr(self.pipeline, "get_icd_scope_review", None)
        if not callable(getter):
            return self._rejected(
                operation,
                "icd_scope_review_unavailable",
                "当前环境不支持对话处理 ICD 范围待办，请在文档工作台处理。",
                work_order_id=order_id,
                next_actions=["open_document_workbench"],
            )
        try:
            review = getter(self.ctx, order_id)
        except PermissionError:
            raise
        except (KeyError, ValueError) as exc:
            return self._rejected(
                operation,
                "icd_scope_review_unavailable",
                str(exc) or "ICD 范围待办当前不可用",
                work_order_id=order_id,
                next_actions=["open_document_workbench"],
            )
        if review is None:
            return self._rejected(
                operation,
                "icd_scope_review_not_found",
                "当前工单没有待处理的 ICD 范围异常",
                work_order_id=order_id,
                next_actions=["open_document_workbench"],
            )
        if str(getattr(review, "status", "") or "") == "frozen":
            # A previous attempt may have frozen the review and then failed to
            # queue generation; submission is idempotent, so retry it.
            return self._resume_scope_generation(operation, order_id)
        if action == "use_edf_connectors":
            rebuild = getattr(self.pipeline, "rebuild_icd_scope_from_edf", None)
            if not callable(rebuild):
                return self._rejected(
                    operation,
                    "edf_scope_rebuild_unavailable",
                    "当前环境不支持按 EDF 实际位号重建范围，请在文档工作台处理。",
                    work_order_id=order_id,
                    next_actions=["open_document_workbench"],
                )
            try:
                outcome = rebuild(
                    self.ctx, order_id, comment=str(comment or "").strip(),
                )
            except PermissionError:
                raise
            except (KeyError, ValueError) as exc:
                return self._rejected(
                    operation,
                    "edf_scope_rebuild_rejected",
                    str(exc) or "按 EDF 实际位号重建范围未被接受",
                    work_order_id=order_id,
                    next_actions=["open_document_workbench"],
                )
            run_id = outcome.get("run_id") if isinstance(outcome, dict) else None
            result = DocumentToolResult(
                status="succeeded",
                operation=operation,
                message="已按冻结 EDF 的实际位号替换模板示例位号，生成已继续。",
                work_order_id=order_id,
                run_id=str(run_id or "").strip() or None,
                data={
                    "status": (
                        outcome.get("status") if isinstance(outcome, dict) else None
                    ),
                    "run_id": str(run_id or "").strip() or None,
                    "refdes_source": "edf",
                },
                next_actions=["await_generation", "get_document_task_status"],
            )
            self._emit_card("work_order_status", result)
            return result
        exceptions = [
            item for item in list(getattr(review, "exceptions", None) or [])
        ]
        blocking = [
            item for item in exceptions
            if str(getattr(item, "kind", "") or "") in ICD_BLOCKING_SCOPE_EXCEPTION_KINDS
        ]
        if blocking:
            instructions = [
                str(getattr(item, "user_instruction", "") or "").strip()
                or f"位号 {getattr(item, 'refdes', '') or '未知'} 缺少模板/EDF 范围映射"
                for item in blocking
            ]
            return self._rejected(
                operation,
                "icd_scope_blocking",
                "该 ICD 范围异常不能直接包含/排除："
                + "；".join(instructions)
                + "。请补齐冻结来源（例如包含该位号的 EDF 管脚映射）并重新解析，"
                "或在对话中明确目标接插件位号后重新发起生成。",
                work_order_id=order_id,
                next_actions=["open_document_workbench"],
            )
        requested_refdes = {
            str(item).strip().casefold() for item in (refdes or []) if str(item).strip()
        }
        selected_ids = {
            str(item).strip() for item in (exception_ids or []) if str(item).strip()
        }
        if not selected_ids and requested_refdes:
            selected_ids = {
                str(getattr(item, "exception_id", "") or "").strip()
                for item in exceptions
                if str(getattr(item, "refdes", "") or "").strip().casefold()
                in requested_refdes
            }
        if not selected_ids:
            selected_ids = {
                str(getattr(item, "exception_id", "") or "").strip()
                for item in exceptions
            }
        resolutions = [
            {
                "exception_id": str(getattr(item, "exception_id", "") or "").strip(),
                "action": action,
            }
            for item in exceptions
            if str(getattr(item, "exception_id", "") or "").strip() in selected_ids
        ]
        if not resolutions:
            return self._rejected(
                operation,
                "icd_scope_exception_not_found",
                "没有匹配的 ICD 范围异常项",
                work_order_id=order_id,
            )
        resolver = getattr(self.pipeline, "submit_icd_scope_resolution", None)
        if not callable(resolver):
            return self._rejected(
                operation,
                "icd_scope_review_unavailable",
                "当前环境不支持对话处理 ICD 范围待办，请在文档工作台处理。",
                work_order_id=order_id,
                next_actions=["open_document_workbench"],
            )
        try:
            resolver(
                self.ctx,
                order_id,
                resolutions=resolutions,
                comment=str(comment or "").strip(),
            )
        except PermissionError:
            raise
        except (KeyError, ValueError) as exc:
            return self._rejected(
                operation,
                "icd_scope_resolution_rejected",
                str(exc) or "ICD 范围处理未被接受",
                work_order_id=order_id,
                next_actions=["open_document_workbench"],
            )
        return self._resume_scope_generation(
            operation,
            order_id,
            resolved_count=len(resolutions),
            action=action,
        )

    def _resume_scope_generation(
        self,
        operation: str,
        order_id: str,
        *,
        resolved_count: int = 0,
        action: str | None = None,
    ) -> DocumentToolResult:
        """Queue the frozen scope decision for generation, idempotently."""

        submitter = getattr(
            self.pipeline, "submit_knowledge_base_document_generation", None,
        )
        if not callable(submitter):
            return DocumentToolResult(
                status="waiting_human",
                operation=operation,
                message="ICD 范围已处理，但当前环境未启用自动重启生成；请在文档工作台继续生成。",
                work_order_id=order_id,
                data={
                    "status": "submission_unavailable",
                    "review_status": "frozen",
                    "resolved_count": resolved_count,
                },
                next_actions=["open_document_workbench"],
            )
        try:
            run_id = submitter(self.ctx, order_id)
        except PermissionError:
            raise
        except (KeyError, ValueError) as exc:
            return DocumentToolResult(
                status="waiting_human",
                operation=operation,
                error_code="icd_scope_submission_failed",
                message=(
                    "ICD 范围已处理，但生成任务重启失败："
                    + (str(exc).strip() or "请稍后重试")
                    + "；可回复“重试提交”或在文档工作台继续。"
                ),
                work_order_id=order_id,
                data={
                    "status": "submission_failed",
                    "review_status": "frozen",
                    "resolved_count": resolved_count,
                    "action": action,
                },
                next_actions=["resolve_icd_scope_exception", "open_document_workbench"],
            )
        result = DocumentToolResult(
            status="succeeded",
            operation=operation,
            message="ICD 范围异常已处理，生成已继续。",
            work_order_id=order_id,
            run_id=str(run_id or "").strip() or None,
            data={
                "status": "resumed",
                "review_status": "frozen",
                "resolved_count": resolved_count,
                "action": action,
                "run_id": str(run_id or "").strip() or None,
            },
            next_actions=["await_generation", "get_document_task_status"],
        )
        self._emit_card("work_order_status", result)
        return result

    def _context_work_order_id(self) -> str:
        """Resolve the current conversation task's work order, server-side."""

        task_id = str(getattr(self.context, "task_id", "") or "").strip()
        if not task_id:
            return ""
        getter = getattr(self.pipeline, "get_document_task_projection", None)
        if not callable(getter):
            return ""
        try:
            projection = getter(self.ctx, task_id)
        except (PermissionError, KeyError, ValueError):
            return ""
        if isinstance(projection, dict):
            return str(projection.get("work_order_id") or "").strip()
        return ""

    def get_document_task_status(self, task_id: str) -> DocumentToolResult:
        """Read the task aggregate by task id (planning + execution states)."""
        operation = "get_document_task_status"
        self._authorize("read")
        try:
            projection = self.pipeline.get_document_task_projection(self.ctx, str(task_id).strip())
        except PermissionError:
            raise
        except KeyError:
            return self._rejected(operation, "task_not_found", "document task was not found", task_id=task_id)
        except ValueError:
            return self._rejected(operation, "task_association_mismatch", "document task is bound to another execution lineage", task_id=task_id)
        if projection is None:
            return self._rejected(operation, "task_not_found", "document task was not found", task_id=task_id)
        return DocumentToolResult(
            status="succeeded",
            operation=operation,
            message="document task status loaded",
            task_id=str(task_id).strip(),
            data=projection,
            next_actions=list(projection.get("next_actions") or []),
        )

    def create_document_revision(
        self,
        task_id: str,
        parent_artifact_id: str,
        request_type: Literal["field_update", "section_update", "full_regeneration"],
        request: str,
        changed_fields: list[str] | None = None,
        changed_sections: list[str] | None = None,
        client_request_id: str = "",
    ) -> DocumentToolResult:
        """Record a task-bound revision request without fabricating bytes."""

        operation = "create_document_revision"
        self._authorize("write")
        normalized_task = str(task_id or "").strip()
        normalized_parent = str(parent_artifact_id or "").strip()
        if not normalized_task or not normalized_parent:
            return self._rejected(
                operation,
                "revision_identity_missing",
                "task_id and parent_artifact_id are required",
            )
        try:
            revision = self.pipeline.create_document_revision(
                self.ctx,
                normalized_task,
                parent_artifact_id=normalized_parent,
                request_type=request_type,
                request=str(request or "").strip(),
                changed_fields=list(changed_fields or []),
                changed_sections=list(changed_sections or []),
                client_request_id=str(client_request_id or "").strip(),
            )
        except PermissionError:
            raise
        except (ValueError, KeyError):
            return self._rejected(
                operation,
                "revision_rejected",
                "document revision request was rejected",
                task_id=normalized_task,
            )
        except Exception:
            return self._unavailable(
                operation,
                "document revision service is temporarily unavailable",
                task_id=normalized_task,
            )
        payload = (
            revision.model_dump(mode="json")
            if isinstance(revision, BaseModel)
            else dict(revision or {})
        )
        returned_task = str(payload.get("task_id") or "").strip()
        if returned_task and returned_task != normalized_task:
            return self._rejected(
                operation,
                "revision_task_mismatch",
                "revision does not belong to the requested task",
                task_id=normalized_task,
            )
        revision_id = str(payload.get("revision_id") or "").strip()
        if not revision_id:
            return self._unavailable(
                operation,
                "revision service did not return a revision identity",
                task_id=normalized_task,
            )
        regeneration = payload.get("regeneration") or {}
        regeneration_order = str(regeneration.get("work_order_id") or "").strip()
        result = DocumentToolResult(
            status="succeeded" if regeneration_order else "waiting_human",
            operation=operation,
            message=(
                "文档修订已受理：已创建新的受控生成工单并在后台执行，完成后需人工审核"
                if regeneration_order
                else "文档修订请求已记录；待受控执行器生成候选 Artifact 后再重新验证"
            ),
            task_id=normalized_task,
            work_order_id=(
                regeneration_order
                or str(payload.get("work_order_id") or "").strip()
            ) or None,
            data={"revision": payload, **payload},
            next_actions=(
                [
                    "get_document_generation_status",
                    "review_revision",
                    "open_document_workbench",
                ]
                if regeneration_order
                else ["review_revision", "open_document_workbench"]
            ),
        )
        self._emit_card("work_order_status", result)
        return result

    def as_tools(self) -> list[Any]:
        def wrap(name: str, description: str, args_schema: type[BaseModel], method):
            def call(**kwargs: Any) -> str:
                return _result_json(method(**kwargs))

            call.__name__ = name
            call.__doc__ = description
            return StructuredTool.from_function(
                call, name=name, description=description, args_schema=args_schema,
            )

        tools = [
            wrap("generate_document_from_template", "按当前已批准模板和知识库证据创建最终文档填充任务；不要改用对话导出。会自动复用当前会话已回答的澄清；后续补充信息时优先调用 answer_clarification，而不是再次调用本工具。", GenerateDocumentArgs, self.generate_document_from_template),
            wrap("get_document_template_analysis", "读取当前模板的结构化分析结果。", GetAnalysisArgs, self.get_document_template_analysis),
            wrap("start_document_generation_session", "开始当前模板的文档生成澄清会话。", StartSessionArgs, self.start_document_generation_session),
            wrap("answer_clarification", "回答当前文档生成会话的澄清问题。question_id 必须使用会话待答问题本身的 id；answer 必须原样使用服务器返回的候选项文本（用户自拟说法也优先映射到最近一次候选原文）。", AnswerClarificationArgs, self.answer_clarification),
            wrap("confirm_generation_session", "确认已完成澄清的文档生成会话。", ConfirmSessionArgs, self.confirm_generation_session),
            wrap("create_document_work_order", "创建异步文档生成工单。", CreateWorkOrderArgs, self.create_document_work_order),
            wrap("create_document_revision", "记录对现有文档 Artifact 的受控修订请求；不要声称修订文件已经生成。", CreateDocumentRevisionArgs, self.create_document_revision),
            wrap("get_document_generation_status", "读取文档生成工单状态。", GetStatusArgs, self.get_document_generation_status),
        ]
        if self._uses_v2_planning():
            # The low-level Work Order transition is owned by the submission
            # worker in v2; the model only proposes and confirms plans.
            tools = [tool for tool in tools if tool.name != "create_document_work_order"]
            tools.extend([
                wrap("propose_document_plan", "为已完成需求澄清的文档会话编译并返回计划提案（不创建工单）。", ProposePlanArgs, self.propose_document_plan),
                wrap("confirm_document_plan", "以当前可见的 spec/plan 哈希显式确认文档计划；确认后由后台 worker 创建工单。", ConfirmPlanArgs, self.confirm_document_plan),
                wrap("get_document_task_status", "按任务 ID 读取文档任务聚合状态。", TaskStatusArgs, self.get_document_task_status),
                wrap("resolve_icd_scope_exception", "处理当前工单的 ICD 范围异常待办：用户明确给出包含/排除指示时提交范围处理并继续生成；阻塞型异常（缺少接插件/EDF 映射）会返回需补齐来源或更换目标位号的说明。", ResolveIcdScopeArgs, self.resolve_icd_scope_exception),
            ])
        return tools


def make_document_authoring_tools(
    rt: Any,
    *,
    pipeline: Any,
    job_store: DocumentAuthoringJobStore | None = None,
    event_sink: Callable[[dict], None] | None = None,
) -> list[Any]:
    """Build document tools only for an already normalized server context."""

    context = rt.document_context
    if not isinstance(context, (DocumentContext, DocumentAuthoringContext)):
        return []
    return DocumentAuthoringToolset(
        pipeline=pipeline,
        ctx=rt.ctx,
        context=context,
        chat_session_id=str(getattr(rt, "chat_session_id", "") or getattr(rt.ctx, "session_id", "")),
        job_store=job_store or DocumentAuthoringJobStore(),
        event_sink=event_sink,
        attachment_refs=list(getattr(rt, "attachment_refs", None) or []),
        source_scope=str(getattr(rt, "source_scope", "knowledge_base_only") or "knowledge_base_only"),
    ).as_tools()


__all__ = [
    "DocumentAuthoringToolset",
    "DocumentToolResult",
    "make_document_authoring_tools",
]
