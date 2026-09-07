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

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from src.document_authoring.chat_context import DocumentContext
from src.document_authoring.job_store import DocumentAuthoringJobStore


class DocumentToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "rejected", "unavailable", "waiting_human"]
    operation: str
    error_code: str | None = None
    message: str = ""
    analysis_id: str | None = None
    template_version_id: str | None = None
    generation_session_id: str | None = None
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


def _safe_session(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "status": session.status,
        "template_version_id": session.template_version_id,
        "knowledge_base_name": session.knowledge_base_name,
        "work_order_id": session.work_order_id,
        "brief": session.brief.model_dump(mode="json"),
        "next_actions": _session_next_actions(session),
    }


def _session_next_actions(session: Any) -> list[str]:
    if session.status == "needs_clarification":
        return ["answer_clarification"]
    if session.status == "ready_to_generate":
        return ["create_document_work_order"]
    if session.status == "generating":
        return ["get_document_generation_status"]
    return []


def _safe_status(status: dict[str, Any]) -> dict[str, Any]:
    harness = status.get("harness_run") or {}
    return {
        "work_order_id": status.get("work_order_id"),
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
        "artifacts": [
            {
                "artifact_id": str(a.get("artifact_id")),
                "stage": str(a.get("stage")),
                **({"output_format": str(a.get("output_format"))} if str(a.get("output_format") or "").strip() else {}),
            }
            for a in (status.get("artifacts") or [])[:8]
        ],
    }


def _result_json(result: DocumentToolResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


@dataclass
class DocumentAuthoringToolset:
    pipeline: Any
    ctx: Any
    context: DocumentContext
    chat_session_id: str
    job_store: DocumentAuthoringJobStore
    event_sink: Callable[[dict], None] | None = None
    # These values are already server-resolved by the runner.  They are
    # carried into the work order so a worker can rebuild the evidence scope
    # without depending on the live chat turn.
    attachment_refs: list[Any] = field(default_factory=list)
    source_scope: str = "knowledge_base_only"

    def _authorize(self, permission: Literal["read", "write"]) -> None:
        self.context.assert_scope(
            ctx=self.ctx,
            expected_kb=self.context.knowledge_base_name,
            required_permission=permission,
        )
        # Every service call also receives the same server-derived KB scope.
        metadata = getattr(self.ctx, "metadata", None)
        if isinstance(metadata, dict):
            metadata["document_template_kb_name"] = self.context.knowledge_base_name

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
        kind: Literal["generation_session", "work_order_created", "work_order_status"],
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
        if result.generation_session_id:
            card["generation_session_id"] = result.generation_session_id
        if kind == "work_order_status":
            # 产物只带不可变引用(artifact_id/stage)与格式枚举;URL 由前端拼装。
            if result.data.get("target_format"):
                card["target_format"] = result.data.get("target_format")
            if result.data.get("artifacts"):
                card["artifacts"] = result.data.get("artifacts")
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
            return (
                template,
                None,
                None,
                self._rejected(
                    "generate_document_from_template",
                    "template_not_approved",
                    "template must be approved before generation; review or confirm the template mapping first",
                    template_version_id=template_id,
                    next_actions=["review_template_mapping", "confirm_document_template"],
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
        if stage != "ready":
            result = DocumentToolResult(
                status="waiting_human",
                operation=operation,
                message="模板已完成安全预检，但生成前仍需要人工处理待办",
                template_version_id=template_id,
                generation_session_id=session.session_id,
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
        result = DocumentToolResult(
            status="succeeded",
            operation=operation,
            message="模板填充任务已提交，完成后可下载最终文档",
            template_version_id=template_id,
            generation_session_id=session.session_id,
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
        template_id = str(template_version_id or self.context.template_version_id).strip()
        if template_id != self.context.template_version_id:
            return self._rejected(operation, "document_context_template_mismatch", "template reference does not match the attached context", template_version_id=template_id)
        try:
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
            template_version_id=template_id, generation_session_id=session.session_id,
            data=_safe_session(session), next_actions=_session_next_actions(session),
        )
        self._emit_card("generation_session", result)
        return result

    def answer_clarification(self, session_id: str, question_id: str, answer: str) -> DocumentToolResult:
        operation = "answer_clarification"
        self._authorize("write")
        if self.context.generation_session_id and session_id != self.context.generation_session_id:
            return self._rejected(operation, "document_context_session_mismatch", "session reference does not match the attached context", generation_session_id=session_id)
        try:
            session = self.pipeline.answer_document_generation_session(
                self.ctx, session_id, question_id=question_id, answer=answer,
            )
        except PermissionError:
            raise
        except (ValueError, KeyError):
            return self._rejected(operation, "clarification_rejected", "clarification answer was rejected", generation_session_id=session_id)
        if session.template_version_id != self.context.template_version_id:
            return self._rejected(operation, "document_context_template_mismatch", "session does not belong to the attached template", generation_session_id=session_id)
        result = DocumentToolResult(
            status="succeeded", operation=operation, message="clarification recorded",
            template_version_id=session.template_version_id, generation_session_id=session.session_id,
            data=_safe_session(session), next_actions=_session_next_actions(session),
        )
        self._emit_card("generation_session", result)
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
        result = DocumentToolResult(
            status="succeeded", operation=operation, message="document work order queued",
            template_version_id=template_id, generation_session_id=session_id,
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
            work_order_id=work_order_id, run_id=harness.get("run_id"), data=result,
            next_actions=list(result.get("next_actions") or []),
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

        return [
            wrap("generate_document_from_template", "按当前已批准模板和知识库证据创建最终文档填充任务；不要改用对话导出。", GenerateDocumentArgs, self.generate_document_from_template),
            wrap("get_document_template_analysis", "读取当前模板的结构化分析结果。", GetAnalysisArgs, self.get_document_template_analysis),
            wrap("start_document_generation_session", "开始当前模板的文档生成澄清会话。", StartSessionArgs, self.start_document_generation_session),
            wrap("answer_clarification", "回答当前文档生成会话的澄清问题。", AnswerClarificationArgs, self.answer_clarification),
            wrap("confirm_generation_session", "确认已完成澄清的文档生成会话。", ConfirmSessionArgs, self.confirm_generation_session),
            wrap("create_document_work_order", "创建异步文档生成工单。", CreateWorkOrderArgs, self.create_document_work_order),
            wrap("get_document_generation_status", "读取文档生成工单状态。", GetStatusArgs, self.get_document_generation_status),
        ]


def make_document_authoring_tools(
    rt: Any,
    *,
    pipeline: Any,
    job_store: DocumentAuthoringJobStore | None = None,
    event_sink: Callable[[dict], None] | None = None,
) -> list[Any]:
    """Build document tools only for an already normalized server context."""

    context = rt.document_context
    if not isinstance(context, DocumentContext):
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
