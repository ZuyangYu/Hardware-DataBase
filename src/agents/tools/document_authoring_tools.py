"""Controlled chat tools for the document-authoring workflow.

These tools are intentionally a thin, permission-checked adapter around
``AppPipeline``.  The typed methods are used by tests/coordinators; the
LangChain ``StructuredTool`` wrappers serialize the result only at the outer
ToolMessage boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from src.document_authoring.chat_context import DocumentContext
from src.document_authoring.job_store import DocumentAuthoringJobStore


class DocumentToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "rejected", "unavailable"]
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
    document_schema_id: str = Field(min_length=1, max_length=200)
    document_schema_version: str = Field(min_length=1, max_length=100)
    generation_session_id: str | None = Field(default=None, max_length=200)
    execution_mode: Literal["internal_harness", "deterministic_only", "external_agent"] = "internal_harness"


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
        # 只保留不可变引用(artifact_id/stage),其余字段(校验/策略状态)不下发到对话侧。
        "artifacts": [
            {"artifact_id": str(a.get("artifact_id")), "stage": str(a.get("stage"))}
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
        if self.event_sink is None or result.status != "succeeded":
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
        document_schema_id: str,
        document_schema_version: str,
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
                idempotency_key=self.context.client_request_id,
                generation_session_id=session_id,
                generation_brief=generation_brief,
                execution_mode=execution_mode,
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
    ).as_tools()


__all__ = [
    "DocumentAuthoringToolset",
    "DocumentToolResult",
    "make_document_authoring_tools",
]
