"""P2a document-generation service.

This service owns the use-case transaction boundaries.  UI, a future REST API,
the background worker and external adapters call it instead of manipulating
templates, project stores or artifacts directly.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from copy import copy
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

import requests

from src.agents.claim_evidence import RetrievalOutcome, RetrievalSourceOutcome
from src.document_authoring.deterministic_rules import DeterministicRuleExecutor
from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
from src.document_authoring.models import (
    DeterministicRuleSpec,
    DocumentArtifact,
    DocumentHumanEvent,
    DocumentUnitDraft,
    HarnessPolicy,
    KnowledgeBaseSourceSnapshot,
    LegacyTemplateClaim,
    DocumentSchema,
    DocumentWorkOrder,
    RendererPolicy,
    TemplateSanitizationReport,
    TemplateUnitBinding,
    TemplateVersion,
    DocxFill,
    DocxFillPlan,
    WorkbookFill,
    WorkbookFillPlan,
    WorkbookRegionSchema,
    content_hash,
)
from src.document_authoring.renderers.docx import DocxRenderer
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.template_activation import decide_template_activation
from src.document_authoring.template_analysis import (
    DocxRegionSchema,
    TemplateAnalysis,
    TemplateMappingCorrection,
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
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.worker import DocumentGenerationWorker
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, LLMManagedWriter, ManagedWriter
from src.core.llm_client import LLMClient
from src.pipelines.document_rag.schemas import RequestContext
from src.projects.service import ProjectService


logger = logging.getLogger(__name__)

_MAX_AUTO_HARNESS_UNITS = 500
_MAX_AUTO_HARNESS_RETRIEVAL_ROUNDS = 1_000
# max_steps = 2 + unit_count * (attempts + 4); with 500 units and attempts=2
# that is 3002, so the ceiling must leave headroom for the rewrite step.
_MAX_AUTO_HARNESS_STEPS = 3_600
_DEFAULT_RETRIEVAL_ATTEMPTS_PER_UNIT = 2


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
        self.workbook_renderer = renderer or XlsmRenderer()
        # Keep the existing public attribute for callers that supplied the
        # XLSX/XLSM renderer before DOCX support was introduced.
        self.renderer = self.workbook_renderer
        self.docx_renderer = docx_renderer or DocxRenderer()
        self.rules = DeterministicRuleExecutor()
        self.validator = DocumentValidator()
        self.worker = worker or DocumentGenerationWorker()
        self.harness_runtime = InternalDocumentHarnessRuntime(self.store, self.validator)
        self.template_suggester = suggestion_provider or LLMTemplateSuggestionProvider(LLMClient())

    # Template and schema registration -------------------------------------------------

    def register_renderer_policy(self, policy: RendererPolicy) -> RendererPolicy:
        return self.store.save_renderer_policy(policy)

    def register_document_schema(self, schema: DocumentSchema) -> DocumentSchema:
        return self.store.save_document_schema(schema)

    def register_deterministic_rule(self, spec: DeterministicRuleSpec) -> DeterministicRuleSpec:
        return self.store.save_rule_spec(spec)

    def register_harness_policy(self, policy: HarnessPolicy) -> HarnessPolicy:
        return self.store.save_harness_policy(policy)

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
        seen_regions = {region.region_id for region in regions}
        if len(seen_regions) != len(regions):
            raise ValueError("template region ids must be unique")
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
            activation_decision = decide_template_activation(analysis)
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
        current = self.store.get_template_analysis(analysis.template_version_id)
        if current is None or current.analysis_id != analysis.analysis_id:
            raise ValueError("template analysis correction is stale")
        template = self._template(analysis.template_version_id)
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
        return self.store.save_template_analysis(corrected)

    def confirm_template_analysis(
        self,
        ctx: RequestContext,
        *,
        analysis_id: str,
        display_name: str,
    ) -> TemplateVersion:
        analysis = self.store.get_template_analysis_by_id(analysis_id)
        if analysis is None:
            raise KeyError(f"template analysis not found: {analysis_id}")
        if analysis.status != "ready_for_confirmation":
            raise ValueError("template analysis requires human exception corrections before confirmation")
        current = self.store.get_template_analysis(analysis.template_version_id)
        if current is None or current.analysis_id != analysis.analysis_id:
            raise ValueError("template analysis is not the current revision")
        template = self._template(analysis.template_version_id)
        actual_hash = hashlib.sha256(self.store.read_template_content(template.template_version_id)).hexdigest()
        if actual_hash != analysis.content_hash or actual_hash != template.content_hash:
            raise ValueError("template content hash changed since analysis")
        analysis.validate_suggestions()
        regions, bindings = self._regions_and_bindings(template, analysis)
        schema = DocumentSchema(
            document_schema_id=template.template_schema_id,
            version=template.template_schema_version,
            document_type=display_name.strip() or template.template_id,
            fields=[
                self._field_for_suggestion(suggestion)
                for suggestion in analysis.suggestions
            ],
            status="approved",
            execution_mode="internal_harness" if analysis.suggestions else "deterministic_only",
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
    ) -> DocumentWorkOrder:
        tenant_id = ctx.tenant_id or "default"
        self.projects.access.require(ctx, project_id, "create_work_order")
        if idempotency_key:
            existing = self.store.find_work_order_by_idempotency(tenant_id, project_id, idempotency_key)
            if existing is not None:
                return existing
        self._validate_work_order_definition(
            template_version_id,
            document_schema_id,
            document_schema_version,
            harness_policy_id,
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
    ) -> DocumentWorkOrder:
        if not ctx.has_kb_permission(knowledge_base_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        tenant_id = ctx.tenant_id or "default"
        if idempotency_key:
            existing = self._find_knowledge_base_work_order_by_idempotency(
                tenant_id,
                knowledge_base_name,
                idempotency_key,
            )
            if existing is not None:
                return existing
        self._validate_work_order_definition(
            template_version_id,
            document_schema_id,
            document_schema_version,
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
            )
        except sqlite3.IntegrityError:
            if idempotency_key:
                existing = self._find_knowledge_base_work_order_by_idempotency(
                    tenant_id,
                    knowledge_base_name,
                    idempotency_key,
                )
                if existing is not None:
                    return existing
            raise

    def _find_knowledge_base_work_order_by_idempotency(
        self,
        tenant_id: str,
        knowledge_base_name: str,
        idempotency_key: str,
    ) -> DocumentWorkOrder | None:
        return next(
            (
                order
                for order in self.store.list_work_orders_for_knowledge_base(
                    tenant_id, knowledge_base_name
                )
                if order.idempotency_key == idempotency_key
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
    ) -> DocumentWorkOrder:
        template = self._template(template_version_id)
        schema = self._schema(document_schema_id, document_schema_version)
        if template.status != "approved" or schema.status != "approved":
            raise ValueError(
                "document generation requires approved template and document schema"
            )
        if schema.execution_mode not in {"deterministic_only", "internal_harness"}:
            raise ValueError("external-agent work orders are not enabled before P3")
        harness_policy = None
        if schema.execution_mode == "internal_harness":
            if harness_policy_id:
                harness_policy = self.store.get_harness_policy(harness_policy_id)
            else:
                harness_policy = self._schema_harness_policy(schema)
                harness_policy_id = harness_policy.harness_policy_id
            if harness_policy is None or harness_policy.status != "approved":
                raise ValueError(
                    "internal-harness work orders require an approved HarnessPolicy"
                )
        is_knowledge_base = scope_type == "knowledge_base"
        order = DocumentWorkOrder(
            work_order_id=(
                f"wo-{uuid.uuid4().hex}"
                if is_knowledge_base
                else snapshot.work_order_id
            ),
            tenant_id=snapshot.tenant_id,
            scope_type=scope_type,
            knowledge_base_name=knowledge_base_name if is_knowledge_base else None,
            project_id=None if is_knowledge_base else snapshot.project_id,
            baseline_id=None if is_knowledge_base else snapshot.baseline_id,
            baseline_content_hash=(
                "" if is_knowledge_base else snapshot.baseline_content_hash
            ),
            source_set_snapshot_id=snapshot.source_set_snapshot_id,
            template_version_id=template.template_version_id,
            document_schema_id=schema.document_schema_id,
            document_schema_version=schema.version,
            template_schema_id=template.template_schema_id,
            template_schema_version=template.template_schema_version,
            retrieval_policy_version="1",
            renderer_policy_version=self._policy(template).version,
            target_format=template.format,
            execution_mode=schema.execution_mode,
            harness_policy_id=harness_policy_id,
            harness_policy_version=harness_policy.version if harness_policy else None,
            unit_statuses={
                **{item.field_id: "planned" for item in schema.fields},
                **{item.review_item_id: "planned" for item in schema.review_items},
            },
            created_by=ctx.user_id,
            idempotency_key=idempotency_key,
        )
        return self.store.create_work_order(order)

    def _validate_work_order_definition(
        self,
        template_version_id: str,
        document_schema_id: str,
        document_schema_version: str,
        harness_policy_id: str | None = None,
    ) -> None:
        template = self._template(template_version_id)
        schema = self._schema(document_schema_id, document_schema_version)
        if template.status != "approved" or schema.status != "approved":
            raise ValueError(
                "document generation requires approved template and document schema"
            )
        if schema.execution_mode not in {"deterministic_only", "internal_harness"}:
            raise ValueError("external-agent work orders are not enabled before P3")
        if schema.execution_mode == "internal_harness" and harness_policy_id:
            policy = self.store.get_harness_policy(harness_policy_id)
            if policy is None or policy.status != "approved":
                raise ValueError(
                    "internal-harness work orders require an approved HarnessPolicy"
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
        """Create, run, validate, and release a document without approval clicks.

        A candidate is released only when the Harness reaches
        ``waiting_human_approval``. Evidence gaps, conflicts, or validation
        findings remain a review candidate and therefore still require a human.
        """
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
        candidate = self.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)
        current = self.store.get_work_order(order.work_order_id)
        if current is None or current.status != "waiting_human_approval":
            return candidate
        return self.approve_document_artifact(
            ctx, candidate.artifact_id, comment="自动生成并发布",
        )

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
        self.store.save_evidence_matrix(order.work_order_id, matrix_rows)
        self.store.save_validation_report(report)
        artifact = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}", tenant_id=order.tenant_id,
            work_order_id=order.work_order_id, run_id=f"run-{uuid.uuid4().hex}",
            stage="review_candidate", content_hash=hashlib.sha256(rendered_content).hexdigest(),
            validation_report_id=report.validation_report_id,
            integrity_manifest_id=integrity_manifest["manifest_hash"],
        )
        artifact = self.store.save_artifact(artifact, rendered_content, template.format)
        next_status = "waiting_human_approval" if report.status in {"passed", "requires_human"} else "blocked"
        self._replace_order(order, status=next_status, unit_statuses=statuses,
                            evidence_matrix_id=f"matrix-{order.work_order_id}", validation_report_id=report.validation_report_id)
        return artifact

    # Internal Harness / semantic-assisted execution ----------------------------------

    def run_internal_harness(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        retrieve: Callable[[Any, int, "str | None"], RetrievalOutcome],
        writer: ManagedWriter | None = None,
    ) -> DocumentArtifact:
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        if order.execution_mode != "internal_harness" or not order.harness_policy_id:
            raise ValueError("work order is not configured for the internal Harness")
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
        schema = self._schema(order.document_schema_id, order.document_schema_version)
        snapshot = self.resolve_source_snapshot(order)
        template = self._template(order.template_version_id)
        run, manifest = self.harness_runtime.create_run(order, policy, snapshot, template, schema)
        order = self._replace_order(order, status="retrieving", run_manifest_id=manifest.run_manifest_id)
        try:
            result = self.harness_runtime.execute(
                work_order=order, run=run, manifest=manifest, policy=policy, schema=schema, snapshot=snapshot,
                legacy_claims=self.store.list_legacy_template_claims(template.template_version_id),
                writer=writer, retrieve=retrieve, rewriter=rewriter, reranker=reranker,
                fit_checker=fit_checker,
            )
        except Exception:
            current_run = self.store.get_harness_run(run.harness_run_id)
            if current_run is not None and current_run.status == "failed":
                self._replace_order(order, status="blocked")
            raise
        return self._finalize_internal_harness_result(order, template, run.harness_run_id, result)

    def pause_harness_run(self, ctx: RequestContext, harness_run_id: str):
        run = self._harness_run_for_context(ctx, harness_run_id)
        paused = self.store.request_harness_run_state(harness_run_id, "paused")
        if paused.checkpoint_id:
            self.store.finalize_harness_checkpoint(paused.checkpoint_id, "paused")
        order = self._order_raw(run.work_order_id)
        if order.status != "cancelled":
            self._replace_order(order, status="blocked")
        return paused

    def cancel_harness_run(self, ctx: RequestContext, harness_run_id: str):
        run = self._harness_run_for_context(ctx, harness_run_id)
        cancelled = self.store.request_harness_run_state(harness_run_id, "cancelled")
        if cancelled.checkpoint_id:
            self.store.finalize_harness_checkpoint(cancelled.checkpoint_id, "cancelled")
        order = self._order_raw(run.work_order_id)
        self._replace_order(order, status="cancelled")
        return cancelled

    def resume_internal_harness(
        self,
        ctx: RequestContext,
        harness_run_id: str,
        *,
        retrieve: Callable[[Any, int, "str | None"], RetrievalOutcome],
        writer: ManagedWriter | None = None,
    ) -> DocumentArtifact:
        run = self._harness_run_for_context(ctx, harness_run_id)
        order = self._order_raw(run.work_order_id)
        if order.execution_mode != "internal_harness" or not order.harness_policy_id:
            raise ValueError("work order is not configured for the internal Harness")
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
        schema = self._schema(order.document_schema_id, order.document_schema_version)
        snapshot = self.resolve_source_snapshot(order)
        template = self._template(order.template_version_id)
        order = self._replace_order(order, status="retrieving", run_manifest_id=manifest.run_manifest_id)
        try:
            result = self.harness_runtime.execute(
                work_order=order, run=run, manifest=manifest, policy=policy, schema=schema, snapshot=snapshot,
                legacy_claims=self.store.list_legacy_template_claims(template.template_version_id),
                writer=writer, retrieve=retrieve, rewriter=rewriter, reranker=reranker,
                fit_checker=fit_checker,
            )
        except Exception:
            current_run = self.store.get_harness_run(run.harness_run_id)
            if current_run is not None and current_run.status == "failed":
                self._replace_order(order, status="blocked")
            raise
        return self._finalize_internal_harness_result(order, template, run.harness_run_id, result)

    def _finalize_internal_harness_result(
        self,
        order: DocumentWorkOrder,
        template: TemplateVersion,
        harness_run_id: str,
        result,
    ) -> DocumentArtifact:
        bindings = self.store.list_unit_bindings(order.template_schema_id, order.template_schema_version)
        binding_by_unit = {binding.semantic_unit_id: binding for binding in bindings}
        fills = self._semantic_fills(template, result.drafts, result.unit_statuses, binding_by_unit)
        rendered_content, integrity_manifest = self._render_fill_plan(template, fills)
        self._assert_generated_artifact_clean(rendered_content, template.format)
        report = self.validator.validate(
            work_order_id=order.work_order_id, matrix_rows=result.matrix_rows,
            integrity_manifest=integrity_manifest, additional_issues=result.issues,
        )
        self.store.save_evidence_matrix(order.work_order_id, result.matrix_rows)
        self.store.save_validation_report(report)
        artifact = DocumentArtifact(
            artifact_id=f"artifact-{uuid.uuid4().hex}", tenant_id=order.tenant_id,
            work_order_id=order.work_order_id, run_id=harness_run_id,
            stage="review_candidate", content_hash=hashlib.sha256(rendered_content).hexdigest(),
            validation_report_id=report.validation_report_id,
            integrity_manifest_id=integrity_manifest["manifest_hash"],
        )
        artifact = self.store.save_artifact(artifact, rendered_content, template.format)
        next_status = "waiting_human_input" if any(
            status in {"requires_human", "blocked", "conflicting", "retrieval_failed", "insufficient_evidence", "tbd"}
            for status in result.unit_statuses.values()
        ) else "waiting_human_approval"
        self._replace_order(
            order, status=next_status, unit_statuses=result.unit_statuses,
            evidence_matrix_id=f"matrix-{order.work_order_id}", validation_report_id=report.validation_report_id,
        )
        return artifact

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

    def approve_document_artifact(self, ctx: RequestContext, artifact_id: str, *, comment: str = "") -> DocumentArtifact:
        candidate = self._artifact_for_context(ctx, artifact_id)
        if candidate.stage != "review_candidate":
            raise ValueError("only a review candidate may be approved")
        order = self._order(ctx, candidate.work_order_id, "approve_artifact")
        report = self.store.get_validation_report(candidate.validation_report_id)
        snapshot = self.resolve_source_snapshot(order)
        if report is None or report.status == "failed":
            raise ValueError("candidate does not have a releasable validation result")
        candidate_content = self.store.read_artifact_content(candidate.artifact_id)
        self._assert_generated_artifact_clean(candidate_content, order.target_format)
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
            content_hash=candidate.content_hash, approval_subject_hash=subject_hash,
            parent_artifact_id=candidate.artifact_id, validation_report_id=candidate.validation_report_id,
            approval_event_ids=[event.event_id for event in approvals], integrity_manifest_id=candidate.integrity_manifest_id,
            released_at=datetime.now(timezone.utc),
        )
        released = self.store.save_artifact(released, candidate_content, order.target_format)
        self._replace_order(order, status="complete")
        return released

    def download_document_artifact(self, ctx: RequestContext, artifact_id: str) -> bytes:
        artifact = self._artifact_for_context(ctx, artifact_id)
        order = self._order_raw(artifact.work_order_id)
        capability = "download_approved_release" if artifact.stage == "approved_release" else "download_review_candidate"
        self.require_work_order_capability(ctx, order, capability)
        return self.store.read_artifact_content(artifact_id)

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

    def _schema_harness_policy(self, schema: DocumentSchema) -> HarnessPolicy:
        """Persist the bounded policy required by one approved semantic schema."""
        unit_count = (
            sum(field.authoring_policy == "managed_writer" for field in schema.fields)
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
            ),
            version=f"units-{unit_count}-attempts-{attempts}-rewrite",
            status="approved",
            max_units_per_run=max(1, unit_count),
            max_retrieval_attempts_per_unit=attempts,
            max_retrieval_rounds=retrieval_rounds,
            max_steps=max_steps,
            # LLM writer calls can take 30-120s each; give each unit a
            # generous per-lease budget so the lease survives one LLM roundtrip
            # even without between-token heartbeats. Runtime heartbeats between
            # each _step call keep the actual usage well below this ceiling.
            lease_seconds=max(300, unit_count * 120),
            writer_provider_id=LLMManagedWriter.provider_id,
        ))

    @staticmethod
    def _field_for_suggestion(suggestion) -> Any:
        from src.document_authoring.models import DocumentFieldSchema

        return DocumentFieldSchema(
            field_id=suggestion.semantic_unit_id,
            label=suggestion.label,
            retrieval_policy_id=f"retrieval-{suggestion.semantic_unit_id}",
            verification_policy_id=f"verification-{suggestion.semantic_unit_id}",
            query_terms=list(suggestion.retrieval_terms),
            authoring_policy="managed_writer",
        )

    @staticmethod
    def _regions_and_bindings(template: TemplateVersion, analysis) -> tuple[list[Any], list[TemplateUnitBinding]]:
        unit_by_id = {unit.unit_id: unit for unit in analysis.units}
        regions: list[Any] = []
        bindings: list[TemplateUnitBinding] = []
        seen_targets: set[str] = set()
        for suggestion in analysis.suggestions:
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
                region_id = f"region-{hashlib.sha256(unit_id.encode('utf-8')).hexdigest()[:16]}"
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
            bindings.append(TemplateUnitBinding(
                binding_id=f"binding-{hashlib.sha256(suggestion.semantic_unit_id.encode('utf-8')).hexdigest()[:16]}",
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
            return ManagedWriter(LLMManagedWriter())
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

    def require_work_order_capability(
        self,
        ctx: RequestContext,
        order: DocumentWorkOrder,
        capability: str,
    ) -> None:
        if order.scope_type == "knowledge_base":
            if (
                order.tenant_id != (ctx.tenant_id or "default")
                or not order.knowledge_base_name
                or not ctx.has_kb_permission(order.knowledge_base_name, "read")
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
    ) -> RetrievalOutcome:
        """Bind backend evidence to one selected knowledge base and source set."""
        frozen_source_names = list(dict.fromkeys(source_names))
        accepted = []
        for evidence in evidences:
            if evidence.source_name not in frozen_source_names:
                raise PermissionError("retrieval evidence is outside the frozen source set")
            declared_kb_names = {
                str(evidence.metadata.get(key) or "").strip()
                for key in ("knowledge_base_name", "kb_name")
            } - {""}
            if any(name != knowledge_base_name for name in declared_kb_names):
                raise PermissionError("retrieval evidence knowledge base does not match selection")
            bound_evidence = copy(evidence)
            bound_evidence.metadata = {
                **evidence.metadata,
                "knowledge_base_name": knowledge_base_name,
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
        if artifact is None or artifact.tenant_id != (ctx.tenant_id or "default"):
            raise KeyError("artifact not found")
        return artifact

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
        return self.store.replace_work_order(revised)

    @staticmethod
    def _semantic_fills(
        template: TemplateVersion,
        drafts: list[DocumentUnitDraft],
        statuses: dict[str, str],
        bindings: dict[str, TemplateUnitBinding],
    ) -> WorkbookFillPlan | DocxFillPlan:
        fills: list[WorkbookFill] | list[DocxFill] = []
        for draft in drafts:
            if draft.validation_status != "supported" or statuses.get(draft.unit_id) != "ready_to_render":
                continue
            semantic_unit_id = draft.unit_id.split(":", 1)[-1]
            binding = bindings.get(semantic_unit_id)
            if binding is None:
                continue
            if template.format != "docx" and len(binding.target_region_ids) != 1:
                raise ValueError("workbook scalar bindings require exactly one target")
            value = draft.proposed_value if draft.proposed_value is not None else draft.content
            if value is None:
                continue
            for region_id in binding.target_region_ids:
                if template.format == "docx":
                    fills.append(DocxFill(region_id=region_id, value=str(value), semantic_unit_id=semantic_unit_id))
                else:
                    fills.append(WorkbookFill(region_id=region_id, value=str(value), semantic_unit_id=semantic_unit_id))
        return DocumentGenerationService._fill_plan(template, fills)

    @staticmethod
    def _fill_plan(
        template: TemplateVersion,
        fills: list[WorkbookFill] | list[DocxFill],
    ) -> WorkbookFillPlan | DocxFillPlan:
        if template.format == "docx":
            return DocxFillPlan(template_version_id=template.template_version_id, fills=fills)
        return WorkbookFillPlan(template_version_id=template.template_version_id, fills=fills)

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
            for evidence in outcome.evidences:
                if evidence.metadata.get("knowledge_base_name") != order.knowledge_base_name:
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
