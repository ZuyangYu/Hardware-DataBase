"""P2a document-generation service.

This service owns the use-case transaction boundaries.  UI, a future REST API,
the background worker and external adapters call it instead of manipulating
templates, project stores or artifacts directly.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import sqlite3
import uuid
import zipfile
from copy import copy
from datetime import datetime, timezone
from collections.abc import Callable, Mapping, Sequence
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import requests

import src.settings
from src.agents.claim_evidence import RetrievalOutcome, RetrievalSourceOutcome
from src.document_authoring.artifact_preview import preview_artifact
from src.document_authoring.conversion import (
    TemplateArtifactConversionService,
    TemplateConversionError,
)
from src.document_authoring.deterministic_rules import DeterministicRuleExecutor
from src.document_authoring.harness.runtime import (
    InternalDocumentHarnessRuntime,
    _plan_field_execution_key,
)
from src.document_authoring.harness.idempotency import human_decision_key
from src.document_authoring.aggregation import DocumentAggregator
from src.document_authoring.icd_generation import (
    render_icd_front_views,
    render_icd_pin_table,
)
from src.document_authoring.icd_profile import classify_icd_template
from src.document_authoring.icd_validation import validate_icd_pin_set
from src.document_authoring.icd_scope_decision import (
    ICD_BLOCKING_SCOPE_EXCEPTION_KINDS,
    effective_frozen_pin_mappings,
)
from src.document_authoring.models import (
    AuthoringExecutionEvent,
    DeterministicRuleSpec,
    DocumentArtifact,
    DocumentHumanEvent,
    DocumentUnitDraft,
    HarnessPolicy,
    IcdScopeResolution,
    IcdScopeReview,
    KnowledgeBaseSourceSnapshot,
    LegacyTemplateClaim,
    DocumentSchema,
    DocumentWorkOrder,
    compute_input_fingerprint_v3,
    icd_scope_decision_hash,
    RendererPolicy,
    TemplateSanitizationReport,
    TemplateUnitBinding,
    TemplateVersion,
    DocxFill,
    DocxFillPlan,
    ValidationReport,
    WorkbookFill,
    WorkbookFillPlan,
    WorkbookTableFill,
    WorkbookTableRowFill,
    WorkbookRegionSchema,
    content_hash,
)
from src.document_authoring.ooxml import validate_ooxml_package
from src.document_authoring.renderers.docx import DocxRenderer
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.renderers.structured import (
    StructuredDocxRenderer,
    StructuredPdfRenderer,
    StructuredXlsxRenderer,
)
from src.document_authoring.planning.recipes import (
    StructureBindingCompiler,
    build_builtin_recipe_registry,
)
from src.document_authoring.render_bindings import (
    LegacyFillPlanAdapter,
    RenderBindingResolver,
)
from src.document_authoring.document_model import DocumentModel
from src.document_authoring.planning.document_review import (
    DocumentReleaseGate,
    DocumentReviewReport,
    DocumentReviewer,
    DocumentReworkRouter,
    ReleaseDecision,
    ReworkDecision,
)
from src.document_authoring.planning.coverage import CoverageEvaluator
from src.document_authoring.planning.unit_review import UnitReviewer
from src.document_authoring.planning.review_contracts import safe_review_projection
from src.document_authoring.template_activation import (
    TemplateActivationPolicy,
    decide_template_activation,
)
from src.document_authoring.template_analysis import (
    DocxRegionSchema,
    TemplateAnalysis,
    TemplateMappingCorrection,
    workbook_value_hash,
)
from src.document_authoring.template_analyzers import analyze_template
from src.document_authoring.template_progress import (
    TemplateProgress,
    TemplateProgressCallback,
    report_template_progress,
)
from src.document_authoring.template_sanitizer import sanitize_template
from src.document_authoring.template_suggester import (
    LLMTemplateSuggestionProvider,
    TemplateSuggestionProvider,
    TemplateSuggestionTechnicalFailure,
)
from src.document_authoring.tasks import DocumentTask, DocumentTaskService, DocumentTaskStore
from src.document_authoring.reviews import DocumentReviewStore
from src.document_authoring.revisions import ArtifactRevisionStore, DocumentRevisionService
from src.document_authoring.compatibility import (
    CompatibilityClosureError,
    DocumentAuthoringCompatibilityService,
)
from src.document_authoring.planning.legacy import legacy_brief_to_output_spec
from src.document_authoring.planning.service import DocumentPlanningService
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.worker import DocumentGenerationWorker
from src.document_authoring.writers.managed import (
    DeterministicEvidenceWriter,
    LLMManagedWriter,
    ManagedWriter,
    _column_semantics,
)
from src.pipelines.document_rag.schemas import RequestContext
from src.projects.service import ProjectService
from src.attachments.models import SOURCE_SCOPES
from src.observability.metrics import record_planning_shadow


logger = logging.getLogger(__name__)

_MAX_AUTO_HARNESS_UNITS = 500
_MAX_AUTO_HARNESS_RETRIEVAL_ROUNDS = 1_000
# max_steps = 2 + unit_count * (attempts + 4); with 500 units and attempts=2
# that is 3002, so the ceiling must leave headroom for the rewrite step.
_MAX_AUTO_HARNESS_STEPS = 3_600
_DEFAULT_RETRIEVAL_ATTEMPTS_PER_UNIT = 2

_ATTACHMENT_REF_SNAPSHOT_FIELDS = (
    "attachment_id", "asset_id", "session_id", "filename", "media_type",
    "extension", "size_bytes", "sha256", "usage_hint", "parse_status",
    "degraded_reason",
)


def _canonical_template_document_type(display_name: str, template_name: str) -> str:
    """Keep an uploaded filename out of the document-family contract."""
    supplied = str(display_name or "").strip()
    hint = " ".join((supplied, str(template_name or "").strip())).casefold()
    if re.search(r"(?:^|[^a-z0-9])icd(?:[^a-z0-9]|$)", hint) or "接口控制" in hint:
        return "icd"
    return supplied or str(template_name or "").strip()


def _ref_value(ref: Any, key: str, default: Any = None) -> Any:
    if isinstance(ref, Mapping):
        return ref.get(key, default)
    return getattr(ref, key, default)


def _freeze_attachment_ref_snapshot(refs: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Copy only server-issued attachment reference fields into a work order."""
    snapshot: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs or ():
        attachment_id = str(_ref_value(ref, "attachment_id", "") or "").strip()
        asset_id = str(_ref_value(ref, "asset_id", "") or "").strip()
        if not attachment_id or not asset_id:
            raise ValueError("document attachment references require attachment_id and asset_id")
        if attachment_id in seen:
            continue
        seen.add(attachment_id)
        item: dict[str, Any] = {}
        for field_name in _ATTACHMENT_REF_SNAPSHOT_FIELDS:
            value = _ref_value(ref, field_name, "")
            if field_name in {"session_id", "size_bytes"}:
                try:
                    value = int(value or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid document attachment reference {field_name}") from exc
            elif value is None:
                value = ""
            item[field_name] = value
        snapshot.append(item)
    return snapshot


def _resolve_document_source_scope(source_scope: Any, *, has_attachments: bool) -> str:
    requested = str(source_scope or "knowledge_base_only").strip()
    if requested not in SOURCE_SCOPES:
        raise ValueError(f"unsupported document source scope: {requested}")
    if requested == "auto":
        return "attachment_and_knowledge_base" if has_attachments else "knowledge_base_only"
    if requested == "attachment_only" and not has_attachments:
        raise ValueError("attachment_only document generation requires attachment references")
    if requested == "attachment_and_knowledge_base" and not has_attachments:
        return "knowledge_base_only"
    return requested


def _requires_human_review(
    unit_statuses: dict[str, str],
    *,
    auto_publish_verified: bool,
) -> bool:
    """Decide whether generation must stop for an operator.

    A missing optional field (``tbd``) does not make the rendered workbook
    unsafe.  In the explicit automatic-publication mode it remains visible in
    the task metadata, while the verified artifact can be delivered without a
    human gate.  All actual integrity, conflict, retrieval and evidence
    failures still require intervention.
    """
    blocking_statuses = {
        "requires_human", "blocked", "conflicting", "retrieval_failed",
        "insufficient_evidence",
    }
    if not auto_publish_verified:
        blocking_statuses.add("tbd")
    return any(status in blocking_statuses for status in unit_statuses.values())


def _automatic_release_allowed(order: DocumentWorkOrder, report: Any, *, requires_review: bool) -> bool:
    """Allow verified direct work orders as well as confirmed chat sessions.

    The upload-and-generate API is an explicit user action and may not have a
    clarification session attached.  When automatic publication is enabled,
    that path is equivalent to a confirmed session; safety still requires a
    passed report and no blocking unit status.
    """
    brief = getattr(order, "generation_brief", None)
    brief = brief if isinstance(brief, dict) else {}
    document_type = str(brief.get("document_type") or "").strip().casefold()
    if document_type in {
        "icd",
        "fpt",
        "requirements",
        "requirements_spec",
        "specification",
    }:
        # Controlled engineering documents always need an explicit release
        # signature. Their review candidate can still be downloaded as a
        # clearly identified draft by an authorized user.
        return False
    if not src.settings.DOCUMENT_AUTO_PUBLISH_VERIFIED or requires_review:
        return False
    # A revision regeneration is a requested change to a released document;
    # its candidate always passes the human release gate even when every
    # machine check is green.
    if getattr(order, "revision_id", None):
        return False
    if getattr(report, "status", None) != "passed":
        return False
    if not order.generation_session_id:
        return True
    return bool(order.generation_brief.get("confirmed"))


class TemplateRequiresHumanReview(ValueError):
    def __init__(
        self,
        *,
        analysis_id: str,
        template_version_id: str,
        reason_codes: list[str],
    ):
        self.analysis_id = analysis_id
        self.template_version_id = template_version_id
        self.reason_codes = list(reason_codes)
        reasons = ", ".join(self.reason_codes) or "unspecified_template_risk"
        super().__init__(
            "automatic template activation failed: analysis status is requires_human; "
            f"analysis_id={analysis_id}; reason_codes={reasons}"
        )


class DocumentGenerationService:
    def __init__(
        self,
        project_service: ProjectService | None = None,
        store: DocumentAuthoringStore | None = None,
        renderer: XlsmRenderer | None = None,
        worker: DocumentGenerationWorker | None = None,
        docx_renderer: DocxRenderer | None = None,
        suggestion_provider: TemplateSuggestionProvider | None = None,
    ):
        self.projects = project_service or ProjectService()
        self.store = store or DocumentAuthoringStore()
        self.planning = DocumentPlanningService(
            store=getattr(self.store, "planning", None),
        )
        self.workbook_renderer = renderer or XlsmRenderer()
        # Keep the existing public attribute for callers that supplied the
        # XLSX/XLSM renderer before DOCX support was introduced.
        self.renderer = self.workbook_renderer
        self.docx_renderer = docx_renderer or DocxRenderer()
        self.rules = DeterministicRuleExecutor()
        self.validator = DocumentValidator()
        self.document_aggregator = DocumentAggregator()
        self.render_binding_resolver = RenderBindingResolver()
        self.document_reviewer = DocumentReviewer()
        self.document_release_gate = DocumentReleaseGate()
        self.document_rework_router = DocumentReworkRouter()
        self.worker = worker or DocumentGenerationWorker()
        self.harness_runtime = InternalDocumentHarnessRuntime(self.store, self.validator)
        self.template_suggester = suggestion_provider or LLMTemplateSuggestionProvider()
        self.converter = TemplateArtifactConversionService()
        # DocumentTask shares the authoring database but owns only the
        # user-facing aggregate identity. WorkOrder, Job and Run retain their
        # existing execution responsibilities.
        self.task_service = DocumentTaskService(DocumentTaskStore(self.store.db_path))
        # Generic review state is a task-bound adapter over the existing
        # ICD/artifact review flows; it shares the authoring DB but has its
        # own command-idempotency namespace.
        self.review_store = DocumentReviewStore(self.store.db_path)
        # Compatibility events share the authoring database so rollout
        # counters and legacy classifications survive process restarts.
        self.compatibility = DocumentAuthoringCompatibilityService(
            db_path=self.store.db_path,
        )
        self.revision_service = DocumentRevisionService(
            authoring_store=self.store,
            task_store=self.task_service.store,
            revision_store=ArtifactRevisionStore(self.store.db_path),
            source_snapshot_resolver=self.resolve_source_snapshot,
        )

    def build_document_model(
        self,
        plan: Any,
        accepted_drafts: Mapping[str, DocumentUnitDraft] | Sequence[DocumentUnitDraft],
        reviews: Mapping[str, Any] | None = None,
    ) -> DocumentModel:
        """Aggregate accepted plan outputs before any format-specific render."""

        return self.document_aggregator.aggregate(plan, accepted_drafts, reviews)

    def build_plan_fill_plan(
        self,
        template: TemplateVersion,
        plan: Any,
        document_model: DocumentModel,
        *,
        bindings: Sequence[TemplateUnitBinding] | Mapping[str, TemplateUnitBinding] | None = None,
    ):
        """Resolve server-owned bindings and adapt a model for the legacy renderer."""

        registered = list(bindings) if bindings is not None else list(
            self.store.list_unit_bindings(
                template.template_schema_id, template.template_schema_version,
            )
        )
        contract = getattr(plan, "layout_contract", None)
        if contract is None and isinstance(plan, Mapping):
            contract = plan.get("layout_contract")
        if contract is None:
            raise ValueError("plan-backed document model requires a template layout contract")
        regions: Sequence[Any] = []
        if template.format == "docx":
            regions = list(self.store.list_docx_regions(
                template.template_schema_id, template.template_schema_version,
            ))
        else:
            regions = list(self.store.list_workbook_regions(
                template.template_schema_id, template.template_schema_version,
            ))
        self.render_binding_resolver.resolve(
            contract, document_model, registered, regions=regions,
        )
        return LegacyFillPlanAdapter.to_fill_plan(
            document_model,
            template_version_id=template.template_version_id,
            output_format=template.format,
            bindings=registered,
        )

    def review_document_pre_render(
        self,
        plan: Any,
        document_model: DocumentModel,
        coverage_report: Any,
        unit_reports: Mapping[str, Any] | Sequence[Any] | None,
    ) -> DocumentReviewReport:
        """Run the independent semantic gate before any binary renderer call."""

        return self.document_reviewer.pre_render(
            plan, document_model, coverage_report, unit_reports,
        )

    def review_document_post_render(
        self,
        plan: Any,
        document_model: DocumentModel,
        render_result: Any,
        artifact_bytes: bytes,
    ) -> DocumentReviewReport:
        """Run the independent physical-package gate for one artifact hash."""

        return self.document_reviewer.post_render(
            plan, document_model, render_result, artifact_bytes,
        )

    def evaluate_document_release(
        self,
        pre_render_report: DocumentReviewReport,
        post_render_report: DocumentReviewReport,
    ) -> ReleaseDecision:
        """Return a fail-closed decision bound to both review reports."""

        return self.document_release_gate.evaluate(pre_render_report, post_render_report)

    def route_document_rework(
        self,
        report: DocumentReviewReport,
        *,
        attempt: int,
        max_attempts: int | None = None,
    ) -> ReworkDecision:
        """Select a bounded semantic/layout route or human review."""

        router = self.document_rework_router
        if max_attempts is not None and max_attempts != router.max_attempts:
            router = DocumentReworkRouter(max_attempts=max_attempts)
        return router.route(report, attempt=attempt)

    @staticmethod
    def bind_document_review_manifest(
        manifest: Any,
        *,
        document_model: DocumentModel,
        pre_render_report: DocumentReviewReport,
        post_render_report: DocumentReviewReport,
        release_decision: ReleaseDecision,
    ) -> Any:
        """Bind review hashes to one run manifest without mutating old fields."""

        if pre_render_report.stage != "pre_render" or post_render_report.stage != "post_render":
            raise ValueError("run manifest requires one pre-render and one post-render report")
        if pre_render_report.model_hash != document_model.model_hash or post_render_report.model_hash != document_model.model_hash:
            raise ValueError("review reports are not bound to the document model")
        if release_decision.model_hash != document_model.model_hash:
            raise ValueError("release decision is not bound to the document model")
        if release_decision.pre_render_report_hash != pre_render_report.report_hash or release_decision.post_render_report_hash != post_render_report.report_hash:
            raise ValueError("release decision is not bound to the review reports")
        release_status = "released" if release_decision.release_allowed else release_decision.status
        update = {
            "document_model_hash": document_model.model_hash,
            "pre_render_review_hash": pre_render_report.report_hash,
            "post_render_review_hash": post_render_report.report_hash,
            "artifact_hash": post_render_report.artifact_hash,
            "release_decision_hash": content_hash(release_decision),
            "release_status": release_status,
        }
        if hasattr(manifest, "model_copy"):
            return manifest.model_copy(update=update)
        if isinstance(manifest, Mapping):
            return {**manifest, **update}
        raise TypeError("run manifest must be a mapping or model_copy-compatible object")

    @staticmethod
    def _shadow_issue_codes(plan: Any) -> list[str]:
        """Return bounded issue codes suitable for an audit event/metric."""
        safe_code = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
        codes: list[str] = []
        for issue in getattr(plan, "issues", []) or []:
            if not getattr(issue, "blocking", False):
                continue
            code = str(getattr(issue, "code", "other") or "other").strip().lower()
            codes.append(code if safe_code.fullmatch(code) else "other")
        return list(dict.fromkeys(codes))

    def _run_shadow_planning(
        self,
        ctx: RequestContext,
        order: DocumentWorkOrder,
        *,
        snapshot: Any | None = None,
        schema: DocumentSchema | None = None,
        template: TemplateVersion | None = None,
        template_analysis: TemplateAnalysis | None = None,
        bindings: Sequence[TemplateUnitBinding] | None = None,
    ) -> Any | None:
        """Compile and persist a non-executing plan for an existing WorkOrder.

        This method is deliberately called after the legacy WorkOrder is
        durable.  Every exception is converted into a sanitized planning event
        and a metric; no execution state, queue entry or renderer input is
        touched by the shadow path.
        """
        if not getattr(src.settings, "DOCUMENT_PLANNING_SHADOW_ENABLED", False):
            return None
        task_id = str(getattr(order, "task_id", "") or "").strip()
        if not task_id:
            # Legacy rows created while task writes were disabled have no safe
            # owner for the planning tables, so leave them entirely unchanged.
            return None
        started = perf_counter()
        planning = getattr(self, "planning", None)
        if planning is None:
            planning = DocumentPlanningService(
                store=getattr(getattr(self, "store", None), "planning", None),
            )
        planning_store = getattr(planning, "store", None)
        if planning_store is None:
            return None
        event_key = f"planning-shadow:{order.work_order_id}:v1"
        try:
            frozen_snapshot = snapshot or self.resolve_source_snapshot(order)
            document_schema = schema or self._schema(
                order.document_schema_id, order.document_schema_version,
            )
            document_template = template or self._template(order.template_version_id)
            analysis = template_analysis
            backing_store = getattr(self, "store", None)
            if analysis is None and backing_store is not None:
                analysis = backing_store.get_template_analysis(order.template_version_id)
            if bindings is not None:
                physical_bindings = list(bindings)
            elif backing_store is not None:
                physical_bindings = list(
                    backing_store.list_unit_bindings(
                        order.template_schema_id, order.template_schema_version,
                    )
                )
            else:
                physical_bindings = []
            output_spec = legacy_brief_to_output_spec(
                getattr(order, "generation_brief", {}) or {},
                template_version_id=document_template.template_version_id,
                template_schema_id=document_template.template_schema_id,
                template_schema_version=document_template.template_schema_version,
                document_type=document_schema.document_type,
                target_format=order.target_format,
                output_spec_id=f"shadow-output:{order.work_order_id}",
                output_spec_version=1,
                document_schema=document_schema,
            )
            plan = planning.propose_shadow(
                tenant_id=str(getattr(order, "tenant_id", None) or "default"),
                user_id=str(
                    getattr(ctx, "user_id", None)
                    or getattr(order, "created_by", None)
                    or "system"
                ),
                task_id=task_id,
                output_spec=output_spec,
                document_schema=document_schema,
                template_analysis=analysis,
                bindings=physical_bindings,
                source_snapshot_id=str(frozen_snapshot.source_set_snapshot_id),
                source_snapshot_hash=str(frozen_snapshot.content_hash),
            )
            issue_codes = self._shadow_issue_codes(plan)
            blocking_count = sum(
                1 for issue in (getattr(plan, "issues", []) or [])
                if getattr(issue, "blocking", False)
            )
            validity = "valid" if bool(getattr(plan, "is_executable", False)) else "invalid"
            payload = {
                "plan_status": str(getattr(plan, "status", "proposed")),
                "validity": validity,
                "blocking_issue_count": blocking_count,
                "blocking_issue_codes": issue_codes,
                "unit_count": len(getattr(plan, "semantic_units", []) or []),
                "table_count": sum(
                    1 for unit in (getattr(plan, "semantic_units", []) or [])
                    if getattr(unit, "kind", None) == "table"
                ),
            }
            planning_store.append_event(
                task_id=task_id,
                event_type="planning_shadow_succeeded",
                idempotency_key=event_key,
                payload=payload,
            )
            record_planning_shadow(
                status="succeeded",
                validity=validity,
                blocking_issue_count=blocking_count,
                blocking_issue_codes=issue_codes,
                units=payload["unit_count"],
                tables=payload["table_count"],
                duration_s=perf_counter() - started,
            )
            return plan
        except Exception:
            # Deliberately omit exception text, source names, paths and
            # evidence from both the event and telemetry.  The legacy caller
            # receives its original result regardless of compiler failure.
            try:
                planning_store.append_event(
                    task_id=task_id,
                    event_type="planning_shadow_failed",
                    idempotency_key=event_key,
                    payload={
                        "plan_status": "unavailable",
                        "validity": "invalid",
                        "blocking_issue_count": 1,
                        "blocking_issue_codes": ["shadow_compiler_failed"],
                        "unit_count": 0,
                        "table_count": 0,
                    },
                )
            except Exception:
                pass
            record_planning_shadow(
                status="failed",
                validity="invalid",
                blocking_issue_count=1,
                blocking_issue_codes=["shadow_compiler_failed"],
                units=0,
                tables=0,
                duration_s=perf_counter() - started,
            )
            logger.warning(
                "document planning shadow failed for work order %s",
                str(getattr(order, "work_order_id", "")),
            )
            return None

    def ensure_document_task(
        self,
        ctx: RequestContext,
        *,
        template_version_id: str | None = None,
        generation_session_id: str | None = None,
        project_id: str | None = None,
        knowledge_base_name: str | None = None,
        idempotency_key: str | None = None,
        status: str = "planned",
    ) -> DocumentTask | None:
        """Create or reuse the user-level task for a generation request."""
        if not getattr(src.settings, "DOCUMENT_TASK_WRITE_ENABLED", True):
            return None
        return self.task_service.ensure_task(
            ctx,
            template_version_id=template_version_id,
            generation_session_id=generation_session_id,
            project_id=project_id,
            knowledge_base_name=knowledge_base_name,
            idempotency_key=idempotency_key,
            status=status,
        )

    def mark_document_task_queued(self, task_id: str | None) -> DocumentTask | None:
        """Project successful queue submission to the user-level task."""
        normalized = str(task_id or "").strip()
        if not normalized or not getattr(src.settings, "DOCUMENT_TASK_WRITE_ENABLED", True):
            return None
        try:
            return self.task_service.store.update_status(normalized, "queued")
        except Exception:
            logger.warning(
                "failed to mark DocumentTask %s as queued",
                normalized,
                exc_info=True,
            )
            return None

    @staticmethod
    def _require_template_kb_scope(
        ctx: RequestContext,
        template: TemplateVersion,
        required_permission: str,
    ) -> None:
        """Enforce the API-bound KB scope for template review, correction, and activation."""
        requested_kb = ctx.metadata.get("document_template_kb_name")
        if requested_kb is None:
            return
        requested_department_id = ctx.metadata.get("resource_department_id")
        requested_kb_id = ctx.metadata.get("kb_id")
        if (
            template.knowledge_base_name != requested_kb
            or template.tenant_id != (ctx.tenant_id or "default")
            or template.resource_department_id is None
            or template.knowledge_base_id is None
            or template.resource_department_id != requested_department_id
            or template.knowledge_base_id != requested_kb_id
            or not ctx.has_kb_permission(requested_kb, required_permission)
        ):
            raise PermissionError("template does not belong to the selected knowledge base")

    # Template and schema registration -------------------------------------------------

    def register_renderer_policy(self, policy: RendererPolicy) -> RendererPolicy:
        return self.store.save_renderer_policy(policy)

    def register_document_schema(
        self,
        schema: DocumentSchema,
        *,
        plan_backed: bool = False,
        output_spec: Any | None = None,
        document_plan: Any | None = None,
        references: Mapping[str, Any] | None = None,
    ) -> DocumentSchema:
        """Persist a schema, subject to the Phase 5 direct-write closure."""
        if plan_backed:
            references_value = self.compatibility.accepted_plan_references(
                output_spec=output_spec,
                document_plan=document_plan,
                references=references,
            )
            self.compatibility.record_plan_backed_execution(
                operation="register_document_schema",
                entity_type="document_schema",
                entity_id=schema.document_schema_id,
                plan_id=references_value.document_plan_id,
                plan_version=references_value.document_plan_version,
                plan_hash=references_value.document_plan_hash,
            )
            self.compatibility.record_new_write(
                operation="register_document_schema",
                entity_type="document_schema",
                entity_id=schema.document_schema_id,
                plan_id=references_value.document_plan_id,
                plan_version=references_value.document_plan_version,
                plan_hash=references_value.document_plan_hash,
                route="plan_backed",
            )
        else:
            if self.compatibility.closure_enabled:
                raise CompatibilityClosureError(
                    "direct document schema writes are closed; use an accepted document plan"
                )
            self.compatibility.record_legacy_write(
                operation="register_document_schema",
                entity_type="document_schema",
                entity_id=schema.document_schema_id,
            )
        return self.store.save_document_schema(schema, allow_plan_backed=plan_backed)

    def register_deterministic_rule(self, spec: DeterministicRuleSpec) -> DeterministicRuleSpec:
        return self.store.save_rule_spec(spec)

    def register_harness_policy(self, policy: HarnessPolicy) -> HarnessPolicy:
        return self.store.save_harness_policy(policy)

    def _record_compatibility_work_order_write(
        self,
        ctx: RequestContext,
        *,
        operation: str,
        entity_id: str,
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        output_spec_hash: str | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        document_plan_hash: str | None = None,
        output_spec: Any | None = None,
        document_plan: Any | None = None,
    ) -> None:
        """Classify a new WorkOrder write and enforce Phase 5 closure."""
        supplied = any(value is not None for value in (
            output_spec_id, output_spec_version, output_spec_hash,
            document_plan_id, document_plan_version, document_plan_hash,
        )) or output_spec is not None or document_plan is not None
        if supplied and (output_spec is None or document_plan is None):
            planning_store = getattr(self.planning, "store", None)
            if planning_store is None:
                raise CompatibilityClosureError(
                    "plan-backed WorkOrder writes require the planning store"
                )
            output_spec = planning_store.get_output_spec(
                str(output_spec_id or ""), int(output_spec_version or 0),
                tenant_id=ctx.tenant_id or "default", user_id=ctx.user_id,
            )
            document_plan = planning_store.get_plan(
                str(document_plan_id or ""), int(document_plan_version or 0),
                tenant_id=ctx.tenant_id or "default", user_id=ctx.user_id,
            )
            if output_spec is None or document_plan is None:
                raise CompatibilityClosureError(
                    "plan-backed WorkOrder references do not resolve to persisted rows"
                )
        refs = self.compatibility.validate_new_document_write(
            operation=operation,
            output_spec=output_spec,
            document_plan=document_plan,
        )
        if refs is not None:
            self.compatibility.record_plan_backed_execution(
                operation=operation,
                tenant_id=ctx.tenant_id or "default",
                entity_type="work_order",
                entity_id=entity_id,
                plan_id=refs.document_plan_id,
                plan_version=refs.document_plan_version,
                plan_hash=refs.document_plan_hash,
            )
            self.compatibility.record_new_write(
                operation=operation,
                tenant_id=ctx.tenant_id or "default",
                entity_type="work_order",
                entity_id=entity_id,
                plan_id=refs.document_plan_id,
                plan_version=refs.document_plan_version,
                plan_hash=refs.document_plan_hash,
            )
        else:
            self.compatibility.record_legacy_direct_execution(
                operation=operation,
                tenant_id=ctx.tenant_id or "default",
                entity_type="work_order",
                entity_id=entity_id,
            )
            self.compatibility.record_legacy_write(
                operation=operation,
                tenant_id=ctx.tenant_id or "default",
                entity_type="work_order",
                entity_id=entity_id,
            )

    def _validate_compatibility_work_order_inputs(
        self,
        ctx: RequestContext,
        *,
        operation: str,
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        output_spec_hash: str | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        document_plan_hash: str | None = None,
    ) -> None:
        """Run the closure check before creating any source snapshot rows."""
        supplied = any(value is not None for value in (
            output_spec_id, output_spec_version, output_spec_hash,
            document_plan_id, document_plan_version, document_plan_hash,
        ))
        if not supplied:
            self.compatibility.validate_new_document_write(operation=operation)
            return
        planning_store = getattr(self.planning, "store", None)
        if planning_store is None:
            raise CompatibilityClosureError("plan-backed WorkOrder writes require the planning store")
        output_spec = planning_store.get_output_spec(
            str(output_spec_id or ""), int(output_spec_version or 0),
            tenant_id=ctx.tenant_id or "default", user_id=ctx.user_id,
        )
        document_plan = planning_store.get_plan(
            str(document_plan_id or ""), int(document_plan_version or 0),
            tenant_id=ctx.tenant_id or "default", user_id=ctx.user_id,
        )
        if output_spec is None or document_plan is None:
            raise CompatibilityClosureError(
                "plan-backed WorkOrder references do not resolve to persisted rows"
            )
        self.compatibility.validate_new_document_write(
            operation=operation,
            output_spec=output_spec,
            document_plan=document_plan,
        )

    def register_template(
        self,
        template: TemplateVersion,
        content: bytes,
        *,
        regions: list[WorkbookRegionSchema] | list[DocxRegionSchema],
        bindings: list[TemplateUnitBinding],
        legacy_claims: list[LegacyTemplateClaim] | None = None,
    ) -> TemplateVersion:
        actual_hash = hashlib.sha256(content).hexdigest()
        if template.content_hash != actual_hash:
            raise ValueError("template content hash does not match supplied bytes")
        if template.format not in {"xlsm", "xlsx", "docx"}:
            raise NotImplementedError(f"controlled renderer does not support {template.format.upper()}")
        if template.format == "docx":
            if not all(isinstance(region, DocxRegionSchema) for region in regions):
                raise TypeError("DOCX templates require DocxRegionSchema regions")
        elif not all(isinstance(region, WorkbookRegionSchema) for region in regions):
            raise TypeError("XLSX/XLSM templates require WorkbookRegionSchema regions")
        if template.format != "docx":
            with zipfile.ZipFile(io.BytesIO(content), "r") as package:
                worksheet_map = self.workbook_renderer._worksheet_part_map(package)
                shared_strings = (
                    self.workbook_renderer._shared_strings(
                        package.read("xl/sharedStrings.xml")
                    )
                    if "xl/sharedStrings.xml" in package.namelist()
                    else []
                )
                regions = [
                    region
                    if region.expected_value_hash is not None
                    else region.model_copy(update={
                        "expected_value_hash": workbook_value_hash(
                            self.workbook_renderer._cell_value(
                                package.read(worksheet_map[region.sheet_name]),
                                str(region.locator["cell"]).upper(),
                                shared_strings,
                            )
                        ),
                    })
                    for region in regions
                ]
        seen_regions = {region.region_id for region in regions}
        if len(seen_regions) != len(regions):
            raise ValueError("template region ids must be unique")
        for binding in bindings:
            if binding.table_schema is not None:
                if template.format == "docx" or binding.semantic_unit_id != binding.table_schema.semantic_unit_id:
                    raise ValueError("table binding does not match workbook semantic unit")
                if binding.target_region_ids != [binding.table_schema.table_region_id]:
                    raise ValueError("table binding must reference its exact table region")
                if binding.table_schema.table_region_id in seen_regions:
                    raise ValueError("duplicate table region id")
                seen_regions.add(binding.table_schema.table_region_id)
        for binding in bindings:
            if binding.template_schema_id != template.template_schema_id or binding.template_schema_version != template.template_schema_version:
                raise ValueError("template unit binding schema version mismatch")
            if not set(binding.target_region_ids) <= seen_regions:
                raise ValueError(f"binding references unknown regions: {binding.binding_id}")
        report = self.docx_renderer.inspect(content) if template.format == "docx" else self.workbook_renderer.inspect(content, template.format)
        saved = self.store.save_template(template, content, report)
        if template.format == "docx":
            self.store.save_docx_regions(template.template_schema_id, template.template_schema_version, regions)
        else:
            self.store.save_workbook_regions(template.template_schema_id, template.template_schema_version, regions)
        self.store.save_unit_bindings(bindings)
        self.store.save_legacy_template_claims(template.template_version_id, legacy_claims or [])
        return saved

    def approve_template(self, template_version_id: str, actor_id: str) -> TemplateVersion:
        template = self._template(template_version_id)
        policy = self._policy(template)
        report = self.store.get_template_security_report(template_version_id)
        if report is None:
            raise ValueError("template security report is missing")
        # Approval is explicit and hash-bound.  An active template can only be
        # approved if its exact content hash is on the renderer policy allowlist.
        if report.active_content_status != "clean":
            self.renderer._validate_active_content(report, policy, security_approved=True)
        approved = template.model_copy(update={
            "status": "approved", "approved_by": actor_id,
            "approved_at": datetime.now(timezone.utc),
        })
        return self.store.replace_template(approved)

    def analyze_uploaded_template(
        self,
        ctx: RequestContext,
        *,
        filename: str,
        content: bytes,
        template_name: str,
        progress_callback: TemplateProgressCallback | None = None,
        origin_source_type: str | None = None,
        origin_attachment_id: str | None = None,
        origin_session_id: int | None = None,
        origin_content_hash: str | None = None,
    ) -> TemplateAnalysis:
        """Persist an immutable draft then analyze its structure, never its bytes by LLM."""
        report_template_progress(progress_callback, TemplateProgress(stage="upload_started"))
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in {"xlsx", "xlsm", "docx"}:
            raise ValueError("template upload supports only .xlsx, .xlsm, and .docx files")
        if not isinstance(content, bytes) or not content:
            raise ValueError("template content must be non-empty bytes")
        source_digest = hashlib.sha256(content).hexdigest()
        template_version_id = f"template-{uuid.uuid4().hex}"
        template_schema_id = f"template-schema-{uuid.uuid4().hex}"
        sanitized = sanitize_template(content, suffix)
        digest = hashlib.sha256(sanitized.content).hexdigest()
        report = self._inspect(sanitized.content, sanitized.format)
        if report.active_content_status != "clean":
            raise ValueError("sanitized template still contains active content")
        # Security findings are content-addressed, but each immutable template
        # version owns its own report row and confirmation lifecycle.
        report = report.model_copy(update={"report_id": f"template-security-{template_version_id}"})
        policy = RendererPolicy(
            renderer_policy_id=f"renderer-{template_version_id}",
            version="1",
            macro_policy="strip",
            external_link_policy="strip",
            embedded_object_policy="strip",
            allowlisted_template_hashes=[],
            allowed_changed_parts=["word/document.xml"] if sanitized.format == "docx" else ["xl/worksheets/"],
        )
        self.store.save_renderer_policy(policy)
        template = TemplateVersion(
            template_version_id=template_version_id,
            template_id=template_name.strip() or filename,
            format=sanitized.format,
            content_hash=digest,
            template_schema_id=template_schema_id,
            template_schema_version="1",
            renderer_policy_id=policy.renderer_policy_id,
            tenant_id=ctx.tenant_id or "default",
            knowledge_base_name=ctx.metadata.get("document_template_kb_name"),
            resource_department_id=ctx.metadata.get("resource_department_id"),
            knowledge_base_id=ctx.metadata.get("kb_id"),
            origin_source_type=origin_source_type,
            origin_attachment_id=origin_attachment_id,
            origin_session_id=origin_session_id,
            origin_content_hash=origin_content_hash,
        )
        sanitization_report = TemplateSanitizationReport(
            template_version_id=template_version_id,
            source_format=suffix,
            source_content_hash=source_digest,
            source_storage_ref="",
            sanitized_format=sanitized.format,
            sanitized_content_hash=digest,
            removed_parts=sanitized.removed_parts,
            removed_relationships=sanitized.removed_relationships,
            status="sanitized",
        )
        template = self.store.save_sanitized_template(
            template,
            content,
            suffix,
            sanitized.content,
            report,
            sanitization_report,
        )
        report_template_progress(
            progress_callback,
            TemplateProgress(stage="sanitization_completed", template_version_id=template.template_version_id),
        )
        analysis = analyze_template(sanitized.content, sanitized.format).model_copy(update={
            "analysis_id": f"analysis-{uuid.uuid4().hex}",
            "template_version_id": template.template_version_id,
        })
        report_template_progress(
            progress_callback,
            TemplateProgress(
                stage="structure_analysis_completed",
                template_version_id=template.template_version_id,
                unit_count=len(analysis.units),
                writable_unit_count=sum(unit.writable for unit in analysis.units),
            ),
        )
        try:
            if isinstance(self.template_suggester, LLMTemplateSuggestionProvider):
                self.template_suggester.suggest(analysis, progress_callback=progress_callback)
            else:
                self.template_suggester.suggest(analysis)
            analysis.validate_suggestions()
            activation_decision = decide_template_activation(
                analysis,
                TemplateActivationPolicy(
                    accept_ai_recommendations=(
                        src.settings.DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS
                    ),
                ),
            )
            analysis = analysis.model_copy(update={
                "activation_decision": activation_decision,
                "status": (
                    "ready_for_confirmation"
                    if activation_decision.status == "auto_accepted"
                    else "requires_human"
                ),
            })
        except (TemplateSuggestionTechnicalFailure, requests.RequestException) as exc:
            logger.exception(
                "Template suggestion technical failure; preserving the sanitized draft for audit: "
                "template_version_id=%s error_type=%s",
                template.template_version_id,
                type(exc).__name__,
            )
            analysis.status = "failed"
            analysis.suggestions = []
            self.store.save_template_analysis(analysis)
            report_template_progress(
                progress_callback,
                TemplateProgress(
                    stage="analysis_failed",
                    template_version_id=template.template_version_id,
                    error_type=type(exc).__name__,
                ),
            )
            raise TemplateSuggestionTechnicalFailure(
                "automatic template upload failed",
                template_version_id=template.template_version_id,
            ) from exc
        except Exception:
            logger.exception(
                "Template suggestion analysis failed; preserving the sanitized draft for audit: template_version_id=%s",
                template.template_version_id,
            )
            analysis.status = "requires_human"
            analysis.suggestions = []
            analysis.activation_decision = decide_template_activation(analysis)
        saved_analysis = self.store.save_template_analysis(analysis)
        report_template_progress(
            progress_callback,
            TemplateProgress(stage="analysis_persisted", template_version_id=template.template_version_id),
        )
        return saved_analysis

    def analyze_and_activate_uploaded_template(
        self,
        ctx: RequestContext,
        *,
        filename: str,
        content: bytes,
        template_name: str,
        progress_callback: TemplateProgressCallback | None = None,
    ) -> TemplateVersion:
        """Persist, analyze, and activate a template without a manual confirmation step.

        Failed analyses remain stored as draft records for audit, but only a
        fully validated analysis may activate a template.
        """
        try:
            analysis = self.analyze_uploaded_template(
                ctx,
                filename=filename,
                content=content,
                template_name=template_name,
                progress_callback=progress_callback,
            )
        except TemplateSuggestionTechnicalFailure as exc:
            report_template_progress(
                progress_callback,
                TemplateProgress(
                    stage="activation_failed",
                    template_version_id=exc.template_version_id,
                    error_type=type(exc).__name__,
                ),
            )
            raise
        if (
            analysis.status != "ready_for_confirmation"
            or analysis.activation_decision is None
            or analysis.activation_decision.status != "auto_accepted"
        ):
            report_template_progress(
                progress_callback,
                TemplateProgress(
                    stage="activation_failed",
                    template_version_id=analysis.template_version_id,
                    error_type="AnalysisNotReady",
                ),
            )
            if analysis.status == "requires_human":
                raise TemplateRequiresHumanReview(
                    analysis_id=analysis.analysis_id,
                    template_version_id=analysis.template_version_id,
                    reason_codes=(
                        analysis.activation_decision.reason_codes
                        if analysis.activation_decision is not None
                        else []
                    ),
                )
            raise ValueError(
                f"automatic template activation failed: analysis status is {analysis.status}"
            )
        report_template_progress(
            progress_callback,
            TemplateProgress(stage="activation_started", template_version_id=analysis.template_version_id),
        )
        try:
            template = self.confirm_template_analysis(
                ctx,
                analysis_id=analysis.analysis_id,
                display_name=template_name.strip() or filename,
            )
        except Exception as exc:
            report_template_progress(
                progress_callback,
                TemplateProgress(
                    stage="activation_failed",
                    template_version_id=analysis.template_version_id,
                    error_type=type(exc).__name__,
                ),
            )
            raise
        report_template_progress(
            progress_callback,
            TemplateProgress(stage="activation_completed", template_version_id=template.template_version_id),
        )
        return template

    def get_template_sanitization_summary(
        self,
        ctx: RequestContext,
        template_version_id: str,
    ) -> dict[str, int | str] | None:
        """Return only safe aggregate sanitization results to application callers."""
        if not ctx.user_id or ctx.user_id == "anonymous":
            raise PermissionError("authenticated user is required to view template sanitization results")
        report = self._get_template_sanitization_report(template_version_id)
        if report is None:
            return None
        removed_parts = [part.lower() for part in report.removed_parts]
        counts = {
            "已移除宏": sum("vba" in part for part in removed_parts),
            "已移除外链": sum("externallink" in part for part in removed_parts),
            "已移除嵌入/控件": sum(
                any(marker in part for marker in ("embedding", "ole", "activex", "control", "ctrlprop"))
                for part in removed_parts
            ),
        }
        displayed_counts = counts if not any(counts.values()) else {
            label: count for label, count in counts.items() if count
        }
        return {**displayed_counts, "安全模板格式": report.sanitized_format}

    def _get_template_sanitization_report(self, template_version_id: str) -> TemplateSanitizationReport | None:
        """Read the complete audit record only within the document-authoring service."""
        return self.store.get_template_sanitization_report(template_version_id)

    def get_template_analysis_for_review(
        self,
        ctx: RequestContext,
        *,
        analysis_id: str,
    ) -> TemplateAnalysis:
        if not ctx.user_id or ctx.user_id == "anonymous":
            raise PermissionError("authenticated user is required to review a template analysis")
        analysis = self.store.get_template_analysis_by_id(analysis_id)
        if analysis is None:
            raise KeyError(f"template analysis not found: {analysis_id}")
        self._require_template_kb_scope(ctx, self._template(analysis.template_version_id), "read")
        return analysis

    def correct_template_analysis(
        self,
        ctx: RequestContext,
        *,
        correction: TemplateMappingCorrection,
    ) -> TemplateAnalysis:
        if not ctx.user_id or ctx.user_id == "anonymous":
            raise PermissionError("authenticated user is required to correct a template analysis")
        if correction.actor_id != ctx.user_id:
            raise PermissionError("template correction actor does not match request context")
        analysis = self.store.get_template_analysis_by_id(correction.analysis_id)
        if analysis is None:
            raise KeyError(f"template analysis not found: {correction.analysis_id}")
        self._require_template_kb_scope(ctx, self._template(analysis.template_version_id), "write")
        current = self.store.get_template_analysis(analysis.template_version_id)
        if current is None or current.analysis_id != analysis.analysis_id:
            raise ValueError("template analysis correction is stale")
        template = self._template(analysis.template_version_id)
        self._require_template_kb_scope(ctx, template, "write")
        actual_hash = hashlib.sha256(
            self.store.read_template_content(template.template_version_id)
        ).hexdigest()
        if (
            correction.expected_content_hash != analysis.content_hash
            or actual_hash != analysis.content_hash
            or actual_hash != template.content_hash
        ):
            raise ValueError("template correction content hash does not match template content hash")

        unit_ids = {unit.unit_id for unit in analysis.units}
        target_ids = {
            unit_id
            for suggestion in correction.suggestions
            for unit_id in suggestion.target_unit_ids
        }
        locked_ids = set(correction.locked_unit_ids)
        overwrite_ids = set(correction.approved_overwrite_unit_ids)
        unknown_ids = (target_ids | locked_ids | overwrite_ids) - unit_ids
        if unknown_ids:
            raise ValueError(f"template correction references unknown units: {sorted(unknown_ids)}")
        if target_ids & locked_ids:
            raise ValueError("template correction targets a locked unit")
        if not overwrite_ids <= target_ids:
            raise ValueError("overwrite permissions must reference corrected targets")

        corrected = analysis.model_copy(update={
            "analysis_id": f"analysis-{uuid.uuid4().hex}",
            "status": "ready_for_confirmation",
            "suggestions": list(correction.suggestions),
            "human_confirmed_target_unit_ids": sorted(target_ids),
            "approved_overwrite_unit_ids": sorted(overwrite_ids),
            "locked_unit_ids": sorted(locked_ids),
            "correction_actor_id": correction.actor_id,
            "correction_comment": correction.comment,
            "mapping_conflict_unit_ids": [],
            "activation_decision": None,
        })
        corrected.validate_suggestions()
        decision = decide_template_activation(corrected)
        corrected = corrected.model_copy(update={
            "activation_decision": decision,
            "status": (
                "ready_for_confirmation"
                if decision.status == "auto_accepted"
                else "requires_human"
            ),
        })
        return self.store.save_corrected_template_analysis(
            corrected,
            expected_parent_analysis_id=analysis.analysis_id,
        )

    def confirm_template_analysis(
        self,
        ctx: RequestContext,
        *,
        analysis_id: str,
        display_name: str,
        execution_mode: str | None = None,
    ) -> TemplateVersion:
        analysis = self.store.get_template_analysis_by_id(analysis_id)
        if analysis is None:
            raise KeyError(f"template analysis not found: {analysis_id}")
        if analysis.status != "ready_for_confirmation":
            raise ValueError("template analysis is not ready for confirmation")
        if analysis.activation_decision is None:
            raise ValueError("template activation decision is required for confirmation")
        if analysis.activation_decision.status != "auto_accepted":
            raise ValueError("template activation decision rejects confirmation")
        current = self.store.get_template_analysis(analysis.template_version_id)
        if current is None or current.analysis_id != analysis.analysis_id:
            raise ValueError("template analysis is not the current revision")
        template = self._template(analysis.template_version_id)
        self._require_template_kb_scope(ctx, template, "write")
        actual_hash = hashlib.sha256(self.store.read_template_content(template.template_version_id)).hexdigest()
        if actual_hash != analysis.content_hash or actual_hash != template.content_hash:
            raise ValueError("template content hash changed since analysis")
        analysis.validate_suggestions()
        regions, bindings = self._regions_and_bindings(template, analysis)
        inferred_execution_mode = "internal_harness" if analysis.suggestions else "deterministic_only"
        requested_execution_mode = execution_mode or inferred_execution_mode
        if requested_execution_mode not in {
            "deterministic_only", "internal_harness", "external_agent"
        }:
            raise ValueError("unsupported document execution mode")
        schema = DocumentSchema(
            document_schema_id=template.template_schema_id,
            version=template.template_schema_version,
            document_type=_canonical_template_document_type(
                display_name,
                template.template_id,
            ),
            fields=[
                self._field_for_suggestion(suggestion, analysis.units)
                for suggestion in analysis.suggestions
            ],
            status="approved",
            execution_mode=requested_execution_mode,
        )
        approved = template.model_copy(update={
            "status": "approved", "approved_by": ctx.user_id,
            "approved_at": datetime.now(timezone.utc),
        })
        return self.store.activate_template_analysis(
            template=approved, analysis_content_hash=analysis.content_hash,
            schema=schema, regions=regions, bindings=bindings,
        )

    # Work order creation --------------------------------------------------------------

    def create_document_work_order(
        self,
        ctx: RequestContext,
        *,
        project_id: str,
        baseline_id: str,
        template_version_id: str,
        document_schema_id: str,
        document_schema_version: str,
        idempotency_key: str | None = None,
        processing_artifact_ids: list[str] | None = None,
        harness_policy_id: str | None = None,
        execution_mode: str | None = None,
        generation_session_id: str | None = None,
        generation_brief: dict[str, Any] | None = None,
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        output_spec_hash: str | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        document_plan_hash: str | None = None,
    ) -> DocumentWorkOrder:
        tenant_id = ctx.tenant_id or "default"
        self.projects.access.require(ctx, project_id, "create_work_order")
        if idempotency_key:
            existing = self.store.find_work_order_by_idempotency(tenant_id, project_id, idempotency_key)
            if existing is not None:
                return existing
        self._validate_compatibility_work_order_inputs(
            ctx,
            operation="create_work_order",
            output_spec_id=output_spec_id,
            output_spec_version=output_spec_version,
            output_spec_hash=output_spec_hash,
            document_plan_id=document_plan_id,
            document_plan_version=document_plan_version,
            document_plan_hash=document_plan_hash,
        )
        self._validate_work_order_definition(
            template_version_id,
            document_schema_id,
            document_schema_version,
            harness_policy_id,
            execution_mode,
        )
        baseline = self.projects.store.get_baseline(baseline_id, tenant_id)
        if baseline is None:
            raise ValueError("baseline not found")
        work_order_id = f"wo-{uuid.uuid4().hex}"
        snapshot = self.projects.create_source_set_snapshot(
            ctx, work_order_id=work_order_id, project_id=project_id, baseline_id=baseline_id,
            processing_artifact_ids=processing_artifact_ids,
        )
        return self._create_frozen_work_order(
            ctx,
            scope_type="project",
            snapshot=snapshot,
            template_version_id=template_version_id,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
            idempotency_key=idempotency_key,
            harness_policy_id=harness_policy_id,
            execution_mode=execution_mode,
            generation_session_id=generation_session_id,
            generation_brief=generation_brief,
            output_spec_id=output_spec_id,
            output_spec_version=output_spec_version,
            output_spec_hash=output_spec_hash,
            document_plan_id=document_plan_id,
            document_plan_version=document_plan_version,
            document_plan_hash=document_plan_hash,
        )

    def create_knowledge_base_work_order(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        source_names: list[str],
        template_version_id: str,
        document_schema_id: str,
        document_schema_version: str,
        idempotency_key: str | None = None,
        harness_policy_id: str | None = None,
        execution_mode: str | None = None,
        generation_session_id: str | None = None,
        generation_brief: dict[str, Any] | None = None,
        source_scope: str = "knowledge_base_only",
        attachment_refs: Sequence[Any] | None = None,
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        output_spec_hash: str | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        document_plan_hash: str | None = None,
    ) -> DocumentWorkOrder:
        # Work-order creation only snapshots sources the caller can read.
        # Execution, approval, resume and artifact actions re-check write
        # access at their own mutation boundaries.
        if not ctx.has_kb_permission(knowledge_base_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        tenant_id = ctx.tenant_id or "default"
        if idempotency_key:
            existing = self._find_knowledge_base_work_order_by_idempotency(
                tenant_id,
                knowledge_base_name,
                idempotency_key,
                department_id=self._ctx_department_id(ctx),
            )
            if existing is not None:
                return existing
        self._validate_compatibility_work_order_inputs(
            ctx,
            operation="create_knowledge_base_work_order",
            output_spec_id=output_spec_id,
            output_spec_version=output_spec_version,
            output_spec_hash=output_spec_hash,
            document_plan_id=document_plan_id,
            document_plan_version=document_plan_version,
            document_plan_hash=document_plan_hash,
        )
        self._validate_work_order_definition(
            template_version_id,
            document_schema_id,
            document_schema_version,
            harness_policy_id,
            execution_mode,
        )
        snapshot = self._create_knowledge_base_source_snapshot(
            ctx, knowledge_base_name, source_names
        )
        try:
            return self._create_frozen_work_order(
                ctx,
                scope_type="knowledge_base",
                snapshot=snapshot,
                knowledge_base_name=knowledge_base_name,
                template_version_id=template_version_id,
                document_schema_id=document_schema_id,
                document_schema_version=document_schema_version,
                idempotency_key=idempotency_key,
                harness_policy_id=harness_policy_id,
                execution_mode=execution_mode,
                generation_session_id=generation_session_id,
                generation_brief=generation_brief,
                source_scope=source_scope,
                attachment_refs=attachment_refs,
                output_spec_id=output_spec_id,
                output_spec_version=output_spec_version,
                output_spec_hash=output_spec_hash,
                document_plan_id=document_plan_id,
                document_plan_version=document_plan_version,
                document_plan_hash=document_plan_hash,
            )
        except sqlite3.IntegrityError:
            if idempotency_key:
                existing = self._find_knowledge_base_work_order_by_idempotency(
                    tenant_id,
                    knowledge_base_name,
                    idempotency_key,
                    department_id=self._ctx_department_id(ctx),
                )
                if existing is not None:
                    return existing
            raise

    def restart_cancelled_knowledge_base_work_order(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        max_parallel_units: int = 8,
    ) -> DocumentWorkOrder:
        original = self._order(ctx, work_order_id, "run_deterministic_work_order")
        if original.status != "cancelled" or original.scope_type != "knowledge_base":
            raise ValueError("only cancelled knowledge-base work orders may be restarted")
        snapshot = self.resolve_source_snapshot(original)
        return self._create_frozen_work_order(
            ctx,
            scope_type="knowledge_base",
            snapshot=snapshot,
            knowledge_base_name=original.knowledge_base_name,
            template_version_id=original.template_version_id,
            document_schema_id=original.document_schema_id,
            document_schema_version=original.document_schema_version,
            idempotency_key=f"restart:{original.work_order_id}",
            generation_session_id=original.generation_session_id,
            generation_brief=original.generation_brief,
            restart_of_work_order_id=original.work_order_id,
            existing_task=self._bound_task(original),
            max_parallel_units=max_parallel_units,
            execution_mode=original.execution_mode,
            harness_policy_id=original.harness_policy_id,
            source_scope=original.source_scope_snapshot or "knowledge_base_only",
            attachment_refs=original.attachment_refs_snapshot,
            kb_scope_snapshot=original.kb_scope_snapshot,
            output_spec_id=original.output_spec_id,
            output_spec_version=original.output_spec_version,
            output_spec_hash=original.output_spec_hash,
            document_plan_id=original.document_plan_id,
            document_plan_version=original.document_plan_version,
            document_plan_hash=original.document_plan_hash,
        )

    def _bound_task(self, order: DocumentWorkOrder) -> DocumentTask | None:
        task_id = str(getattr(order, "task_id", None) or "").strip()
        if not task_id:
            return None
        task = self.task_service.store.get(task_id)
        if task is None:
            return None
        if getattr(task, "work_order_id", None) not in {None, order.work_order_id}:
            # The task has already advanced to a newer work order; do not fork
            # the user-facing aggregate back to this lineage.
            return None
        return task

    def restart_work_order_for_revision(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        revision_id: str,
        max_parallel_units: int = 8,
    ) -> DocumentWorkOrder:
        """Freeze the controlled regeneration work order for an artifact revision.

        Revisions never mutate the released parent.  This method is the
        execution bridge requested by the optimization plan: the revision
        record becomes a queued background generation that produces a fresh
        candidate artifact, which is bound back to the revision on completion.
        The work order is idempotent per revision, so client replays cannot
        fork the lineage.
        """
        revision_service = getattr(self, "revision_service", None)
        revision = revision_service.store.get(str(revision_id or "").strip()) if revision_service else None
        if revision is None:
            raise KeyError("artifact revision not found")
        if revision.child_artifact_id:
            raise ValueError("artifact revision already generated its child artifact")
        original = self._order(ctx, work_order_id, "run_deterministic_work_order")
        if revision.work_order_id != original.work_order_id:
            raise ValueError("artifact revision does not belong to this work order")
        if original.scope_type != "knowledge_base" or not original.knowledge_base_name:
            raise ValueError("revision regeneration currently supports knowledge-base work orders")
        idempotency_key = f"revision:{revision.revision_id}"
        existing = self._find_knowledge_base_work_order_by_idempotency(
            original.tenant_id,
            original.knowledge_base_name,
            idempotency_key,
            department_id=original.resource_department_id,
        )
        if existing is not None:
            return existing
        snapshot = self.resolve_source_snapshot(original)
        order = self._create_frozen_work_order(
            ctx,
            scope_type="knowledge_base",
            snapshot=snapshot,
            knowledge_base_name=original.knowledge_base_name,
            template_version_id=original.template_version_id,
            document_schema_id=original.document_schema_id,
            document_schema_version=original.document_schema_version,
            idempotency_key=idempotency_key,
            generation_session_id=original.generation_session_id,
            generation_brief=original.generation_brief,
            restart_of_work_order_id=original.work_order_id,
            revision_id=revision.revision_id,
            existing_task=self._bound_task(original),
            max_parallel_units=max_parallel_units,
            execution_mode=original.execution_mode,
            harness_policy_id=original.harness_policy_id,
            source_scope=original.source_scope_snapshot or "knowledge_base_only",
            attachment_refs=original.attachment_refs_snapshot,
            kb_scope_snapshot=original.kb_scope_snapshot,
            output_spec_id=original.output_spec_id,
            output_spec_version=original.output_spec_version,
            output_spec_hash=original.output_spec_hash,
            document_plan_id=original.document_plan_id,
            document_plan_version=original.document_plan_version,
            document_plan_hash=original.document_plan_hash,
        )
        revision_service.store.update_status(revision.revision_id, "generating")
        return order

    def _find_knowledge_base_work_order_by_idempotency(
        self,
        tenant_id: str,
        knowledge_base_name: str,
        idempotency_key: str,
        department_id: str | None = None,
    ) -> DocumentWorkOrder | None:
        return next(
            (
                order
                for order in self.store.list_work_orders_for_knowledge_base(
                    tenant_id, knowledge_base_name
                )
                if order.idempotency_key == idempotency_key
                and (order.resource_department_id is None or order.resource_department_id == department_id)
            ),
            None,
        )

    def _create_frozen_work_order(
        self,
        ctx: RequestContext,
        *,
        scope_type: str,
        snapshot,
        template_version_id: str,
        document_schema_id: str,
        document_schema_version: str,
        knowledge_base_name: str | None = None,
        idempotency_key: str | None = None,
        harness_policy_id: str | None = None,
        execution_mode: str | None = None,
        generation_session_id: str | None = None,
        generation_brief: dict[str, Any] | None = None,
        restart_of_work_order_id: str | None = None,
        revision_id: str | None = None,
        existing_task: DocumentTask | None = None,
        max_parallel_units: int = 3,
        source_scope: str = "knowledge_base_only",
        attachment_refs: Sequence[Any] | None = None,
        kb_scope_snapshot: dict[str, Any] | None = None,
        output_spec_id: str | None = None,
        output_spec_version: int | None = None,
        output_spec_hash: str | None = None,
        document_plan_id: str | None = None,
        document_plan_version: int | None = None,
        document_plan_hash: str | None = None,
    ) -> DocumentWorkOrder:
        prospective_work_order_id = (
            snapshot.work_order_id
            if scope_type != "knowledge_base"
            else f"wo-{uuid.uuid4().hex}"
        )
        self._record_compatibility_work_order_write(
            ctx,
            operation="create_work_order",
            entity_id=prospective_work_order_id,
            output_spec_id=output_spec_id,
            output_spec_version=output_spec_version,
            output_spec_hash=output_spec_hash,
            document_plan_id=document_plan_id,
            document_plan_version=document_plan_version,
            document_plan_hash=document_plan_hash,
        )
        template = self._template(template_version_id)
        schema = self._schema(document_schema_id, document_schema_version)
        if template.status != "approved" or schema.status != "approved":
            raise ValueError(
                "document generation requires approved template and document schema"
            )
        if schema.execution_mode not in {
            "deterministic_only", "internal_harness", "external_agent"
        }:
            raise ValueError("unsupported document execution mode")
        if execution_mode is not None and execution_mode != schema.execution_mode:
            raise ValueError("requested execution_mode does not match the approved schema")
        requested_executor = execution_mode or schema.execution_mode
        harness_policy = None
        if requested_executor in {"internal_harness", "external_agent"}:
            if harness_policy_id:
                harness_policy = self.store.get_harness_policy(harness_policy_id)
            else:
                harness_policy = self._schema_harness_policy(schema, max_parallel_units=max_parallel_units)
                harness_policy_id = harness_policy.harness_policy_id
            if harness_policy is None or harness_policy.status != "approved":
                raise ValueError(
                    "Harness-backed work orders require an approved HarnessPolicy"
                )
        is_knowledge_base = scope_type == "knowledge_base"
        frozen_attachment_refs = (
            _freeze_attachment_ref_snapshot(attachment_refs)
            if is_knowledge_base
            else []
        )
        frozen_source_scope = (
            _resolve_document_source_scope(
                source_scope,
                has_attachments=bool(frozen_attachment_refs),
            )
            if is_knowledge_base
            else ""
        )
        frozen_kb_scope = (
            dict(kb_scope_snapshot)
            if kb_scope_snapshot
            else {
                "tenant_id": snapshot.tenant_id,
                "knowledge_base_name": knowledge_base_name,
                "resource_department_id": self._ctx_department_id(ctx),
                "knowledge_base_id": self._ctx_kb_id(ctx),
            }
            if is_knowledge_base
            else {}
        )
        if existing_task is not None:
            # Restart/regeneration lineages must reuse the exact task bound to
            # the parent work order; a fresh idempotency lookup could otherwise
            # fork the user-facing aggregate.
            task = existing_task
        else:
            task = self.ensure_document_task(
                ctx,
                template_version_id=template.template_version_id,
                generation_session_id=generation_session_id,
                project_id=None if is_knowledge_base else snapshot.project_id,
                knowledge_base_name=knowledge_base_name if is_knowledge_base else None,
                idempotency_key=(
                    idempotency_key
                    or (f"generation-session:{generation_session_id}" if generation_session_id else None)
                ),
            )
        if task is None and getattr(src.settings, "DOCUMENT_TASK_ASSOCIATION_REQUIRED", False):
            raise RuntimeError("document task association is required but task writes are disabled")
        order = DocumentWorkOrder(
            work_order_id=(
                prospective_work_order_id
                if is_knowledge_base
                else snapshot.work_order_id
            ),
            tenant_id=snapshot.tenant_id,
            scope_type=scope_type,
            knowledge_base_name=knowledge_base_name if is_knowledge_base else None,
            resource_department_id=(
                self._ctx_department_id(ctx) if is_knowledge_base else None
            ),
            knowledge_base_id=self._ctx_kb_id(ctx) if is_knowledge_base else None,
            project_id=None if is_knowledge_base else snapshot.project_id,
            baseline_id=None if is_knowledge_base else snapshot.baseline_id,
            baseline_content_hash=(
                "" if is_knowledge_base else snapshot.baseline_content_hash
            ),
            source_set_snapshot_id=snapshot.source_set_snapshot_id,
            source_scope_snapshot=frozen_source_scope,
            attachment_refs_snapshot=frozen_attachment_refs,
            kb_scope_snapshot=frozen_kb_scope,
            template_version_id=template.template_version_id,
            document_schema_id=schema.document_schema_id,
            document_schema_version=schema.version,
            template_schema_id=template.template_schema_id,
            template_schema_version=template.template_schema_version,
            retrieval_policy_version="1",
            renderer_policy_version=self._policy(template).version,
            target_format=template.format,
            execution_mode=requested_executor,
            harness_policy_id=harness_policy_id,
            harness_policy_version=harness_policy.version if harness_policy else None,
            requested_executor=requested_executor,
            task_id=task.task_id if task is not None else None,
            revision_id=revision_id,
            unit_statuses={
                **{item.field_id: "planned" for item in schema.fields},
                **{item.review_item_id: "planned" for item in schema.review_items},
            },
            created_by=ctx.user_id,
            idempotency_key=idempotency_key,
            generation_session_id=generation_session_id,
            generation_brief=dict(generation_brief or {}),
            output_spec_id=output_spec_id,
            output_spec_version=output_spec_version,
            output_spec_hash=output_spec_hash,
            document_plan_id=document_plan_id,
            document_plan_version=document_plan_version,
            document_plan_hash=document_plan_hash,
            restart_of_work_order_id=restart_of_work_order_id,
        )
        # New plan-backed work orders use the v3 preimage, which binds the
        # accepted OutputSpec/DocumentPlan references.  Legacy callers retain
        # the v2 preimage and historical v1 rows remain untouched.
        if any(
            value is not None
            for value in (
                output_spec_id, output_spec_version, output_spec_hash,
                document_plan_id, document_plan_version, document_plan_hash,
            )
        ):
            if not all(
                value is not None
                for value in (
                    output_spec_id, output_spec_version, output_spec_hash,
                    document_plan_id, document_plan_version, document_plan_hash,
                )
            ):
                raise ValueError("plan-backed work orders require complete planning references")
            order = order.model_copy(update={"input_fingerprint_version": 3})
            order = order.model_copy(update={
                "input_fingerprint": compute_input_fingerprint_v3(order),
            })
        else:
            from src.document_authoring.models import compute_input_fingerprint_v2

            order = order.model_copy(update={"input_fingerprint_version": 2})
            order = order.model_copy(update={
                "input_fingerprint": compute_input_fingerprint_v2(order),
            })
        persisted = self.store.create_work_order(order)
        if task is not None:
            if restart_of_work_order_id and task.work_order_id == restart_of_work_order_id:
                self.task_service.store.advance_work_order(
                    task.task_id,
                    persisted.work_order_id,
                    expected_work_order_id=restart_of_work_order_id,
                )
            else:
                self.task_service.store.attach_work_order(task.task_id, persisted.work_order_id)
        # Phase 0 planning is observational only.  It runs after both the
        # WorkOrder and its task association are durable, and is fail-soft by
        # construction so the legacy execution contract remains untouched.
        self._run_shadow_planning(
            ctx,
            persisted,
            snapshot=snapshot,
            template=template,
            schema=schema,
        )
        return persisted

    def _create_template_free_work_order(
        self,
        ctx: RequestContext,
        *,
        plan: Any,
        output_spec: Any,
        snapshot: KnowledgeBaseSourceSnapshot,
        knowledge_base_name: str,
        idempotency_key: str,
        generation_session_id: str | None = None,
        generation_brief: dict[str, Any] | None = None,
        source_scope: str = "knowledge_base_only",
    ) -> DocumentWorkOrder:
        """Create a v3 WorkOrder for an accepted server-owned recipe plan.

        Template-free execution still uses the existing WorkOrder, HarnessRun
        and task stores.  The required template/schema columns carry
        server-owned sentinel identities so legacy fingerprints and readers
        remain compatible; no template bytes or caller-supplied package data
        are introduced into this route.
        """
        from src.document_authoring.planning.recipes import build_builtin_recipe_registry

        if getattr(plan, "status", None) != "accepted" or getattr(output_spec, "status", None) != "accepted":
            raise ValueError("template-free WorkOrder requires accepted planning rows")
        layout = getattr(plan, "layout_contract", None)
        if getattr(layout, "kind", None) != "structure":
            raise ValueError("template-free WorkOrder requires a structure layout contract")
        prospective_work_order_id = f"wo-{uuid.uuid4().hex}"
        refs = self.compatibility.validate_new_document_write(
            operation="create_template_free_work_order",
            output_spec=output_spec,
            document_plan=plan,
        )
        if refs is not None:
            self.compatibility.record_plan_backed_execution(
                operation="create_template_free_work_order",
                tenant_id=snapshot.tenant_id,
                entity_type="work_order",
                entity_id=prospective_work_order_id,
                plan_id=refs.document_plan_id,
                plan_version=refs.document_plan_version,
                plan_hash=refs.document_plan_hash,
            )
            self.compatibility.record_new_write(
                operation="create_template_free_work_order",
                tenant_id=snapshot.tenant_id,
                entity_type="work_order",
                entity_id=prospective_work_order_id,
                plan_id=refs.document_plan_id,
                plan_version=refs.document_plan_version,
                plan_hash=refs.document_plan_hash,
            )
        render_spec = dict(getattr(plan, "render_spec", {}) or {})
        recipe_id = str(
            render_spec.get("recipe_id") or getattr(layout, "structure_profile_id", "")
        ).strip()
        recipe_version = str(
            render_spec.get("recipe_version") or getattr(layout, "structure_profile_version", "")
        ).strip()
        recipe = build_builtin_recipe_registry().resolve(recipe_id, recipe_version)
        primary_deliverables = [
            item for item in output_spec.artifact.deliverables
            if item.role == "primary"
        ]
        if len(primary_deliverables) != 1:
            raise ValueError("template-free OutputSpec requires exactly one primary deliverable")
        target_format = str(primary_deliverables[0].format).strip().casefold().lstrip(".")
        if target_format not in recipe.supported_formats:
            raise ValueError("template-free recipe does not support the primary deliverable")

        template_version_id = f"system-recipe:{recipe_id}@{recipe_version}"
        schema_id = f"system-recipe-schema:{recipe_id}@{recipe_version}"
        base_schema = DocumentSchema(
            document_schema_id=schema_id,
            version=recipe_version,
            document_type=output_spec.document_type,
            status="approved",
            execution_mode="internal_harness",
        )
        schema = self.harness_runtime._plan_execution_schema(plan, base_schema)
        harness_policy = self._schema_harness_policy(schema)
        task = self.ensure_document_task(
            ctx,
            template_version_id=template_version_id,
            generation_session_id=generation_session_id,
            project_id=None,
            knowledge_base_name=knowledge_base_name,
            idempotency_key=idempotency_key,
        )
        if task is None and getattr(src.settings, "DOCUMENT_TASK_ASSOCIATION_REQUIRED", False):
            raise RuntimeError("document task association is required but task writes are disabled")

        frozen_attachment_refs: list[dict[str, Any]] = []
        frozen_source_scope = _resolve_document_source_scope(
            source_scope,
            has_attachments=False,
        )
        frozen_kb_scope = {
            "tenant_id": snapshot.tenant_id,
            "knowledge_base_name": knowledge_base_name,
            "resource_department_id": self._ctx_department_id(ctx),
            "knowledge_base_id": self._ctx_kb_id(ctx),
        }
        order = DocumentWorkOrder(
            work_order_id=prospective_work_order_id,
            tenant_id=snapshot.tenant_id,
            scope_type="knowledge_base",
            knowledge_base_name=knowledge_base_name,
            resource_department_id=self._ctx_department_id(ctx),
            knowledge_base_id=self._ctx_kb_id(ctx),
            project_id=None,
            baseline_id=None,
            baseline_content_hash="",
            source_set_snapshot_id=snapshot.source_set_snapshot_id,
            source_scope_snapshot=frozen_source_scope,
            attachment_refs_snapshot=frozen_attachment_refs,
            kb_scope_snapshot=frozen_kb_scope,
            template_version_id=template_version_id,
            document_schema_id=schema.document_schema_id,
            document_schema_version=schema.version,
            template_schema_id=schema_id,
            template_schema_version=recipe_version,
            retrieval_policy_version="1",
            renderer_policy_version=plan.renderer_capability_version,
            target_format=target_format,
            execution_mode="internal_harness",
            harness_policy_id=harness_policy.harness_policy_id,
            harness_policy_version=harness_policy.version,
            requested_executor="internal_harness",
            task_id=task.task_id if task is not None else None,
            unit_statuses={
                **{unit.unit_id: "planned" for unit in plan.semantic_units},
            },
            created_by=ctx.user_id,
            idempotency_key=idempotency_key,
            generation_session_id=generation_session_id,
            generation_brief=dict(generation_brief or {}),
            output_spec_id=output_spec.output_spec_id,
            output_spec_version=output_spec.version,
            output_spec_hash=output_spec.content_hash,
            document_plan_id=plan.document_plan_id,
            document_plan_version=plan.version,
            document_plan_hash=plan.plan_hash,
        )
        order = order.model_copy(update={
            "input_fingerprint_version": 3,
            "input_fingerprint": compute_input_fingerprint_v3(order.model_copy(update={
                "input_fingerprint_version": 3,
            })),
        })
        persisted = self.store.create_work_order(order)
        if task is not None:
            self.task_service.store.attach_work_order(task.task_id, persisted.work_order_id)
        return persisted

    def _template_free_execution_inputs(
        self,
        order: DocumentWorkOrder,
    ) -> tuple[Any, DocumentSchema, Any, Any]:
        """Reload the accepted recipe plan and build request-local run inputs."""
        planning_store = getattr(self.planning, "store", None)
        if planning_store is None:
            raise ValueError("template-free execution requires a planning store")
        plan = planning_store.get_plan(
            str(order.document_plan_id or ""),
            int(order.document_plan_version or 0),
            tenant_id=order.tenant_id,
            user_id=order.created_by,
        )
        output_spec = planning_store.get_output_spec(
            str(order.output_spec_id or ""),
            int(order.output_spec_version or 0),
            tenant_id=order.tenant_id,
            user_id=order.created_by,
        )
        if plan is None or plan.status != "accepted":
            raise ValueError("accepted DocumentPlan is unavailable for template-free execution")
        if output_spec is None or output_spec.status != "accepted":
            raise ValueError("accepted OutputSpec is unavailable for template-free execution")
        if (
            plan.plan_hash != order.document_plan_hash
            or plan.output_spec_id != order.output_spec_id
            or plan.output_spec_version != order.output_spec_version
            or plan.output_spec_hash != order.output_spec_hash
            or output_spec.content_hash != order.output_spec_hash
        ):
            raise ValueError("template-free planning references do not match the WorkOrder")
        layout = plan.layout_contract
        if getattr(layout, "kind", None) != "structure":
            raise ValueError("template-free execution requires a structure layout contract")
        from src.document_authoring.planning.recipes import build_builtin_recipe_registry

        render_spec = dict(plan.render_spec or {})
        recipe_id = str(
            render_spec.get("recipe_id") or getattr(layout, "structure_profile_id", "")
        ).strip()
        recipe_version = str(
            render_spec.get("recipe_version") or getattr(layout, "structure_profile_version", "")
        ).strip()
        recipe = build_builtin_recipe_registry().resolve(recipe_id, recipe_version)
        primary = [item for item in output_spec.artifact.deliverables if item.role == "primary"]
        if len(primary) != 1 or str(primary[0].format).strip().casefold() != order.target_format:
            raise ValueError("template-free primary deliverable does not match the WorkOrder")
        base_schema = DocumentSchema(
            document_schema_id=order.document_schema_id,
            version=order.document_schema_version,
            document_type=output_spec.document_type,
            status="approved",
            execution_mode="internal_harness",
        )
        schema = self.harness_runtime._plan_execution_schema(plan, base_schema)
        template = SimpleNamespace(
            template_version_id=order.template_version_id,
            template_id=f"system-recipe:{recipe_id}",
            format=order.target_format,
            content_hash=recipe.recipe_hash or "",
            template_schema_id=order.template_schema_id,
            template_schema_version=order.template_schema_version,
            renderer_policy_id=f"system-recipe-renderer:{recipe_id}@{recipe_version}",
            status="approved",
        )
        return template, schema, plan, recipe

    def _validate_work_order_definition(
        self,
        template_version_id: str,
        document_schema_id: str,
        document_schema_version: str,
        harness_policy_id: str | None = None,
        execution_mode: str | None = None,
    ) -> None:
        template = self._template(template_version_id)
        schema = self._schema(document_schema_id, document_schema_version)
        if template.status != "approved" or schema.status != "approved":
            raise ValueError(
                "document generation requires approved template and document schema"
            )
        if schema.execution_mode not in {
            "deterministic_only", "internal_harness", "external_agent"
        }:
            raise ValueError("unsupported document execution mode")
        if execution_mode is not None and execution_mode != schema.execution_mode:
            raise ValueError("requested execution_mode does not match the approved schema")
        if schema.execution_mode in {"internal_harness", "external_agent"} and harness_policy_id:
            policy = self.store.get_harness_policy(harness_policy_id)
            if policy is None or policy.status != "approved":
                raise ValueError(
                    "work orders using a Harness executor require an approved HarnessPolicy"
                )

    def auto_generate_document(
        self,
        ctx: RequestContext,
        *,
        project_id: str,
        baseline_id: str,
        template_version_id: str,
        document_schema_id: str,
        document_schema_version: str,
        retrieve: Callable[[Any, int, "str | None"], RetrievalOutcome] | None = None,
        retrieve_factory: Callable[[DocumentWorkOrder], Callable[[Any, int, "str | None"], RetrievalOutcome]] | None = None,
        idempotency_key: str | None = None,
    ):
        """Create, run, and validate a document as a human-review candidate."""
        order = self.create_document_work_order(
            ctx,
            project_id=project_id,
            baseline_id=baseline_id,
            template_version_id=template_version_id,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
            idempotency_key=idempotency_key,
        )
        if retrieve_factory is not None:
            retrieve = retrieve_factory(order)
        if retrieve is None:
            raise ValueError("auto generation requires a retrieval provider")
        return self.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)

    # Deterministic execution ----------------------------------------------------------

    def run_deterministic_work_order(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        rule_inputs: dict[str, dict[str, Any]],
        retrieval_outcomes: dict[str, RetrievalOutcome],
    ) -> DocumentArtifact:
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        if order.execution_mode != "deterministic_only":
            raise ValueError("work order is not deterministic-only")
        if order.status not in {"planned", "retrieving", "blocked", "waiting_human_input"}:
            raise ValueError(f"work order cannot run from status {order.status}")
        schema = self._schema(order.document_schema_id, order.document_schema_version)
        template = self._template(order.template_version_id)
        snapshot = self.resolve_source_snapshot(order)
        bindings = self.store.list_unit_bindings(order.template_schema_id, order.template_schema_version)
        by_unit = {binding.semantic_unit_id: binding for binding in bindings}

        matrix_rows: list[dict[str, Any]] = []
        fills: list[WorkbookFill] | list[DocxFill] = []
        statuses: dict[str, str] = {}
        for item in schema.review_items:
            outcome = retrieval_outcomes.get(item.retrieval_rule_id)
            if outcome is not None:
                self._validate_retrieval_outcome(order, snapshot, outcome)
            evidence_ids = [str(getattr(evidence, "id", "")) for evidence in (outcome.evidences if outcome else []) if getattr(evidence, "id", "")]
            if outcome is None:
                result_status, display, diagnostics = "retrieval_failed", "检索未执行", ["retrieval outcome is required"]
            elif outcome.status in {"retrieval_failed", "source_unavailable", "access_denied", "partial_failure"}:
                result_status, display, diagnostics = "retrieval_failed", "检索异常", [f"retrieval outcome: {outcome.status}"]
            else:
                spec = self._rule(item.deterministic_rule_id or "")
                result = self.rules.execute(item.review_item_id, spec, rule_inputs.get(item.review_item_id, {}), evidence_ids)
                result_status, display, diagnostics = result.status, result.display_value, result.diagnostics
            statuses[item.review_item_id] = result_status
            matrix_rows.append({
                "review_item_id": item.review_item_id,
                "requirement_id": item.retrieval_rule_id,
                "coverage_status": self._coverage_status(result_status, outcome),
                "evidence_ids": evidence_ids,
                "display_value": display,
                "diagnostics": diagnostics,
            })
            binding = by_unit.get(item.review_item_id)
            if binding is not None:
                label = _result_label(result_status, display)
                for region_id in binding.target_region_ids:
                    if template.format == "docx":
                        fills.append(DocxFill(region_id=region_id, value=label, semantic_unit_id=item.review_item_id))
                    else:
                        fills.append(WorkbookFill(region_id=region_id, value=label, semantic_unit_id=item.review_item_id))

        fill_plan = self._fill_plan(template, fills)
        rendered_content, integrity_manifest = self._render_fill_plan(template, fill_plan)
        self._assert_generated_artifact_clean(rendered_content, template.format)
        report = self.validator.validate(
            work_order_id=order.work_order_id, matrix_rows=matrix_rows, integrity_manifest=integrity_manifest,
        )
        report = self._append_icd_pin_validation(
            order,
            report,
            rendered_content,
        )
        self.store.save_evidence_matrix(order.work_order_id, matrix_rows)
        self.store.save_validation_report(report)
        artifact = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}", tenant_id=order.tenant_id,
            work_order_id=order.work_order_id, run_id=f"run-{uuid.uuid4().hex}",
            output_format=template.format,
            stage="review_candidate", content_hash=hashlib.sha256(rendered_content).hexdigest(),
            validation_report_id=report.validation_report_id,
            integrity_manifest_id=integrity_manifest["manifest_hash"],
        )
        artifact = self._save_artifact_for_task(order, artifact, rendered_content, template.format)
        self._bind_revision_child(order, artifact, report_status=report.status)
        next_status = "waiting_human_approval" if report.status in {"passed", "requires_human"} else "blocked"
        self._replace_order(order, status=next_status, unit_statuses=statuses,
                            evidence_matrix_id=f"matrix-{order.work_order_id}", validation_report_id=report.validation_report_id)
        return artifact

    # Internal Harness / semantic-assisted execution ----------------------------------

    def prepare_icd_scope_review(
        self,
        ctx: RequestContext,
        work_order_id: str,
        decision,
    ) -> IcdScopeReview:
        """Persist an ICD scope decision against this work order's frozen sources."""
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        snapshot = self.resolve_source_snapshot(order)
        self._validate_icd_scope_decision_sources(decision, snapshot)
        existing = self.store.get_icd_scope_review(work_order_id)
        if existing is not None:
            if existing.source_snapshot_hash != snapshot.content_hash:
                raise ValueError("ICD scope review source snapshot differs from the work order")
            if existing.decision_content_hash != icd_scope_decision_hash(decision):
                raise ValueError("ICD scope review is already bound to a different decision")
            return existing
        review = IcdScopeReview(
            work_order_id=work_order_id,
            decision=decision,
            source_snapshot_hash=snapshot.content_hash,
            status="frozen" if not decision.exceptions else "pending",
        )
        return self.store.save_icd_scope_review(review)

    def get_icd_scope_review(
        self,
        ctx: RequestContext,
        work_order_id: str,
    ) -> IcdScopeReview | None:
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        review = self.store.get_icd_scope_review(work_order_id)
        if review is not None and review.source_snapshot_hash != self.resolve_source_snapshot(order).content_hash:
            raise ValueError("ICD scope review source snapshot differs from the work order")
        return review

    def submit_icd_scope_resolution(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        resolutions: list[dict[str, str]],
        comment: str,
    ) -> IcdScopeReview:
        self._order(ctx, work_order_id, "run_deterministic_work_order")
        review = self.get_icd_scope_review(ctx, work_order_id)
        if review is None:
            raise KeyError("ICD scope review not found")
        if review.status == "frozen":
            raise ValueError("ICD scope review is already frozen")
        if any(
            exception.kind in ICD_BLOCKING_SCOPE_EXCEPTION_KINDS
            for exception in review.exceptions
        ):
            raise ValueError(
                "ICD connector scope must be completed from the template and EDF before resolution"
            )
        normalized_comment = comment.strip()
        if not normalized_comment:
            raise ValueError("ICD scope resolution comment is required")
        if not isinstance(resolutions, list):
            raise ValueError("ICD scope resolutions must be submitted in one batch")
        if any(
            not isinstance(resolution, dict)
            or str(resolution.get("action") or "").strip().casefold()
            not in {"include", "exclude"}
            for resolution in resolutions
        ):
            raise ValueError("ICD scope resolution action must be include or exclude")
        batch = [
            IcdScopeResolution(
                exception_id=str(resolution.get("exception_id") or ""),
                action=str(resolution.get("action") or ""),
                actor_id=ctx.user_id,
            )
            for resolution in resolutions
            if isinstance(resolution, dict)
        ]
        if len(batch) != len(resolutions):
            raise ValueError("ICD scope resolutions must be objects")
        resolution_ids = [resolution.exception_id for resolution in batch]
        if len(resolution_ids) != len(set(resolution_ids)):
            raise ValueError("ICD scope resolution batch must resolve every exception exactly once")
        expected_ids = {exception.exception_id for exception in review.exceptions}
        if {resolution.exception_id for resolution in batch} != expected_ids:
            raise ValueError("ICD scope resolution batch must resolve every exception exactly once")
        frozen = review.model_copy(update={
            "status": "frozen",
            "resolutions": batch,
            "resolution_comment": normalized_comment,
            "frozen_at": datetime.now(timezone.utc),
        })
        return self.store.freeze_icd_scope_review(frozen)

    def _validate_icd_scope_decision_sources(self, decision, snapshot: Any) -> None:
        referenced_source_names = {
            source_name.strip()
            for item in [*decision.auto_items, *decision.exceptions]
            for source_name in item.source_names
            if source_name.strip()
        }
        frozen_source_identities = {
            source_name.strip()
            for source_name in getattr(snapshot, "source_names", [])
            if source_name.strip()
        }
        if not frozen_source_identities:
            source_version_ids = (
                list(getattr(snapshot, "source_version_ids", []))
                + list(getattr(snapshot, "shared_reference_version_ids", []))
            )
            for source_version_id in source_version_ids:
                source_version = self.projects.store.get_source_version(
                    source_version_id,
                    snapshot.tenant_id,
                )
                if source_version is None:
                    continue
                document = self.projects.store.get_logical_document(
                    source_version.document_id,
                    snapshot.tenant_id,
                )
                if document is not None and document.title.strip():
                    frozen_source_identities.add(document.title.strip())
        foreign_source_names = referenced_source_names - frozen_source_identities
        if foreign_source_names:
            raise ValueError(
                "ICD scope decision source names differ from the frozen work order snapshot"
            )

    def run_internal_harness(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        retrieve: Callable[[Any, int, "str | None"], RetrievalOutcome],
        writer: ManagedWriter | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> DocumentArtifact:
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        scope_review = self.store.get_icd_scope_review(work_order_id)
        if scope_review is not None:
            snapshot = self.resolve_source_snapshot(order)
            if scope_review.source_snapshot_hash != snapshot.content_hash:
                raise ValueError("ICD scope review source snapshot differs from the work order")
            if scope_review.pending_count:
                raise ValueError("unresolved ICD scope exceptions block Harness execution")
        if order.execution_mode not in {"internal_harness", "external_agent"} or not order.harness_policy_id:
            raise ValueError("work order is not configured for a Harness executor")
        if order.status not in {"planned", "retrieving", "blocked", "waiting_human_input"}:
            raise ValueError(f"work order cannot run from status {order.status}")
        policy = self.store.get_harness_policy(order.harness_policy_id, order.harness_policy_version)
        if policy is None or policy.status != "approved":
            raise ValueError("approved HarnessPolicy is required")
        writer = writer or self._writer_for_policy(policy)
        if writer.provider.provider_id != policy.writer_provider_id:
            raise PermissionError("writer provider does not match the approved HarnessPolicy")
        rewriter = self._rewriter_for_policy(policy)
        reranker = self._reranker_for_policy(policy)
        fit_checker = self._fit_checker_for_policy(policy)
        snapshot = self.resolve_source_snapshot(order)
        if str(order.template_version_id).startswith("system-recipe:"):
            template, schema, _plan, _recipe = self._template_free_execution_inputs(order)
        else:
            schema = self._schema(order.document_schema_id, order.document_schema_version)
            template = self._template(order.template_version_id)
        run, manifest = self.harness_runtime.create_run(order, policy, snapshot, template, schema)
        self._associate_task_run(order, run.harness_run_id)
        order = self._replace_order(order, status="retrieving", run_manifest_id=manifest.run_manifest_id)
        try:
            result = self.harness_runtime.execute(
                work_order=order, run=run, manifest=manifest, policy=policy, schema=schema, snapshot=snapshot,
                legacy_claims=self.store.list_legacy_template_claims(template.template_version_id),
                writer=writer, retrieve=retrieve, rewriter=rewriter, reranker=reranker,
                fit_checker=fit_checker, should_cancel=should_cancel,
            )
        except Exception:
            current_run = self.store.get_harness_run(run.harness_run_id)
            if current_run is not None and current_run.status == "failed":
                self._replace_order(order, status="blocked")
            elif current_run is not None and current_run.status == "cancelled":
                self._replace_order(order, status="cancelled")
            raise
        return self._finalize_internal_harness_safely(
            order,
            template,
            run.harness_run_id,
            result,
        )

    def pause_harness_run(self, ctx: RequestContext, harness_run_id: str):
        run = self._harness_run_for_context(ctx, harness_run_id)
        paused = self.store.request_harness_run_state(harness_run_id, "paused")
        order = self._order_raw(run.work_order_id)
        if order.status != "cancelled":
            self._replace_order(order, status="paused")
        return paused

    def cancel_harness_run(self, ctx: RequestContext, harness_run_id: str):
        run = self._harness_run_for_context(ctx, harness_run_id)
        cancelled = self.store.request_harness_run_state(harness_run_id, "cancelled")
        order = self._order_raw(run.work_order_id)
        self._replace_order(order, status="cancelled")
        return cancelled

    def delete_document_work_order(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        reason: str = "",
    ):
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        return self.store.delete_terminal_work_order(
            order.work_order_id,
            actor_id=ctx.user_id,
            reason=reason,
        )

    def resume_internal_harness(
        self,
        ctx: RequestContext,
        harness_run_id: str,
        *,
        retrieve: Callable[[Any, int, "str | None"], RetrievalOutcome],
        writer: ManagedWriter | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> DocumentArtifact:
        run = self._harness_run_for_context(ctx, harness_run_id)
        order = self._order_raw(run.work_order_id)
        if order.execution_mode not in {"internal_harness", "external_agent"} or not order.harness_policy_id:
            raise ValueError("work order is not configured for a Harness executor")
        snapshot = self.resolve_source_snapshot(order)
        scope_review = self.store.get_icd_scope_review(order.work_order_id)
        if scope_review is not None:
            if scope_review.source_snapshot_hash != snapshot.content_hash:
                raise ValueError("ICD scope review source snapshot differs from the work order")
            if scope_review.pending_count:
                raise ValueError("unresolved ICD scope exceptions block Harness execution")
        policy = self.store.get_harness_policy(order.harness_policy_id, order.harness_policy_version)
        if policy is None or policy.status != "approved":
            raise ValueError("frozen HarnessPolicy is no longer approved")
        if run.status in {"paused", "failed"}:
            run = self.store.queue_harness_retry(run.harness_run_id, policy.max_retries)
        if run.status != "retrying":
            raise ValueError(f"harness run cannot resume from status {run.status}")
        manifest = self.store.get_run_manifest(run.run_manifest_id)
        if manifest is None or manifest.input_fingerprint != order.input_fingerprint:
            raise ValueError("run manifest does not match the frozen work order inputs")
        if manifest.source_set_snapshot_id != order.source_set_snapshot_id:
            raise ValueError("run manifest source set does not match the work order")
        writer = writer or self._writer_for_policy(policy)
        if writer.provider.provider_id != policy.writer_provider_id:
            raise PermissionError("writer provider does not match the frozen HarnessPolicy")
        rewriter = self._rewriter_for_policy(policy)
        reranker = self._reranker_for_policy(policy)
        fit_checker = self._fit_checker_for_policy(policy)
        snapshot = self.resolve_source_snapshot(order)
        if str(order.template_version_id).startswith("system-recipe:"):
            template, schema, _plan, _recipe = self._template_free_execution_inputs(order)
        else:
            schema = self._schema(order.document_schema_id, order.document_schema_version)
            template = self._template(order.template_version_id)
        self._associate_task_run(order, run.harness_run_id)
        order = self._replace_order(order, status="retrieving", run_manifest_id=manifest.run_manifest_id)
        try:
            result = self.harness_runtime.execute(
                work_order=order, run=run, manifest=manifest, policy=policy, schema=schema, snapshot=snapshot,
                legacy_claims=self.store.list_legacy_template_claims(template.template_version_id),
                writer=writer, retrieve=retrieve, rewriter=rewriter, reranker=reranker,
                fit_checker=fit_checker, should_cancel=should_cancel,
            )
        except Exception:
            current_run = self.store.get_harness_run(run.harness_run_id)
            if current_run is not None and current_run.status == "failed":
                self._replace_order(order, status="blocked")
            elif current_run is not None and current_run.status == "cancelled":
                self._replace_order(order, status="cancelled")
            raise
        return self._finalize_internal_harness_safely(
            order,
            template,
            run.harness_run_id,
            result,
        )

    def resolve_agent_human_decision(
        self,
        ctx: RequestContext,
        harness_run_id: str,
        *,
        pending_event_id: str,
        proposal_hash: str,
        decision: str,
        retrieve: Callable[[Any, int, "str | None"], RetrievalOutcome] | None = None,
        writer: ManagedWriter | None = None,
    ) -> dict[str, Any]:
        """Consume a low-confidence proposal decision on the same HarnessRun.

        The Store performs the tenant/hash/expiry/single-use check and fences
        the state transition.  Approval may then resume the same logical run;
        it never creates a second business run.  A missing retriever leaves an
        approved run in ``retrying`` for an explicit worker retry rather than
        pretending that approval completed generation.
        """
        run = self._harness_run_for_context(ctx, harness_run_id)
        order = self._order_raw(run.work_order_id)
        if order.scope_type == "knowledge_base":
            if not order.knowledge_base_name or not ctx.has_kb_permission(order.knowledge_base_name, "write"):
                raise PermissionError("knowledge base write permission is required for an agent decision")
        try:
            decision_key = human_decision_key(
                harness_run_id=run.harness_run_id,
                pending_event_id=str(pending_event_id),
                proposal_hash=str(proposal_hash),
                decision=decision,
            )
        except ValueError:
            raise
        updated, event = self.store.resolve_harness_human_decision(
            run.harness_run_id,
            pending_event_id=str(pending_event_id),
            proposal_hash=str(proposal_hash),
            decision=decision,
            decision_key=decision_key,
            actor_id=str(ctx.user_id),
            tenant_id=ctx.tenant_id or "default",
        )
        result: dict[str, Any] = {
            "decision": str(decision).strip().casefold(),
            "decision_key": decision_key,
            "event_id": event.event_id,
            "harness_run": updated.model_dump(mode="json"),
        }
        if str(decision).strip().casefold() != "approve" or retrieve is None:
            return result
        artifact = self.resume_internal_harness(
            ctx,
            run.harness_run_id,
            retrieve=retrieve,
            writer=writer,
        )
        result["artifact"] = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else artifact
        latest = self.store.get_harness_run(run.harness_run_id)
        if latest is not None:
            result["harness_run"] = latest.model_dump(mode="json")
        return result

    def _finalize_internal_harness_safely(
        self,
        order: DocumentWorkOrder,
        template: TemplateVersion,
        harness_run_id: str,
        result,
    ) -> DocumentArtifact:
        try:
            return self._finalize_internal_harness_result(
                order,
                template,
                harness_run_id,
                result,
            )
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            normalized = message.casefold()
            if "duplicate long value fan-out" in normalized:
                updates = {
                    "status": "blocked",
                    "error_code": "renderer_safety_violation",
                    "error_message": message,
                    "retryable": False,
                    "next_actions": ["view_error", "replace_template", "retry_generation"],
                }
            elif isinstance(exc, (ValueError, PermissionError)):
                updates = {
                    "status": "blocked",
                    "error_code": "rendering_validation_failed",
                    "error_message": message,
                    "retryable": False,
                    "next_actions": ["view_error", "replace_template"],
                }
            else:
                updates = {
                    "status": "failed",
                    "error_code": "finalization_failed",
                    "error_message": message,
                    "retryable": True,
                    "next_actions": ["view_error", "retry_generation"],
                }
            self._replace_order(order, **updates)
            logger.exception(
                "Document generation finalization failed for work order %s",
                order.work_order_id,
            )
            raise

    def _finalize_internal_harness_result(
        self,
        order: DocumentWorkOrder,
        template: TemplateVersion,
        harness_run_id: str,
        result,
    ) -> DocumentArtifact:
        if getattr(result, "execution_route", "legacy_schema") == "plan_dag":
            return self._finalize_plan_internal_harness_result(
                order, template, harness_run_id, result,
            )
        blocked_fields = self._block_generation_fields(order, result)
        bindings = self.store.list_unit_bindings(order.template_schema_id, order.template_schema_version)
        binding_by_unit = {binding.semantic_unit_id: binding for binding in bindings}
        fills = self._semantic_fills(template, result.drafts, result.unit_statuses, binding_by_unit)
        rendered_content, integrity_manifest = self._render_fill_plan(template, fills)
        rendered_content, integrity_manifest, front_view_issues = self._apply_icd_front_view_layout(
            order,
            template,
            rendered_content,
            integrity_manifest,
            pin_functions=self._pin_functions_from_drafts(order, result.drafts),
        )
        self._assert_generated_artifact_clean(rendered_content, template.format)
        report = self.validator.validate(
            work_order_id=order.work_order_id, matrix_rows=result.matrix_rows,
            integrity_manifest=integrity_manifest, additional_issues=result.issues,
        )
        report = self._append_icd_pin_validation(
            order,
            report,
            rendered_content,
        )
        self.store.save_evidence_matrix(order.work_order_id, result.matrix_rows)
        report = self._append_icd_validation_issues(report, front_view_issues)
        required_issues = self._required_content_issues(order, result.unit_statuses)
        if required_issues:
            report = report.model_copy(update={
                "issues": [*report.issues, *required_issues],
                "status": "requires_human" if report.status != "failed" else "failed",
            })
        if blocked_fields:
            report = self._append_block_generation_issues(report, blocked_fields)
        self.store.save_validation_report(report)
        artifact = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}", tenant_id=order.tenant_id,
            work_order_id=order.work_order_id, run_id=harness_run_id,
            output_format=template.format,
            stage="review_candidate", content_hash=hashlib.sha256(rendered_content).hexdigest(),
            validation_report_id=report.validation_report_id,
            integrity_manifest_id=integrity_manifest["manifest_hash"],
        )
        artifact = self._save_artifact_for_task(order, artifact, rendered_content, template.format)
        self._bind_revision_child(order, artifact, report_status=report.status)
        requires_review = _requires_human_review(
            result.unit_statuses,
            auto_publish_verified=src.settings.DOCUMENT_AUTO_PUBLISH_VERIFIED,
        ) or bool(required_issues)
        if blocked_fields:
            # Coordinator-level block_generation: unresolved required missing /
            # conflicts under a confirmed block_generation brief never reach
            # rendering or automatic publication. The review/status record and
            # queries stay available while a human decides.
            self._replace_order(
                order, status="blocked", unit_statuses=result.unit_statuses,
                evidence_matrix_id=f"matrix-{order.work_order_id}",
                validation_report_id=report.validation_report_id,
                error_code="block_generation_unresolved_missing",
                error_message=f"confirmed brief blocks release for: {sorted(blocked_fields)}",
                retryable=False,
                next_actions=["provide_value", "view_error"],
            )
            self._publish_missing_data_clarification(
                order,
                [
                    {"field_id": field_id, "message": f"必填内容尚未完成：{field_id}"}
                    for field_id in blocked_fields
                ],
            )
            return artifact
        if (
            _automatic_release_allowed(order, report, requires_review=requires_review)
        ):
            return self._auto_publish_verified_candidate(
                order,
                artifact,
                report,
                rendered_content,
                unit_statuses=result.unit_statuses,
                evidence_matrix_id=f"matrix-{order.work_order_id}",
                validation_report_id=report.validation_report_id,
            )
        next_status = "waiting_human_input" if requires_review else "waiting_human_approval"
        self._replace_order(
            order, status=next_status, unit_statuses=result.unit_statuses,
            evidence_matrix_id=f"matrix-{order.work_order_id}", validation_report_id=report.validation_report_id,
        )
        return artifact

    def _finalize_plan_internal_harness_result(
        self,
        order: DocumentWorkOrder,
        template: TemplateVersion,
        harness_run_id: str,
        result,
    ) -> DocumentArtifact:
        """Run the plan-only quality pipeline after unit DAG execution.

        The legacy finalizer above deliberately remains untouched.  This
        branch consumes only the accepted plan and typed drafts, then performs
        coverage, unit review, deterministic aggregation, both review gates,
        and the existing allowlisted renderer in that order.
        """
        planning_store = getattr(self.planning, "store", None)
        if planning_store is None:
            raise ValueError("plan-backed finalization requires a planning store")
        plan = planning_store.get_plan(
            str(order.document_plan_id),
            int(order.document_plan_version),
            tenant_id=order.tenant_id,
            user_id=order.created_by,
        )
        if plan is None or plan.status != "accepted":
            raise ValueError("accepted DocumentPlan is unavailable during finalization")
        output_spec = planning_store.get_output_spec(
            str(order.output_spec_id),
            int(order.output_spec_version),
            tenant_id=order.tenant_id,
            user_id=order.created_by,
        )
        if output_spec is None or output_spec.status != "accepted":
            raise ValueError("accepted OutputSpec is unavailable during finalization")
        if (
            output_spec.output_spec_id != order.output_spec_id
            or output_spec.version != int(order.output_spec_version)
            or output_spec.content_hash != order.output_spec_hash
            or plan.output_spec_id != output_spec.output_spec_id
            or plan.output_spec_version != output_spec.version
            or plan.output_spec_hash != output_spec.content_hash
        ):
            raise ValueError(
                "accepted OutputSpec identity or hash does not match the frozen Work Order and plan"
            )
        if (
            plan.plan_hash != order.document_plan_hash
            or plan.output_spec_id != order.output_spec_id
            or plan.output_spec_version != order.output_spec_version
            or plan.output_spec_hash != order.output_spec_hash
        ):
            raise ValueError("plan references do not match the frozen Work Order")
        layout = plan.layout_contract
        is_template_free = getattr(layout, "kind", None) == "structure"
        icd_layout_issues: list[dict[str, Any]] = []
        if not is_template_free:
            if getattr(layout, "kind", None) != "template":
                raise ValueError("plan-backed finalization requires a supported layout contract")
            if (
                layout.template_version_id != template.template_version_id
                or layout.template_schema_id != template.template_schema_id
                or layout.template_schema_version != template.template_schema_version
            ):
                raise ValueError("plan layout contract does not match the frozen template")
            declared_template_hash = str(
                (plan.output_spec_summary or {}).get("template_content_hash") or ""
            )
            if declared_template_hash and declared_template_hash != template.content_hash:
                raise ValueError("plan template content hash does not match the frozen template")
        else:
            # The synthetic template object is only the compatibility input
            # required by HarnessRun/manifest schemas. Binding and rendering
            # below use the accepted recipe, never template bytes or regions.
            if not str(order.template_version_id).startswith("system-recipe:"):
                raise ValueError("structure plan requires a server-owned recipe WorkOrder")

        evidence_entries = self.store.list_evidence_entries(harness_run_id)
        raw_evidence: dict[str, dict[str, Any]] = {}
        for outcome in (getattr(result, "outcomes", {}) or {}).values():
            for evidence in getattr(outcome, "evidences", []) or []:
                evidence_id = str(getattr(evidence, "id", "") or "").strip()
                if not evidence_id:
                    continue
                raw_evidence[evidence_id] = {
                    "id": evidence_id,
                    "content": str(getattr(evidence, "content", "") or ""),
                    "metadata": dict(getattr(evidence, "metadata", {}) or {}),
                }
        # On a restarted run the registry is intentionally opaque and does
        # not contain source text.  Coverage can still verify ownership and
        # presence; the already validated durable drafts are reviewed by the
        # deterministic unit reports without re-reading source content.
        coverage_evidence: Any = evidence_entries
        coverage = CoverageEvaluator().evaluate(
            plan, result.drafts, coverage_evidence,
        )
        result.coverage_report = coverage
        self._append_plan_execution_event(
            harness_run_id=harness_run_id,
            order=order,
            event_type="coverage_evaluated",
            node_name="coverage",
            subject_hash=coverage.report_hash,
            payload={
                "report_hash": coverage.report_hash,
                "expected_count": coverage.expected_count,
                "covered_count": coverage.covered_count,
                "missing_count": coverage.missing_count,
                "unsupported_count": coverage.unsupported_count,
                "duplicate_count": coverage.duplicate_count,
            },
        )

        drafts_by_id = {draft.unit_id: draft for draft in result.drafts}
        draft_by_unit = {
            unit.unit_id: (
                drafts_by_id.get(unit.unit_id)
                or drafts_by_id.get(_plan_field_execution_key(unit.unit_id))
            )
            for unit in plan.semantic_units
        }
        task_by_unit = {task.unit_id: task for task in plan.unit_tasks}
        unit_reviewer = UnitReviewer(self.validator)
        unit_reports: dict[str, Any] = {}
        for unit in plan.semantic_units:
            task = task_by_unit[unit.unit_id]
            task_payload = task.model_dump(mode="json")
            task_output = dict(task_payload.get("output_schema") or {})
            requirement = next(
                (
                    item for item in plan.coverage_contract.requirements
                    if item.unit_id == unit.unit_id
                ),
                None,
            )
            if requirement is not None:
                task_output.setdefault("row_keys", list(requirement.row_keys))
                task_output.setdefault("required_columns", list(requirement.required_columns))
                task_output.setdefault("row_order", requirement.row_order)
                task_output.setdefault("duplicate_policy", requirement.duplicate_policy)
            raw_type = str(task_output.get("type") or "text").casefold()
            if raw_type in {"object", "section", "paragraph"}:
                task_output["type"] = "text"
            task_payload["output_schema"] = task_output
            review_evidence = raw_evidence or None
            report = unit_reviewer.review(
                task_payload,
                draft_by_unit.get(unit.unit_id),
                coverage,
                review_evidence,
                attempt=1,
            )
            unit_reports[unit.unit_id] = report
            self._append_plan_execution_event(
                harness_run_id=harness_run_id,
                order=order,
                event_type="unit_reviewed",
                node_name=f"unit:{task.task_id}",
                unit_id=unit.unit_id,
                subject_hash=report.report_hash,
                payload={
                    "unit_id": unit.unit_id,
                    "status": report.status,
                    "report_hash": report.report_hash,
                    "issue_codes": report.issue_codes if hasattr(report, "issue_codes") else [
                        issue.code for issue in report.issues
                    ],
                },
            )
        result.unit_reports = unit_reports

        document_model = self.build_document_model(plan, result.drafts, unit_reports)
        result.document_model = document_model
        pre_render = self.review_document_pre_render(
            plan, document_model, coverage, unit_reports,
        )
        result.pre_render_report = pre_render
        self._append_plan_execution_event(
            harness_run_id=harness_run_id,
            order=order,
            event_type="pre_render_reviewed",
            node_name="pre-render-review",
            subject_hash=pre_render.report_hash,
            payload={
                "report_hash": pre_render.report_hash,
                "status": pre_render.status,
                "issue_codes": pre_render.issue_codes,
            },
        )

        icd_layout_issues: list[dict[str, Any]] = []
        if is_template_free:
            recipe_id = str(
                (plan.render_spec or {}).get("recipe_id")
                or getattr(layout, "structure_profile_id", "")
            ).strip()
            recipe_version = str(
                (plan.render_spec or {}).get("recipe_version")
                or getattr(layout, "structure_profile_version", "")
            ).strip()
            recipe = build_builtin_recipe_registry().resolve(recipe_id, recipe_version)
            binding = StructureBindingCompiler().compile(plan, document_model, recipe)
            renderers = {
                "docx": StructuredDocxRenderer(),
                "pdf": StructuredPdfRenderer(),
                "xlsx": StructuredXlsxRenderer(),
            }
            renderer = renderers.get(order.target_format)
            if renderer is None:
                raise ValueError(f"no template-free renderer is registered for {order.target_format}")
            structured = renderer.render(document_model, recipe, binding)
            rendered_content = structured.content
            integrity_manifest = structured.integrity_manifest
            render_result = structured
        else:
            fill_plan = self.build_plan_fill_plan(template, plan, document_model)
            rendered_content, integrity_manifest = self._render_fill_plan(template, fill_plan)
            rendered_content, integrity_manifest, icd_layout_issues = (
                self._apply_icd_front_view_layout(
                    order,
                    template,
                    rendered_content,
                    integrity_manifest,
                    pin_functions=self._pin_functions_from_drafts(order, result.drafts),
                )
            )
            render_result = {
                "content": rendered_content,
                "integrity_manifest": integrity_manifest,
            }
        post_render = self.review_document_post_render(
            plan, document_model, render_result, rendered_content,
        )
        result.post_render_report = post_render
        self._append_plan_execution_event(
            harness_run_id=harness_run_id,
            order=order,
            event_type="post_render_reviewed",
            node_name="post-render-review",
            subject_hash=post_render.report_hash,
            payload={
                "report_hash": post_render.report_hash,
                "status": post_render.status,
                "artifact_hash": post_render.artifact_hash,
                "issue_codes": post_render.issue_codes,
            },
        )
        decision = self.evaluate_document_release(pre_render, post_render)
        result.release_decision = decision
        self._append_plan_execution_event(
            harness_run_id=harness_run_id,
            order=order,
            event_type="release_gated",
            node_name="release",
            subject_hash=content_hash(decision),
            payload={
                "status": decision.status,
                "release_allowed": decision.release_allowed,
                "issue_codes": decision.issue_codes,
                "subject_hash": decision.subject_hash,
            },
        )

        review_issues = [
            {"kind": issue.code, **issue.model_dump(mode="json")}
            for report in (pre_render, post_render)
            for issue in report.issues
        ]
        review_issues.extend(
            {"kind": str(issue.get("code") or issue.get("kind") or "icd_layout"), **issue}
            for issue in icd_layout_issues
        )
        required_issues = self._required_content_issues(
            order, result.unit_statuses, plan=plan,
        )
        review_issues.extend(required_issues)
        self.store.save_evidence_matrix(order.work_order_id, result.matrix_rows)
        validation = self.validator.validate(
            work_order_id=order.work_order_id,
            matrix_rows=result.matrix_rows,
            integrity_manifest=integrity_manifest,
            additional_issues=review_issues,
        )
        # The plan route must apply the same frozen-scope pin check as the
        # legacy route; otherwise a template whose pin table could not be
        # discovered can silently keep its example rows.  Structure-only plans
        # have no template pin table, so the check stays template-scoped.
        if not is_template_free:
            validation = self._append_icd_pin_validation(order, validation, rendered_content)
        if decision.status == "blocked":
            validation = validation.model_copy(update={
                "status": "requires_human" if not integrity_manifest.get("policy_violations") else "failed",
                "issues": [*validation.issues, {
                    "kind": "plan_release_blocked",
                    "code": "plan_release_blocked",
                    "severity": "blocking",
                    "issue_codes": decision.issue_codes,
                }],
            })
        elif decision.status == "needs_review" and validation.status == "passed":
            validation = validation.model_copy(update={"status": "requires_human"})
        self.store.save_validation_report(validation)

        artifact = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}",
            tenant_id=order.tenant_id,
            work_order_id=order.work_order_id,
            run_id=harness_run_id,
            output_format=order.target_format if is_template_free else template.format,
            stage="review_candidate",
            content_hash=hashlib.sha256(rendered_content).hexdigest(),
            validation_report_id=validation.validation_report_id,
            integrity_manifest_id=integrity_manifest["manifest_hash"],
        )
        artifact = self._save_artifact_for_task(
            order, artifact, rendered_content,
            order.target_format if is_template_free else template.format,
        )
        result.artifact = artifact
        self._bind_revision_child(order, artifact, report_status=validation.status)

        bound_manifest = self.bind_document_review_manifest(
            self.store.get_run_manifest(
                self.store.get_harness_run(harness_run_id).run_manifest_id
            ) if self.store.get_harness_run(harness_run_id) is not None else None,
            document_model=document_model,
            pre_render_report=pre_render,
            post_render_report=post_render,
            release_decision=decision,
        )
        if bound_manifest is not None:
            self.store.replace_run_manifest(bound_manifest)
        current_run = self.store.get_harness_run(harness_run_id)
        if current_run is not None:
            self.store.replace_harness_run(current_run.model_copy(update={
                "document_model_hash": document_model.model_hash,
                "pre_render_review_hash": pre_render.report_hash,
                "post_render_review_hash": post_render.report_hash,
                "artifact_hash": post_render.artifact_hash,
                "release_decision_hash": content_hash(decision),
                "release_status": (
                    "released" if decision.release_allowed and src.settings.DOCUMENT_AUTO_PUBLISH_VERIFIED
                    else decision.status if decision.status in {"blocked", "needs_review"} else "pending"
                ),
            }))

        requires_review = not decision.release_allowed
        if (
            decision.release_allowed
            and src.settings.DOCUMENT_AUTO_PUBLISH_VERIFIED
            and validation.status == "passed"
        ):
            return self._auto_publish_verified_candidate(
                order,
                artifact,
                validation,
                rendered_content,
                unit_statuses=result.unit_statuses,
                evidence_matrix_id=f"matrix-{order.work_order_id}",
                validation_report_id=validation.validation_report_id,
            )
        next_status = (
            "blocked" if decision.status == "blocked" else "waiting_human_approval"
        ) if requires_review else "waiting_human_approval"
        self._replace_order(
            order,
            status=next_status,
            unit_statuses=result.unit_statuses,
            evidence_matrix_id=f"matrix-{order.work_order_id}",
            validation_report_id=validation.validation_report_id,
            error_code=("plan_release_blocked" if decision.status == "blocked" else None),
            error_message=(
                "plan-backed release is blocked by deterministic review gates"
                if decision.status == "blocked" else None
            ),
            retryable=False if decision.status == "blocked" else None,
            next_actions=["view_error", "provide_value"] if decision.status == "blocked" else ["approve"],
        )
        self._publish_missing_data_clarification(order, required_issues)
        return artifact

    def _append_plan_execution_event(
        self,
        *,
        harness_run_id: str,
        order: DocumentWorkOrder,
        event_type: str,
        node_name: str,
        subject_hash: str,
        payload: dict[str, Any],
        unit_id: str | None = None,
    ) -> AuthoringExecutionEvent:
        """Append one idempotent, sanitized plan-review business fact."""
        event = AuthoringExecutionEvent(
            event_id=f"authoring-event-{uuid.uuid4().hex}",
            event_type=event_type,
            tenant_id=order.tenant_id,
            work_order_id=order.work_order_id,
            harness_run_id=harness_run_id,
            idempotency_key=f"plan:{harness_run_id}:{event_type}:{node_name}:{subject_hash}",
            executor="authoring_graph",
            node_name=node_name,
            unit_id=unit_id,
            sanitized_payload=safe_review_projection(payload),
        )
        return self.store.append_execution_event(event)

    def _required_content_issues(
        self,
        order: DocumentWorkOrder,
        statuses: dict[str, str],
        *,
        plan: Any | None = None,
    ) -> list[dict[str, Any]]:
        """A usable candidate is not a complete release when required content is absent.

        The brief's mark_tbd policy permits drafting, never silently relaxing
        the frozen schema's release requirements. Missing execution statuses
        are gaps too; otherwise a skipped field would evade the gate.
        """
        if plan is not None and getattr(getattr(plan, "layout_contract", None), "kind", None) == "structure":
            return [
                {
                    "code": "required_content_incomplete",
                    "kind": "required_content_incomplete",
                    "field_id": requirement.unit_id,
                    "unit_id": _plan_field_execution_key(requirement.unit_id),
                    "coverage_status": statuses.get(
                        _plan_field_execution_key(requirement.unit_id), "unsearched"
                    ),
                    "severity": "blocking",
                    "message": f"必填内容尚未完成：{requirement.unit_id}",
                }
                for requirement in plan.coverage_contract.requirements
                if requirement.required
                and statuses.get(_plan_field_execution_key(requirement.unit_id)) != "ready_to_render"
            ]
        schema = self.store.get_document_schema(order.document_schema_id, order.document_schema_version)
        if schema is None:
            return [{"code": "required_content_incomplete", "kind": "missing_schema", "severity": "blocking"}]
        return [
            {"code": "required_content_incomplete", "kind": "required_content_incomplete",
             "field_id": field.field_id, "unit_id": _plan_field_execution_key(field.field_id),
             "coverage_status": statuses.get(_plan_field_execution_key(field.field_id), "unsearched"),
             "severity": "blocking", "message": f"必填内容尚未完成：{field.label}"}
            for field in schema.fields
            if field.required and statuses.get(_plan_field_execution_key(field.field_id)) != "ready_to_render"
        ]

    def _publish_missing_data_clarification(
        self,
        order: DocumentWorkOrder,
        issues: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Hand required evidence gaps back to the owning chat session.

        A blocked release is an execution fact, not an instruction for the
        user to open the workbench and edit cells.  When the order belongs to
        an OutputSpec session, persist one deterministic question so the next
        chat turn can choose a policy or add a value and re-plan safely.
        """
        session_id = str(getattr(order, "generation_session_id", None) or "").strip()
        if not session_id:
            return False
        sessions = getattr(self.store, "generation_sessions", None)
        if sessions is None or not callable(getattr(sessions, "get_session", None)):
            return False
        try:
            session = sessions.get_session(session_id)
        except (KeyError, PermissionError, ValueError):
            return False
        if str(getattr(session, "contract_version", "") or "") != "output_spec_v1":
            return False
        field_labels: list[str] = []
        for issue in issues or ():
            field_id = str(issue.get("field_id") or issue.get("unit_id") or "").strip()
            if field_id and field_id not in field_labels:
                field_labels.append(field_id)
        if not field_labels:
            return False
        question_id = "missing_data_resolution"
        # Do not append the same unanswered question on every status poll.
        messages = list(getattr(session, "messages", None) or [])
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if getattr(message, "role", None) != "assistant" or getattr(message, "question_id", None) != question_id:
                continue
            answered = any(
                getattr(item, "role", None) == "user"
                and getattr(item, "question_id", None) == question_id
                for item in messages[index + 1:]
            )
            if not answered:
                return False
            break
        content = (
            "知识库中暂未找到以下必填字段的可靠资料："
            + "、".join(field_labels[:20])
            + "。请选择处理方式："
        )
        try:
            setter = getattr(sessions, "set_status", None)
            if callable(setter):
                setter(session_id, "needs_clarification")
            sessions.append_message(
                session_id,
                role="assistant",
                content=content,
                question_id=question_id,
                options=["标记为未提供，继续生成", "补充说明", "暂停等待资料"],
                reason="必填字段缺少可靠证据，需由对话确认处理方式。",
            )
            task_id = str(getattr(order, "task_id", None) or "").strip()
            task_store = getattr(getattr(self, "task_service", None), "store", None)
            if task_id and callable(getattr(task_store, "update_status", None)):
                task_store.update_status(task_id, "needs_clarification")
        except (KeyError, PermissionError, ValueError):
            logger.warning("failed to persist missing-data clarification for %s", session_id, exc_info=True)
            return False
        return True

    def _block_generation_fields(self, order: DocumentWorkOrder, result) -> list[str]:
        """Fields whose confirmed brief forces block_generation on unresolved
        required missing/conflicts. Data-complete runs are never blocked."""
        from src.document_authoring.harness.agent_contracts import (
            effective_missing_policy,
            normalize_clarification_policy,
        )

        brief = dict(order.generation_brief or {})
        if not brief.get("confirmed"):
            return []
        brief_missing = normalize_clarification_policy(
            "missing_data_policy", brief.get("missing_data_policy")
        )
        if brief_missing != "block_generation":
            return []
        schema = self.store.get_document_schema(
            order.document_schema_id, order.document_schema_version
        )
        if schema is None:
            return []
        missing_statuses = {"requires_human", "blocked", "conflicting", "retrieval_failed", "insufficient_evidence", "tbd"}
        blocked: list[str] = []
        for field in schema.fields:
            unit_id = _plan_field_execution_key(field.field_id)
            status = result.unit_statuses.get(unit_id)
            if (
                field.required
                and effective_missing_policy(brief_missing, field.missing_policy) == "block_section"
                and status in missing_statuses
            ):
                blocked.append(field.field_id)
        return blocked

    def _append_block_generation_issues(self, report, blocked_fields: list[str]):
        issues = list(getattr(report, "issues", []) or [])
        for field_id in blocked_fields:
            issues.append({
                "code": "block_generation_unresolved_missing",
                "field_id": field_id,
                "message": "confirmed brief blocks release until this required field resolves",
                "severity": "blocking",
            })
        return report.model_copy(update={"issues": issues, "status": "requires_human"}) \
            if hasattr(report, "model_copy") else report

    def _auto_publish_verified_candidate(
        self,
        order: DocumentWorkOrder,
        candidate: DocumentArtifact,
        report,
        candidate_content: bytes,
        *,
        unit_statuses: dict[str, str],
        evidence_matrix_id: str,
        validation_report_id: str,
    ) -> DocumentArtifact:
        if not src.settings.DOCUMENT_AUTO_PUBLISH_VERIFIED:
            return candidate
        if (
            report.status != "passed"
            or self._has_icd_blocking_issue(report.issues)
            or self._has_blocking_validation_issue(report.issues)
        ):
            return candidate
        if hashlib.sha256(candidate_content).hexdigest() != candidate.content_hash:
            raise ValueError("candidate content hash changed before automatic publication")
        snapshot = self.resolve_source_snapshot(order)
        subject_hash = _approval_subject_hash(
            candidate.content_hash,
            report.content_hash,
            snapshot.content_hash,
        )
        released = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}",
            tenant_id=candidate.tenant_id,
            work_order_id=candidate.work_order_id,
            run_id=candidate.run_id,
            output_format=getattr(candidate, "output_format", None) or order.target_format,
            stage="approved_release",
            content_hash=candidate.content_hash,
            approval_subject_hash=subject_hash,
            parent_artifact_id=candidate.artifact_id,
            validation_report_id=candidate.validation_report_id,
            approval_event_ids=[],
            integrity_manifest_id=candidate.integrity_manifest_id,
            status_reasons=[{
                "code": "auto_published_verified",
                "message": "需求、证据、内容和渲染校验全部通过，已按部署策略自动发布。",
            }],
            released_at=datetime.now(timezone.utc),
        )
        released = self._save_artifact_for_task(
            order,
            released,
            candidate_content,
            getattr(candidate, "output_format", None) or order.target_format,
        )
        self._replace_order(
            order,
            status="complete",
            error_code=None,
            error_message=None,
            retryable=None,
            next_actions=["view_result"],
            unit_statuses=unit_statuses,
            evidence_matrix_id=evidence_matrix_id,
            validation_report_id=validation_report_id,
        )
        return released

    def start_document_generation(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        rule_inputs: dict[str, dict[str, Any]],
        retrieval_outcomes: dict[str, RetrievalOutcome],
    ) -> str:
        """Queue the P2a deterministic run off the caller/UI request thread."""
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        if order.status != "planned":
            raise ValueError(f"work order cannot be queued from status {order.status}")
        self._replace_order(order, status="retrieving")
        return self.worker.submit(
            work_order_id,
            lambda: self.run_deterministic_work_order(
                ctx, work_order_id, rule_inputs=rule_inputs, retrieval_outcomes=retrieval_outcomes,
            ),
        )

    def get_background_run_status(
        self,
        ctx: RequestContext,
        run_id: str,
    ) -> dict[str, str] | None:
        run = self.worker.get(run_id)
        if run is None:
            return None
        self._order(ctx, run.work_order_id, "run_deterministic_work_order")
        return {"run_id": run.run_id, "work_order_id": run.work_order_id, "status": run.status, "error": run.error}

    # Human review and release ---------------------------------------------------------

    def submit_document_human_event(
        self,
        ctx: RequestContext,
        *,
        artifact_id: str,
        unit_id: str,
        event_type: str,
        value: Any = None,
        comment: str = "",
    ) -> DocumentHumanEvent:
        if event_type == "feedback" and not comment.strip():
            raise ValueError("feedback comment is required")
        artifact = self._artifact_for_context(ctx, artifact_id)
        required = "approve_artifact" if event_type in {"approve", "sign"} else "submit_human_event"
        order = self._order_raw(artifact.work_order_id)
        self.require_work_order_capability(ctx, order, required)
        actor_role = (
            "knowledge_base_reader"
            if order.scope_type == "knowledge_base"
            else self._actor_role(ctx, order.project_id)
        )
        report = self.store.get_validation_report(artifact.validation_report_id)
        if report is None:
            raise ValueError("artifact validation report is missing")
        snapshot = self.resolve_source_snapshot(order)
        subject_hash = _approval_subject_hash(artifact.content_hash, report.content_hash, snapshot.content_hash)
        event = DocumentHumanEvent(
            event_id=f"event-{uuid.uuid4().hex}", work_order_id=artifact.work_order_id,
            run_id=artifact.run_id, artifact_id=artifact.artifact_id, unit_id=unit_id,
            event_type=event_type, subject_artifact_content_hash=artifact.content_hash,
            approval_subject_hash=subject_hash if event_type in {"approve", "sign"} else None,
            value=value, actor_id=ctx.user_id, actor_role=actor_role, comment=comment,
        )
        return self.store.save_human_event(event)
    def _pin_functions_from_drafts(
        self,
        order: DocumentWorkOrder,
        drafts: list[DocumentUnitDraft],
    ) -> dict[str, str]:
        """Collect evidence-backed function text from validated table drafts.

        The render overlay must not replace an evidence-verified function with
        the TBD placeholder, so the draft's function column is forwarded to
        the workbook pin-table renderer.
        """

        try:
            schema = self._schema(order.document_schema_id, order.document_schema_version)
        except (KeyError, ValueError):
            return {}
        function_columns: set[str] = set()
        for field in getattr(schema, "fields", []) or []:
            columns = getattr(field, "table_columns", None)
            if not columns:
                continue
            for column_id, kind in _column_semantics(dict(columns)).items():
                if kind == "function":
                    function_columns.add(column_id)
        if not function_columns:
            return {}
        functions: dict[str, str] = {}
        for draft in drafts:
            typed = getattr(draft, "typed_value", None)
            if typed is None or typed.kind != "table":
                continue
            for row in typed.rows:
                row_key = str(row.row_key or "").strip()
                if not row_key:
                    continue
                for column_id in function_columns:
                    value = str(row.cells.get(column_id) or "").strip()
                    if value and not value.upper().startswith("TBD"):
                        functions.setdefault(row_key, value)
                        break
        return functions

    def _apply_icd_front_view_layout(
        self,
        order: DocumentWorkOrder,
        template: TemplateVersion,
        rendered_content: bytes,
        integrity_manifest: dict[str, Any],
        *,
        pin_functions: Mapping[str, str] | None = None,
    ) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
        """Overlay ICD pin tables/front views using the same frozen pin facts.

        Template example rows are layout samples, not product truth.  This
        deterministic post-render pass replaces the pin table and physical
        connector-view slots, so an old connector such as X302 cannot survive
        into an X1900 deliverable.  It is a no-op outside a frozen ICD scope.
        """
        review = self.store.get_icd_scope_review(order.work_order_id)
        if review is None or review.pending_count:
            return rendered_content, integrity_manifest, []
        if template.format.casefold() not in {"xlsx", "xlsm"}:
            return rendered_content, integrity_manifest, []
        mappings = effective_frozen_pin_mappings(review)
        brief = dict(getattr(order, "generation_brief", {}) or {})
        inference_policy = str(brief.get("inference_policy") or "forbid").strip().casefold()
        target_identity = brief.get("target_identity") or {}
        target_metadata, assembly_erp = self._icd_target_metadata(
            target_identity,
            mappings,
        )
        pin_table = render_icd_pin_table(
            rendered_content,
            mappings,
            target_format=template.format,
            template_version_id=template.template_version_id,
            allow_rule_inference=inference_policy in {"allow_labeled", "allow_limited"},
            assembly_erp=assembly_erp,
            metadata=target_metadata,
            function_by_pin=pin_functions,
        )
        current_content = pin_table.content
        current_manifest = integrity_manifest
        if pin_table.integrity_manifest is not None:
            current_manifest = self._combine_icd_overlay_manifests(
                current_manifest,
                pin_table.integrity_manifest,
                overlay_kind="pin_table",
            )
        front_view = render_icd_front_views(
            current_content,
            mappings,
            target_format=template.format,
            template_version_id=template.template_version_id,
        )
        if front_view.integrity_manifest is None:
            return front_view.content, current_manifest, [*pin_table.issues, *front_view.issues]
        combined_manifest = self._combine_icd_overlay_manifests(
            current_manifest,
            front_view.integrity_manifest,
            overlay_kind="front_view",
        )
        return front_view.content, combined_manifest, [*pin_table.issues, *front_view.issues]

    @staticmethod
    def _icd_target_metadata(
        target_identity: Any,
        mappings: list[dict[str, Any]],
    ) -> tuple[dict[str, str], str | None]:
        values = target_identity if isinstance(target_identity, Mapping) else {}

        def by_key(*tokens: str) -> str:
            for key, value in values.items():
                normalized_key = str(key).strip().casefold()
                if any(token in normalized_key for token in tokens):
                    text = str(value or "").strip()
                    if text:
                        return text
            return ""

        raw_identity = (
            str(target_identity or "").strip()
            if isinstance(target_identity, str)
            else " ".join(str(value or "").strip() for value in values.values())
        )
        assembly_erp = by_key("erp")
        if not assembly_erp:
            match = re.search(r"(?<!\d)(6\d{6,11})(?!\d)", raw_identity)
            assembly_erp = match.group(1) if match else ""
        product_name = by_key("product", "hardware", "device", "产品", "硬件", "总成")
        if product_name == assembly_erp:
            product_name = ""
        if not product_name and isinstance(target_identity, str):
            product_name = target_identity.strip()
        location_number = "、".join(dict.fromkeys(
            str(mapping.get("refdes") or "").strip()
            for mapping in mappings
            if str(mapping.get("refdes") or "").strip()
        ))
        return {
            "product_name": product_name or "TBD",
            "customer_number": by_key("customer", "客户") or "TBD",
            "pcb_connector": by_key("pcb_connector", "board_connector", "板端") or "TBD",
            "harness_connector": by_key("harness_connector", "线束端") or "TBD",
            "location_number": location_number or "TBD",
        }, assembly_erp or None

    @staticmethod
    def _combine_icd_overlay_manifests(
        integrity_manifest: dict[str, Any],
        overlay_manifest: dict[str, Any],
        *,
        overlay_kind: str,
    ) -> dict[str, Any]:
        combined_manifest = {
            **integrity_manifest,
            "after_content_hash": overlay_manifest.get("after_content_hash"),
            "changed_parts": sorted(set(integrity_manifest.get("changed_parts", [])) | set(overlay_manifest.get("changed_parts", []))),
            "policy_violations": [
                *integrity_manifest.get("policy_violations", []),
                *overlay_manifest.get("policy_violations", []),
            ],
            "cell_policy_violations": [
                *integrity_manifest.get("cell_policy_violations", []),
                *overlay_manifest.get("cell_policy_violations", []),
            ],
            "cell_changes": [
                *integrity_manifest.get("cell_changes", []),
                *overlay_manifest.get("cell_changes", []),
            ],
            "table_row_operations": [
                *integrity_manifest.get("table_row_operations", []),
                *overlay_manifest.get("table_row_operations", []),
            ],
        }
        combined_manifest["manifest_hash"] = content_hash({
            "base_manifest_hash": integrity_manifest.get("manifest_hash"),
            f"{overlay_kind}_manifest_hash": overlay_manifest.get("manifest_hash"),
            "after_content_hash": overlay_manifest.get("after_content_hash"),
        })
        return combined_manifest


    def submit_document_feedback(
        self,
        ctx: RequestContext,
        artifact_id: str,
        *,
        comment: str,
    ) -> DocumentHumanEvent:
        """Record review feedback without changing candidate or release state."""
        normalized_comment = comment.strip()
        if not normalized_comment:
            raise ValueError("feedback comment is required")
        return self.submit_document_human_event(
            ctx,
            artifact_id=artifact_id,
            unit_id="artifact",
            event_type="feedback",
            comment=normalized_comment,
        )

    def approve_document_artifact(self, ctx: RequestContext, artifact_id: str, *, comment: str = "") -> DocumentArtifact:
        candidate = self._artifact_for_context(ctx, artifact_id)
        if candidate.stage != "review_candidate":
            raise ValueError("only a review candidate may be approved")
        order = self._order(ctx, candidate.work_order_id, "approve_artifact")
        report = self.store.get_validation_report(candidate.validation_report_id)
        if report is not None and self._has_icd_blocking_issue(report.issues):
            raise ValueError("candidate has an ICD blocking validation issue")
        if report is not None and self._has_blocking_validation_issue(report.issues):
            raise ValueError("candidate has a blocking validation issue")
        snapshot = self.resolve_source_snapshot(order)
        if report is None or report.status == "failed":
            raise ValueError("candidate does not have a releasable validation result")
        read_for_validation = getattr(
            self.store,
            "read_artifact_content_for_validation",
            self.store.read_artifact_content,
        )
        candidate_content = read_for_validation(candidate.artifact_id)
        self._assert_artifact_content_clean(candidate_content, getattr(candidate, "output_format", None) or order.target_format)
        if hashlib.sha256(candidate_content).hexdigest() != candidate.content_hash:
            raise ValueError("candidate content hash changed since validation")
        subject_hash = _approval_subject_hash(candidate.content_hash, report.content_hash, snapshot.content_hash)
        approvals = [event for event in self.store.list_human_events(candidate.artifact_id) if event.event_type in {"approve", "sign"}]
        if not approvals:
            self.submit_document_human_event(
                ctx, artifact_id=candidate.artifact_id, unit_id="artifact", event_type="approve", comment=comment,
            )
            approvals = [event for event in self.store.list_human_events(candidate.artifact_id) if event.event_type == "approve"]
        if any(event.approval_subject_hash != subject_hash or event.subject_artifact_content_hash != candidate.content_hash for event in approvals):
            raise ValueError("approval event does not bind the final candidate content and validation")
        released = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}", tenant_id=candidate.tenant_id,
            work_order_id=candidate.work_order_id, run_id=candidate.run_id, stage="approved_release",
            output_format=getattr(candidate, "output_format", None) or order.target_format,
            content_hash=candidate.content_hash, approval_subject_hash=subject_hash,
            parent_artifact_id=candidate.artifact_id, validation_report_id=candidate.validation_report_id,
            approval_event_ids=[event.event_id for event in approvals], integrity_manifest_id=candidate.integrity_manifest_id,
            released_at=datetime.now(timezone.utc),
        )
        released = self._save_artifact_for_task(
            order,
            released,
            candidate_content,
            getattr(candidate, "output_format", None) or order.target_format,
        )
        self._replace_order(order, status="complete")
        return released

    def _append_icd_pin_validation(
        self,
        order: DocumentWorkOrder,
        report,
        artifact_content: bytes,
    ):
        profile = self._icd_template_profile(order)
        if profile is not None and profile.kind == "icd_sample":
            report = self._append_icd_validation_issues(report, [{
                "code": "icd_formal_template_required",
                "severity": "blocking",
                "message": "ICD 示例模板不能生成可审核文档；请使用正式 ICD 模板。",
            }])
        review = self.store.get_icd_scope_review(order.work_order_id)
        if review is None:
            return report
        if review.pending_count:
            issues = [{
                "code": "icd_scope_unresolved",
                "severity": "blocking",
            }]
        else:
            issues = validate_icd_pin_set(
                effective_frozen_pin_mappings(review),
                artifact_content,
                order.target_format,
            )
        return self._append_icd_validation_issues(report, issues)

    def _icd_template_profile(self, order: DocumentWorkOrder):
        """Return the immutable template contract when it can be read safely."""
        reader = getattr(self.store, "read_template_content", None)
        template_version_id = str(getattr(order, "template_version_id", "")).strip()
        if not callable(reader) or not template_version_id:
            return None
        try:
            content = reader(template_version_id)
        except (KeyError, OSError, ValueError):
            return None
        return classify_icd_template(content, order.target_format)

    @staticmethod
    def _has_icd_blocking_issue(issues: list[dict[str, Any]]) -> bool:
        return any(
            issue.get("severity") == "blocking"
            and str(issue.get("code") or "").startswith("icd_")
            for issue in issues
        )

    @staticmethod
    def _has_blocking_validation_issue(issues: list[dict[str, Any]]) -> bool:
        return any(
            bool(issue.get("blocking"))
            or str(issue.get("severity") or "").strip().casefold() == "blocking"
            for issue in issues
            if isinstance(issue, Mapping)
        )

    @staticmethod
    def _append_icd_validation_issues(report, issues: list[dict[str, Any]]):
        if not issues:
            return report
        return report.model_copy(update={
            "issues": [*report.issues, *issues],
            "status": "failed" if report.status == "failed" else "requires_human",
        })

    def download_document_artifact(self, ctx: RequestContext, artifact_id: str) -> bytes:
        artifact = self._artifact_for_context(ctx, artifact_id)
        order = self._order_raw(artifact.work_order_id)
        capability = "download_approved_release" if artifact.stage == "approved_release" else "download_review_candidate"
        self.require_work_order_capability(ctx, order, capability)
        content = self.store.read_artifact_content(artifact_id)
        artifact_format = getattr(artifact, "output_format", None) or order.target_format
        self._assert_artifact_content_clean(content, artifact_format)
        return content

    def convert_document_artifact(
        self,
        ctx: RequestContext,
        artifact_id: str,
        *,
        target_format: str,
    ) -> DocumentArtifact:
        """Create a validated PDF/PPTX child artifact from a native result.

        Conversion is a separate immutable operation.  The source artifact is
        hash-checked before reading, the output receives its own validation
        report and the parent/source hash is recorded in the idempotency key.
        A candidate remains a candidate; an approved release can produce an
        approved derived artifact without silently changing the work order.
        """
        source = self._artifact_for_context(ctx, artifact_id)
        order = self._order(ctx, source.work_order_id, "run_deterministic_work_order")
        if order.scope_type == "knowledge_base" and (
            not order.knowledge_base_name
            or not ctx.has_kb_permission(order.knowledge_base_name, "write")
        ):
            raise PermissionError("knowledge base write permission is required for artifact conversion")
        normalized_target = str(target_format or "").strip().lower().lstrip(".")
        if normalized_target not in self.converter.supported_targets:
            raise ValueError("document artifact conversion supports only pdf or pptx")
        source_format = str(getattr(source, "output_format", None) or order.target_format or "").strip().lower().lstrip(".")
        if source_format == normalized_target:
            raise ValueError("artifact is already in the requested output format")
        get_validation_report = getattr(self.store, "get_validation_report", None)
        source_report = (
            get_validation_report(source.validation_report_id)
            if callable(get_validation_report)
            else None
        )
        if source_report is not None and source_report.status == "failed":
            raise ValueError("source artifact validation failed; conversion is blocked")
        source_content = self.store.read_artifact_content(source.artifact_id)
        try:
            converted = self.converter.convert(
                source_content,
                source_format=source_format,
                target_format=normalized_target,
            )
        except TemplateConversionError:
            raise
        report = ValidationReport(
            validation_report_id=f"validation-{uuid.uuid4().hex}",
            work_order_id=order.work_order_id,
            status="passed",
            issues=[
                {"code": "template_conversion_warning", "message": warning, "severity": "warning"}
                for warning in converted.warnings
            ],
            evidence_matrix_hash=content_hash({
                "source_artifact_id": source.artifact_id,
                "source_content_hash": source.content_hash,
                "target_format": normalized_target,
            }),
            renderer_manifest_hash=content_hash(converted.metadata),
        )
        self.store.save_validation_report(report)
        subject_hash = None
        if source.stage == "approved_release":
            snapshot = self.resolve_source_snapshot(order)
            subject_hash = _approval_subject_hash(
                converted.metadata["content_hash"], report.content_hash, snapshot.content_hash,
            )
        child = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}",
            tenant_id=source.tenant_id,
            work_order_id=source.work_order_id,
            run_id=f"conversion-{uuid.uuid4().hex}",
            output_format=normalized_target,
            stage=source.stage,
            validity_status="current",
            policy_status="active",
            access_status=source.access_status,
            content_hash=converted.metadata["content_hash"],
            approval_subject_hash=subject_hash,
            parent_artifact_id=source.artifact_id,
            validation_report_id=report.validation_report_id,
            approval_event_ids=list(source.approval_event_ids) if source.stage == "approved_release" else [],
            integrity_manifest_id=content_hash({
                "source_artifact_id": source.artifact_id,
                "source_content_hash": source.content_hash,
                "conversion": converted.metadata,
            }),
            idempotency_fingerprint=content_hash({
                "conversion": "template-artifact",
                "source_artifact_id": source.artifact_id,
                "source_content_hash": source.content_hash,
                "target_format": normalized_target,
                "converter_version": converted.metadata["converter_version"],
            }),
            status_reasons=[{
                "code": "semantic_template_conversion",
                "message": "模板成品已按段落/表格语义转换，并完成输出制品校验。",
                "source_artifact_id": source.artifact_id,
                "source_format": source_format,
                "target_format": normalized_target,
                "converter_version": converted.metadata["converter_version"],
                "warnings": list(converted.warnings),
            }],
            released_at=datetime.now(timezone.utc) if source.stage == "approved_release" else None,
        )
        return self._save_artifact_for_task(order, child, converted.content, normalized_target)

    def _assert_artifact_content_clean(self, content: bytes, artifact_format: str) -> None:
        normalized = str(artifact_format or "").strip().lower().lstrip(".")
        if normalized in {"md", "markdown"}:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("generated Markdown artifact is not valid UTF-8") from exc
            return
        if normalized in {"pdf", "pptx"}:
            if normalized == "pdf":
                self.converter._validate_pdf(content)
            else:
                self.converter._validate_pptx(content)
            return
        self._assert_generated_artifact_clean(content, normalized)

    def preview_document_artifact(self, ctx: RequestContext, artifact_id: str) -> dict[str, Any]:
        """Return a bounded, read-only artifact preview after download-equivalent checks."""
        artifact = self._artifact_for_context(ctx, artifact_id)
        order = self._order_raw(artifact.work_order_id)
        capability = "download_approved_release" if artifact.stage == "approved_release" else "download_review_candidate"
        self.require_work_order_capability(ctx, order, capability)
        return preview_artifact(
            self.store.read_artifact_content(artifact_id),
            getattr(artifact, "output_format", None) or order.target_format,
        )

    # Internal helpers -----------------------------------------------------------------

    def _template(self, template_version_id: str) -> TemplateVersion:
        template = self.store.get_template(template_version_id)
        if template is None:
            raise KeyError(f"template not found: {template_version_id}")
        return template

    def _create_default_harness_policy(self) -> HarnessPolicy:
        policy = HarnessPolicy(
            harness_policy_id="default-managed-document-writer",
            version="1",
            status="approved",
            writer_provider_id=LLMManagedWriter.provider_id,
        )
        return self.store.save_harness_policy(policy)

    def _schema_harness_policy(self, schema: DocumentSchema, *, max_parallel_units: int = 3) -> HarnessPolicy:
        """Persist the bounded policy required by one approved semantic schema."""
        unit_count = (
            sum(field.authoring_policy in {"managed_writer", "external_agent_draft"} for field in schema.fields)
            + sum(item.evaluation_mode == "semantic_assisted" for item in schema.review_items)
        )
        attempts = _DEFAULT_RETRIEVAL_ATTEMPTS_PER_UNIT
        retrieval_rounds = max(1, unit_count * attempts)
        # Each unit may spend one extra step on query rewrite (stage 1).
        max_steps = 2 + unit_count * (attempts + 4)
        if (
            unit_count > _MAX_AUTO_HARNESS_UNITS
            or retrieval_rounds > _MAX_AUTO_HARNESS_RETRIEVAL_ROUNDS
            or max_steps > _MAX_AUTO_HARNESS_STEPS
        ):
            raise ValueError(
                "schema semantic unit count exceeds automatic-generation capacity"
            )
        return self.store.save_harness_policy(HarnessPolicy(
            harness_policy_id=(
                f"schema-{schema.document_schema_id}-{schema.version}-managed-writer"
                + ("" if max_parallel_units == 3 else f"-parallel-{max_parallel_units}")
            ),
            version=f"units-{unit_count}-attempts-{attempts}-rewrite",
            status="approved",
            max_units_per_run=max(1, unit_count),
            max_parallel_units=max_parallel_units,
            max_retrieval_attempts_per_unit=attempts,
            max_retrieval_rounds=retrieval_rounds,
            max_steps=max_steps,
            # LLM writer calls can take 30-120s each; give each unit a
            # generous per-lease budget so the lease survives one LLM roundtrip
            # even without between-token heartbeats. Runtime heartbeats between
            # each _step call keep the actual usage well below this ceiling.
            lease_seconds=max(300, unit_count * 120),
            writer_provider_id=LLMManagedWriter.provider_id,
            agent_tools=(
                [
                    "read_field_brief", "retrieve_evidence", "propose_field_value", "mark_missing",
                ]
                if schema.execution_mode == "external_agent" else []
            ),
        ))

    @staticmethod
    def _field_for_suggestion(suggestion, units: list | None = None) -> Any:
        from src.document_authoring.contract_registry import (
            normalized_missing_policy,
            normalized_source_roles,
            normalized_value_type,
            supported_capabilities,
        )
        from src.document_authoring.models import DocumentFieldSchema
        from src.document_authoring.template_analysis import infer_field_contract

        inferred = infer_field_contract(suggestion, units or [])
        llm_eligible = suggestion.confidence >= 0.7
        value_type = normalized_value_type(
            suggestion.value_type if llm_eligible else None
        ) or normalized_value_type(inferred["value_type"]) or "text"
        raw_capabilities = list(suggestion.required_capabilities or []) or list(inferred["required_capabilities"])
        capabilities, unsupported = supported_capabilities(raw_capabilities)
        if unsupported:
            import logging
            logging.getLogger(__name__).warning(
                "suggestion %s carries unsupported capabilities %s; kept deterministic set",
                suggestion.semantic_unit_id, unsupported,
            )
            capabilities, _ = supported_capabilities(list(inferred["required_capabilities"]))
        missing_policy = normalized_missing_policy(
            suggestion.missing_policy if llm_eligible else None
        ) or normalized_missing_policy(inferred["missing_policy"]) or "mark_tbd"
        description = suggestion.description or inferred["description"]
        return DocumentFieldSchema(
            field_id=suggestion.semantic_unit_id,
            label=suggestion.label,
            description=description,
            value_type=value_type,
            required_capabilities=capabilities,
            preferred_source_roles=normalized_source_roles(
                suggestion.preferred_source_roles if llm_eligible else inferred["preferred_source_roles"]
            ),
            missing_policy=missing_policy,
            allow_derivation=bool(suggestion.allow_derivation) if llm_eligible else inferred["allow_derivation"],
            max_evidence_items=5,
            retrieval_policy_id=f"retrieval-{suggestion.semantic_unit_id}",
            verification_policy_id=f"verification-{suggestion.semantic_unit_id}",
            query_terms=list(suggestion.retrieval_terms),
            authoring_policy="managed_writer",
            table_columns=inferred.get("table_columns"),
            # Template-backed tables receive rows from the frozen source set;
            # their row keys are discovered from evidence at execution time.
            # Persist the bounded scope so planning does not confuse a
            # dynamic table with an unresolved table contract.
            table_row_scope=("template_rows" if value_type == "table" else None),
            table_row_order="input" if value_type == "table" else "declared",
        )

    @staticmethod
    def _regions_and_bindings(template: TemplateVersion, analysis) -> tuple[list[Any], list[TemplateUnitBinding]]:
        unit_by_id = {unit.unit_id: unit for unit in analysis.units}
        regions: list[Any] = []
        bindings: list[TemplateUnitBinding] = []
        seen_targets: set[str] = set()
        for suggestion in analysis.suggestions:
            if template.format != "docx" and suggestion.value_shape == "repeating_table":
                from src.document_authoring.table_contracts import table_schema_from_targets
                table = table_schema_from_targets(analysis, suggestion)
                if seen_targets.intersection(suggestion.target_unit_ids):
                    raise ValueError("suggested template targets may only be bound once")
                seen_targets.update(suggestion.target_unit_ids)
                bindings.append(TemplateUnitBinding(
                    binding_id=f"binding-{table.table_region_id}",
                    template_schema_id=template.template_schema_id,
                    template_schema_version=template.template_schema_version,
                    semantic_unit_type="field", semantic_unit_id=suggestion.semantic_unit_id,
                    target_region_ids=[table.table_region_id], table_schema=table,
                ))
                continue
            if template.format != "docx":
                if suggestion.value_shape != "scalar":
                    raise ValueError("workbook repeating tables require an explicit table schema")
                if len(suggestion.target_unit_ids) != 1:
                    raise ValueError("workbook scalar mappings require exactly one target")
            target_regions: list[str] = []
            for unit_id in suggestion.target_unit_ids:
                if unit_id in seen_targets:
                    raise ValueError("suggested template targets may only be bound once")
                unit = unit_by_id[unit_id]
                region_key = f"{template.template_version_id}:{unit_id}"
                region_id = f"region-{hashlib.sha256(region_key.encode('utf-8')).hexdigest()[:16]}"
                if template.format == "docx":
                    regions.append(DocxRegionSchema(
                        region_id=region_id, locator=dict(unit.locator), role="semantic_draft",
                        write_policy="validated_draft", value_type="text",
                    ))
                else:
                    from src.document_authoring.models import WorkbookRegionSchema

                    regions.append(WorkbookRegionSchema(
                        region_id=region_id, sheet_name=str(unit.locator["sheet_name"]),
                        locator={"cell": unit.locator["cell"]}, role="semantic_draft",
                        write_policy="validated_draft", value_type="text",
                        expected_value_hash=unit.value_hash,
                        allow_nonempty_overwrite=(
                            unit.structural_role_hint == "placeholder"
                            or unit_id in analysis.approved_overwrite_unit_ids
                        ),
                    ))
                seen_targets.add(unit_id)
                target_regions.append(region_id)
            binding_key = f"{template.template_version_id}:{suggestion.semantic_unit_id}"
            bindings.append(TemplateUnitBinding(
                binding_id=f"binding-{hashlib.sha256(binding_key.encode('utf-8')).hexdigest()[:16]}",
                template_schema_id=template.template_schema_id,
                template_schema_version=template.template_schema_version,
                semantic_unit_type="field", semantic_unit_id=suggestion.semantic_unit_id,
                target_region_ids=target_regions,
            ))
        return regions, bindings

    @staticmethod
    def _writer_for_policy(policy: HarnessPolicy) -> ManagedWriter:
        if policy.writer_provider_id == DeterministicEvidenceWriter.provider_id:
            return ManagedWriter(DeterministicEvidenceWriter())
        if policy.writer_provider_id == LLMManagedWriter.provider_id:
            # Production managed writing is backed by the shared LangChain
            # model factory. The normal service path probes native structured
            # output first and keeps compatibility behavior inside the writer.
            from src.core.model_factory import create_chat_model

            return ManagedWriter(LLMManagedWriter(model=create_chat_model()))
        raise PermissionError("approved HarnessPolicy references an unsupported managed writer provider")

    @staticmethod
    def _rewriter_for_policy(policy: HarnessPolicy):
        """Build a QueryRewriter only when the frozen policy allows it."""
        if "rewrite_query" in policy.allowed_tools:
            from src.document_authoring.writers.query_rewriter import QueryRewriter
            return QueryRewriter()
        return None

    @staticmethod
    def _reranker_for_policy(policy: HarnessPolicy):
        """Build an EvidenceReranker only when the frozen policy allows it."""
        if "rerank_evidence" in policy.allowed_tools:
            from src.document_authoring.writers.evidence_reranker import EvidenceReranker
            return EvidenceReranker()
        return None

    @staticmethod
    def _fit_checker_for_policy(policy: HarnessPolicy):
        """Build a RequirementFitChecker only when the frozen policy allows it."""
        if "requirement_fit_check" in policy.allowed_tools:
            from src.document_authoring.writers.requirement_fit_checker import RequirementFitChecker
            return RequirementFitChecker()
        return None

    def _schema(self, schema_id: str, version: str) -> DocumentSchema:
        schema = self.store.get_document_schema(schema_id, version)
        if schema is None:
            raise KeyError(f"document schema not found: {schema_id}@{version}")
        return schema

    def _policy(self, template: TemplateVersion) -> RendererPolicy:
        policy = self.store.get_renderer_policy(template.renderer_policy_id)
        if policy is None:
            raise KeyError(f"renderer policy not found: {template.renderer_policy_id}")
        return policy

    def _rule(self, rule_id: str) -> DeterministicRuleSpec:
        spec = self.store.get_rule_spec(rule_id)
        if spec is None:
            raise KeyError(f"deterministic rule not found or not uniquely versioned: {rule_id}")
        return spec

    def _order(self, ctx: RequestContext, work_order_id: str, capability: str) -> DocumentWorkOrder:
        order = self._order_raw(work_order_id)
        self.require_work_order_capability(ctx, order, capability)
        return order

    @staticmethod
    def _ctx_department_id(ctx: RequestContext) -> str | None:
        department_id = ctx.metadata.get("resource_department_id")
        if department_id in (None, ""):
            department_id = ctx.metadata.get("department_id")
        if department_id in (None, ""):
            return None
        return str(department_id)

    @staticmethod
    def _ctx_kb_id(ctx: RequestContext) -> str | None:
        kb_id = ctx.metadata.get("kb_id")
        if kb_id in (None, ""):
            return None
        return str(kb_id)

    def require_work_order_capability(
        self,
        ctx: RequestContext,
        order: DocumentWorkOrder,
        capability: str,
    ) -> None:
        if order.scope_type == "knowledge_base":
            owner_department = order.resource_department_id
            if (
                not order.knowledge_base_name
                or not ctx.has_kb_permission(order.knowledge_base_name, "read")
                or owner_department is None
                or owner_department != self._ctx_department_id(ctx)
            ):
                raise PermissionError(
                    "knowledge base access is required for this work order"
                )
            return
        self.projects.access.require(ctx, order.project_id, capability)

    def resolve_source_snapshot(self, order: DocumentWorkOrder):
        if order.scope_type == "knowledge_base":
            snapshot = self.store.get_knowledge_base_source_snapshot(
                order.source_set_snapshot_id
            )
            if (
                snapshot is None
                or snapshot.tenant_id != order.tenant_id
                or snapshot.knowledge_base_name != order.knowledge_base_name
            ):
                raise ValueError(
                    "work order knowledge-base source snapshot is missing or mismatched"
                )
            return snapshot
        snapshot = self.projects.store.get_source_set_snapshot(
            order.source_set_snapshot_id, order.tenant_id
        )
        if (
            snapshot is None
            or snapshot.project_id != order.project_id
            or snapshot.baseline_id != order.baseline_id
            or snapshot.baseline_content_hash != order.baseline_content_hash
        ):
            raise ValueError(
                "work order project source snapshot is missing or mismatched"
            )
        return snapshot

    @staticmethod
    def build_knowledge_base_retrieval_outcome(
        knowledge_base_name: str,
        source_names: list[str],
        evidences: list[Any],
        *,
        requirement_id: str = "knowledge-base-retrieval",
        source_set_snapshot_id: str = "",
        attachment_ids: Sequence[str] = (),
    ) -> RetrievalOutcome:
        """Bind KB and chat-attachment evidence to frozen source references."""
        frozen_source_names = list(dict.fromkeys(source_names))
        frozen_attachment_ids = {
            str(attachment_id).strip()
            for attachment_id in attachment_ids
            if str(attachment_id).strip()
        }
        accepted = []
        attachment_evidence_by_id: dict[str, list[str]] = {}
        for evidence in evidences:
            metadata = dict(getattr(evidence, "metadata", {}) or {})
            source_type = str(
                metadata.get("source_type")
                or getattr(evidence, "source_type", "")
                or ""
            ).strip()
            if source_type == "chat_attachment":
                attachment_id = str(metadata.get("attachment_id") or "").strip()
                if not attachment_id or attachment_id not in frozen_attachment_ids:
                    raise PermissionError(
                        "retrieval evidence attachment is outside the frozen attachment set"
                    )
                bound_evidence = copy(evidence)
                bound_evidence.metadata = {
                    **metadata,
                    "source_type": "chat_attachment",
                    "attachment_id": attachment_id,
                }
                accepted.append(bound_evidence)
                attachment_evidence_by_id.setdefault(attachment_id, []).append(
                    str(getattr(evidence, "id", ""))
                )
                continue
            if evidence.source_name not in frozen_source_names:
                raise PermissionError("retrieval evidence is outside the frozen source set")
            declared_kb_names = {
                str(metadata.get(key) or "").strip()
                for key in ("knowledge_base_name", "kb_name")
            } - {""}
            if any(name != knowledge_base_name for name in declared_kb_names):
                raise PermissionError("retrieval evidence knowledge base does not match selection")
            bound_evidence = copy(evidence)
            bound_evidence.metadata = {
                **metadata,
                "knowledge_base_name": knowledge_base_name,
                "source_type": "knowledge_base",
            }
            accepted.append(bound_evidence)
        evidence_by_source = {
            source_name: [
                evidence.id
                for evidence in accepted
                if evidence.source_name == source_name
            ]
            for source_name in frozen_source_names
        }
        return RetrievalOutcome(
            requirement_id=requirement_id,
            status="success_with_hits" if accepted else "success_empty",
            evidences=accepted,
            source_outcomes=[
                RetrievalSourceOutcome(
                    source_version_id=source_name,
                    status=(
                        "success_with_hits"
                        if evidence_by_source[source_name]
                        else "success_empty"
                    ),
                    evidence_ids=evidence_by_source[source_name],
                )
                for source_name in frozen_source_names
            ] + [
                RetrievalSourceOutcome(
                    source_version_id=f"attachment:{attachment_id}",
                    status="success_with_hits" if evidence_ids else "success_empty",
                    evidence_ids=evidence_ids,
                )
                for attachment_id, evidence_ids in attachment_evidence_by_id.items()
            ],
            query_fingerprint=hashlib.sha256(
                (
                    f"{requirement_id}|{knowledge_base_name}|"
                    f"{'|'.join(frozen_source_names)}|"
                    f"{'|'.join(evidence.id for evidence in accepted)}"
                ).encode("utf-8")
            ).hexdigest(),
            applied_source_set_snapshot_id=source_set_snapshot_id,
            applied_region_policy_versions={},
        )

    def _create_knowledge_base_source_snapshot(
        self,
        ctx: RequestContext,
        knowledge_base_name: str,
        source_names: list[str],
    ) -> KnowledgeBaseSourceSnapshot:
        snapshot = KnowledgeBaseSourceSnapshot.create(
            tenant_id=ctx.tenant_id or "default",
            knowledge_base_name=knowledge_base_name,
            source_names=source_names,
            created_by=ctx.user_id,
        )
        return self.store.create_knowledge_base_source_snapshot(snapshot)

    def _order_raw(self, work_order_id: str) -> DocumentWorkOrder:
        order = self.store.get_work_order(work_order_id)
        if order is None:
            raise KeyError(f"work order not found: {work_order_id}")
        return order

    def _artifact_for_context(self, ctx: RequestContext, artifact_id: str) -> DocumentArtifact:
        artifact = self.store.get_artifact(artifact_id)
        if artifact is None:
            raise KeyError("artifact not found")
        return artifact

    def list_knowledge_base_work_orders_for_context(
        self,
        ctx: RequestContext,
        knowledge_base_name: str,
    ) -> list[DocumentWorkOrder]:
        if not ctx.has_kb_permission(knowledge_base_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        department_id = self._ctx_department_id(ctx)
        orders = self.store.list_work_orders_for_knowledge_base(
            ctx.tenant_id or "default", knowledge_base_name
        )
        return [
            order
            for order in orders
            if order.resource_department_id is not None
            and order.resource_department_id == department_id
        ]

    def _harness_run_for_context(self, ctx: RequestContext, harness_run_id: str):
        run = self.store.get_harness_run(harness_run_id)
        if run is None:
            raise KeyError(f"harness run not found: {harness_run_id}")
        order = self._order_raw(run.work_order_id)
        self.require_work_order_capability(
            ctx, order, "run_deterministic_work_order"
        )
        return run

    def _replace_order(self, order: DocumentWorkOrder, **updates: Any) -> DocumentWorkOrder:
        revised = order.model_copy(update={
            **updates,
            "updated_at": datetime.now(timezone.utc),
            "lock_version": order.lock_version + 1,
        })
        persisted = self.store.replace_work_order(revised)
        self._sync_task_status(persisted, persisted.status)
        return persisted

    def _associate_task_run(self, order: Any, run_id: str) -> None:
        task_id = str(getattr(order, "task_id", None) or "").strip()
        if task_id:
            self.task_service.store.attach_run(task_id, run_id)

    def _save_artifact_for_task(
        self,
        order: Any,
        artifact: DocumentArtifact,
        content: bytes,
        suffix: str,
    ) -> DocumentArtifact:
        revision_id = str(getattr(order, "revision_id", None) or "").strip()
        if revision_id and not str(getattr(artifact, "parent_artifact_id", None) or "").strip():
            revision = self.revision_service.store.get(revision_id)
            if revision is not None:
                artifact = artifact.model_copy(update={
                    "parent_artifact_id": revision.parent_artifact_id,
                })
        persisted = self.store.save_artifact(artifact, content, suffix)
        task_id = str(getattr(order, "task_id", None) or "").strip()
        if task_id:
            if getattr(persisted, "run_id", None):
                self.task_service.store.attach_run(task_id, persisted.run_id)
            self.task_service.store.attach_artifact(task_id, persisted.artifact_id)
        return persisted

    def _bind_revision_child(self, order: Any, artifact: Any, *, report_status: str) -> None:
        """Bind a finalized candidate as the child of the order's revision.

        Worker-path completion: no user context exists here, so lineage is
        re-verified inside ``bind_generated_child`` against stored facts.  A
        failure must not lose the generated artifact, so binding errors are
        logged and surfaced through the revision state instead.
        """
        if not str(getattr(order, "revision_id", None) or "").strip():
            return
        revalidation_status = {
            "passed": "passed",
            "requires_human": "requires_human",
            "failed": "failed",
        }.get(str(report_status or "").casefold())
        if revalidation_status is None:
            return
        try:
            self.revision_service.bind_generated_child(
                order,
                artifact,
                revalidation_status=revalidation_status,
                revalidation_result={
                    "validation_report_id": str(getattr(artifact, "validation_report_id", "") or ""),
                    "work_order_status_report": str(report_status),
                },
            )
        except Exception:
            logger.exception(
                "failed to bind revision child artifact %s for work order %s",
                getattr(artifact, "artifact_id", "?"), getattr(order, "work_order_id", "?"),
            )
            raise

    def _sync_task_status(self, order: Any, work_order_status: str) -> None:
        task_id = str(getattr(order, "task_id", None) or "").strip()
        if not task_id:
            return
        task_status = {
            "planned": "planned",
            "retrieving": "running",
            "ready_to_draft": "running",
            "drafting": "running",
            "validating": "running",
            "ready_to_render": "running",
            "rendering": "running",
            "waiting_human_input": "needs_clarification",
            "waiting_human_approval": "waiting_human",
            "paused": "waiting_human",
            "complete": "completed",
            "failed": "failed",
            "blocked": "failed",
            "cancelled": "cancelled",
        }.get(str(work_order_status or "").strip(), "running")
        try:
            self.task_service.store.update_status(task_id, task_status)
        except Exception:
            # The task projection is additive during migration.  Do not turn
            # a successfully persisted WorkOrder transition into a failed
            # generation request if the projection store is temporarily
            # unavailable; reconciliation can replay the status event.
            logger.warning(
                "failed to project WorkOrder %s status to DocumentTask %s",
                getattr(order, "work_order_id", ""),
                task_id,
                exc_info=True,
            )

    @staticmethod
    def _semantic_fills(
        template: TemplateVersion,
        drafts: list[DocumentUnitDraft],
        statuses: dict[str, str],
        bindings: dict[str, TemplateUnitBinding],
    ) -> WorkbookFillPlan | DocxFillPlan:
        fills: list[WorkbookFill] | list[DocxFill] = []
        table_fills: list[WorkbookTableFill] = []
        for draft in drafts:
            if draft.validation_status != "supported" or statuses.get(draft.unit_id) != "ready_to_render":
                continue
            semantic_unit_id = draft.unit_id.split(":", 1)[-1]
            binding = bindings.get(semantic_unit_id)
            if binding is None:
                continue
            if draft.typed_value is None:
                continue
            if binding.table_schema is not None:
                if draft.typed_value.kind != "table":
                    raise ValueError("table binding requires a typed table draft")
                columns = {col.column_id for col in binding.table_schema.columns}
                if not draft.typed_value.rows or any(set(row.cells) != columns for row in draft.typed_value.rows):
                    raise ValueError("table rows must match all mapped columns exactly")
                rows = list(draft.typed_value.rows)
                expected_row_keys = list(binding.table_schema.expected_row_keys)
                row_keys = [row.row_key.strip() for row in rows]
                if expected_row_keys:
                    if any(not row_key for row_key in row_keys):
                        raise ValueError("table rows require server-owned row keys")
                    if set(row_keys) != set(expected_row_keys):
                        missing = [key for key in expected_row_keys if key not in set(row_keys)]
                        unexpected = [key for key in row_keys if key not in set(expected_row_keys)]
                        raise ValueError(
                            "table row keys do not match expected scope: "
                            f"missing={missing}, unexpected={unexpected}"
                        )
                    if len(row_keys) != len(set(row_keys)) and binding.table_schema.duplicate_policy == "reject":
                        raise ValueError("duplicate table row keys are not allowed")
                    order = {key: index for index, key in enumerate(expected_row_keys)}
                    rows.sort(key=lambda row: order[row.row_key])
                elif binding.table_schema.row_order == "stable_key":
                    if any(not row_key for row_key in row_keys):
                        raise ValueError("stable table row ordering requires row keys")
                    rows.sort(key=lambda row: row.row_key)
                    if len(row_keys) != len(set(row_keys)) and binding.table_schema.duplicate_policy == "reject":
                        raise ValueError("duplicate table row keys are not allowed")
                elif len(row_keys) != len(set(row_keys)) and any(row_keys) and binding.table_schema.duplicate_policy == "reject":
                    raise ValueError("duplicate table row keys are not allowed")
                table_fills.append(WorkbookTableFill(
                    table_region_id=binding.table_schema.table_region_id,
                    semantic_unit_id=semantic_unit_id,
                    rows=[WorkbookTableRowFill(
                        row_key=row.row_key,
                        cells=dict(row.cells),
                        evidence_ids=list(row.evidence_ids),
                        cell_evidence_ids={
                            column: list(evidence_ids)
                            for column, evidence_ids in row.cell_evidence_ids.items()
                        },
                    ) for row in rows],
                ))
                continue
            if draft.typed_value.kind == "table":
                raise ValueError("typed table requires a registered table binding")
            if template.format != "docx" and len(binding.target_region_ids) != 1:
                raise ValueError("workbook scalar bindings require exactly one target")
            value = draft.typed_value.display_value
            for region_id in binding.target_region_ids:
                if template.format == "docx":
                    fills.append(DocxFill(region_id=region_id, value=str(value), semantic_unit_id=semantic_unit_id))
                else:
                    fills.append(WorkbookFill(region_id=region_id, value=str(value), semantic_unit_id=semantic_unit_id))
        plan = DocumentGenerationService._fill_plan(template, fills)
        if isinstance(plan, WorkbookFillPlan):
            plan.table_fills = table_fills
        return plan

    @staticmethod
    def _fill_plan(
        template: TemplateVersion,
        fills: list[WorkbookFill] | list[DocxFill],
    ) -> WorkbookFillPlan | DocxFillPlan:
        if template.format == "docx":
            return DocxFillPlan(template_version_id=template.template_version_id, fills=fills)
        return WorkbookFillPlan(template_version_id=template.template_version_id, fills=fills)

    @staticmethod
    def _table_schemas_for_fill_plan(
        table_schemas: list[Any],
        fill_plan: Any,
    ) -> list[Any]:
        """Allow validated table fills to exceed an activation-time row bound.

        ``max_output_rows`` was recorded when the template mapping was
        activated, but a frozen dynamic table (for example the EDF connector
        pin set) may legitimately contain more rows than the template sample.
        The fill rows have already passed coverage and unit review, so the
        renderer bound is widened to the validated row count instead of
        rejecting the deliverable.
        """

        schemas = list(table_schemas)
        table_fills = list(getattr(fill_plan, "table_fills", []) or [])
        if not table_fills:
            return schemas
        by_id = {schema.table_region_id: index for index, schema in enumerate(schemas)}
        for fill in table_fills:
            index = by_id.get(fill.table_region_id)
            if index is None:
                continue
            schema = schemas[index]
            row_count = len(list(getattr(fill, "rows", []) or []))
            if row_count > int(getattr(schema, "max_output_rows", 0) or 0):
                schemas[index] = schema.model_copy(update={"max_output_rows": row_count})
        return schemas

    def _render_fill_plan(
        self,
        template: TemplateVersion,
        fill_plan: WorkbookFillPlan | DocxFillPlan,
    ) -> tuple[bytes, dict]:
        if fill_plan.template_version_id != template.template_version_id:
            raise PermissionError("FillPlan belongs to a different frozen template version")
        content = self._read_hash_bound_template_content(template)
        policy = self._policy(template)
        if template.format in {"xlsx", "xlsm"}:
            if not isinstance(fill_plan, WorkbookFillPlan):
                raise TypeError("XLSX/XLSM templates require WorkbookFillPlan")
            result = self.workbook_renderer.render(
                content,
                self.store.list_workbook_regions(template.template_schema_id, template.template_schema_version),
                fill_plan,
                policy,
                security_approved=True,
                table_schemas=self._table_schemas_for_fill_plan(
                    [
                        binding.table_schema
                        for binding in self.store.list_unit_bindings(
                            template.template_schema_id, template.template_schema_version,
                        )
                        if binding.table_schema is not None
                    ],
                    fill_plan,
                ),
            )
        elif template.format == "docx":
            if not isinstance(fill_plan, DocxFillPlan):
                raise TypeError("DOCX templates require DocxFillPlan")
            result = self.docx_renderer.render(
                content,
                self.store.list_docx_regions(template.template_schema_id, template.template_schema_version),
                fill_plan,
                policy,
                security_approved=True,
            )
        else:
            raise ValueError(f"unsupported controlled output format: {template.format}")
        return result.content, result.integrity_manifest

    def _inspect(self, content: bytes, format: str):
        if format == "docx":
            return self.docx_renderer.inspect(content)
        return self.workbook_renderer.inspect(content, format)

    def _assert_generated_artifact_clean(self, content: bytes, format: str) -> None:
        normalized = str(format or "").strip().lower().lstrip(".")
        if normalized in {"xlsx", "xlsm", "docx"}:
            validate_ooxml_package(content, normalized)
        if self._inspect(content, format).active_content_status != "clean":
            raise ValueError("generated artifact contains active content")

    def _read_hash_bound_template_content(self, template: TemplateVersion) -> bytes:
        """Return only bytes that still match the frozen template analysis and version."""
        content = self.store.read_template_content(template.template_version_id)
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != template.content_hash:
            raise ValueError("template content hash changed since confirmation")
        get_analysis = getattr(self.store, "get_template_analysis", None)
        analysis = get_analysis(template.template_version_id) if get_analysis is not None else None
        if analysis is not None and analysis.content_hash != actual_hash:
            raise ValueError("template content hash no longer matches its analysis")
        return content

    @staticmethod
    def _validate_retrieval_outcome(order: DocumentWorkOrder, snapshot, outcome: RetrievalOutcome) -> None:
        if outcome.applied_source_set_snapshot_id != snapshot.source_set_snapshot_id:
            raise PermissionError("retrieval outcome was not produced for this work order source set")
        if order.scope_type == "knowledge_base":
            if outcome.applied_region_policy_versions:
                raise PermissionError(
                    "knowledge base retrieval outcome used unexpected region policies"
                )
            source_scope = order.source_scope_snapshot or "knowledge_base_only"
            if source_scope == "auto":
                source_scope = (
                    "attachment_and_knowledge_base"
                    if order.attachment_refs_snapshot
                    else "knowledge_base_only"
                )
            allowed_attachment_refs = {
                str(ref.get("attachment_id") or "").strip(): ref
                for ref in order.attachment_refs_snapshot
                if str(ref.get("attachment_id") or "").strip()
            }
            for evidence in outcome.evidences:
                metadata = dict(getattr(evidence, "metadata", {}) or {})
                source_type = str(
                    metadata.get("source_type")
                    or getattr(evidence, "source_type", "")
                    or ""
                ).strip()
                if source_type == "chat_attachment":
                    if source_scope not in {"attachment_only", "attachment_and_knowledge_base"}:
                        raise PermissionError(
                            "retrieval outcome contains attachment evidence outside its source scope"
                        )
                    attachment_id = str(metadata.get("attachment_id") or "").strip()
                    frozen_ref = allowed_attachment_refs.get(attachment_id)
                    if frozen_ref is None:
                        raise PermissionError(
                            "retrieval evidence attachment is outside the work order snapshot"
                        )
                    asset_id = str(metadata.get("asset_id") or "").strip()
                    frozen_asset_id = str(frozen_ref.get("asset_id") or "").strip()
                    if asset_id and frozen_asset_id and asset_id != frozen_asset_id:
                        raise PermissionError(
                            "retrieval evidence attachment asset does not match the work order snapshot"
                        )
                    continue
                if source_scope == "attachment_only":
                    raise PermissionError(
                        "retrieval outcome contains knowledge-base evidence outside its source scope"
                    )
                if metadata.get("knowledge_base_name") != order.knowledge_base_name:
                    raise PermissionError(
                        "retrieval evidence knowledge base does not match work order"
                    )
                if evidence.source_name not in snapshot.source_names:
                    raise PermissionError(
                        "retrieval evidence is outside the frozen source set"
                    )
            return
        allowed_versions = set(snapshot.source_version_ids) | set(snapshot.shared_reference_version_ids)
        allowed_artifacts = set(snapshot.processing_artifact_ids)
        if outcome.applied_region_policy_versions != snapshot.region_policy_versions:
            raise PermissionError("retrieval outcome used different source region policies")
        for evidence in outcome.evidences:
            if getattr(evidence, "project_id", None) != order.project_id:
                raise PermissionError("retrieval evidence project scope does not match work order")
            if getattr(evidence, "source_version_id", None) not in allowed_versions:
                raise PermissionError("retrieval evidence source version is outside frozen source set")
            if getattr(evidence, "processing_artifact_id", None) not in allowed_artifacts:
                raise PermissionError("retrieval evidence processing artifact is outside frozen source set")

    def _actor_role(self, ctx: RequestContext, project_id: str) -> str:
        bindings = self.projects.access.active_bindings(ctx, project_id)
        priority = {"viewer": 0, "author": 1, "reviewer": 2, "approver": 3, "project_admin": 4}
        return max((binding.project_role for binding in bindings), key=lambda role: priority[role], default="viewer")

    @staticmethod
    def _coverage_status(result_status: str, outcome: RetrievalOutcome | None) -> str:
        if outcome is None:
            return "retrieval_failed"
        if outcome.status in {"retrieval_failed", "source_unavailable", "access_denied", "partial_failure"}:
            return outcome.status
        return "supported" if result_status in {"passed", "failed"} else result_status


def _result_label(status: str, display: str) -> str:
    labels = {
        "passed": "PASS",
        "failed": "FAIL",
        "insufficient_evidence": "TBD",
        "requires_human": "REQUIRES HUMAN",
        "retrieval_failed": "RETRIEVAL FAILED",
        "conflicting": "CONFLICT",
    }
    suffix = f": {display}" if display else ""
    return labels.get(status, status.upper()) + suffix


def _approval_subject_hash(content: str, report: str, snapshot: str) -> str:
    return content_hash({
        "artifact_content_hash": content,
        "validation_report_hash": report,
        "source_set_snapshot_hash": snapshot,
    })
