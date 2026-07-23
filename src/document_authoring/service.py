"""P2a document-generation service.

This service owns the use-case transaction boundaries.  UI, a future REST API,
the background worker and external adapters call it instead of manipulating
templates, project stores or artifacts directly.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.deterministic_rules import DeterministicRuleExecutor
from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
from src.document_authoring.models import (
    DeterministicRuleSpec,
    DocumentArtifact,
    DocumentHumanEvent,
    DocumentUnitDraft,
    HarnessPolicy,
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
from src.document_authoring.template_analysis import DocxRegionSchema, TemplateAnalysis
from src.document_authoring.template_analyzers import analyze_template
from src.document_authoring.template_sanitizer import sanitize_template
from src.document_authoring.template_suggester import LLMTemplateSuggestionProvider, TemplateSuggestionProvider
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.worker import DocumentGenerationWorker
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, LLMManagedWriter, ManagedWriter
from src.core.llm_client import LLMClient
from src.pipelines.document_rag.schemas import RequestContext
from src.projects.service import ProjectService


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
    ) -> TemplateAnalysis:
        """Persist an immutable draft then analyze its structure, never its bytes by LLM."""
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
        analysis = analyze_template(sanitized.content, sanitized.format).model_copy(update={
            "analysis_id": f"analysis-{uuid.uuid4().hex}",
            "template_version_id": template.template_version_id,
        })
        self.template_suggester.suggest(analysis)
        analysis.validate_suggestions()
        return self.store.save_template_analysis(analysis)

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
        template = self._template(template_version_id)
        schema = self._schema(document_schema_id, document_schema_version)
        if template.status != "approved" or schema.status != "approved":
            raise ValueError("document generation requires approved template and document schema")
        if template.template_schema_id != template.template_schema_id:  # pragma: no cover - defensive invariant
            raise ValueError("invalid template schema")
        if schema.execution_mode not in {"deterministic_only", "internal_harness"}:
            raise ValueError("external-agent work orders are not enabled before P3")
        harness_policy = None
        if schema.execution_mode == "internal_harness":
            if not harness_policy_id:
                existing = self.store.list_harness_policies(approved_only=True)
                harness_policy = existing[0] if existing else self._create_default_harness_policy()
                harness_policy_id = harness_policy.harness_policy_id
            else:
                harness_policy = self.store.get_harness_policy(harness_policy_id)
            if harness_policy is None or harness_policy.status != "approved":
                raise ValueError("internal-harness work orders require an approved HarnessPolicy")
        baseline = self.projects.store.get_baseline(baseline_id, tenant_id)
        if baseline is None:
            raise ValueError("baseline not found")
        work_order_id = f"wo-{uuid.uuid4().hex}"
        snapshot = self.projects.create_source_set_snapshot(
            ctx, work_order_id=work_order_id, project_id=project_id, baseline_id=baseline_id,
            processing_artifact_ids=processing_artifact_ids,
        )
        order = DocumentWorkOrder(
            work_order_id=work_order_id, tenant_id=tenant_id, project_id=project_id,
            baseline_id=baseline_id, baseline_content_hash=baseline.content_hash,
            source_set_snapshot_id=snapshot.source_set_snapshot_id,
            template_version_id=template.template_version_id,
            document_schema_id=schema.document_schema_id, document_schema_version=schema.version,
            template_schema_id=template.template_schema_id, template_schema_version=template.template_schema_version,
            retrieval_policy_version="1", renderer_policy_version=self._policy(template).version,
            target_format=template.format, execution_mode=schema.execution_mode,
            harness_policy_id=harness_policy_id,
            harness_policy_version=harness_policy.version if harness_policy else None,
            unit_statuses={
                **{item.field_id: "planned" for item in schema.fields},
                **{item.review_item_id: "planned" for item in schema.review_items},
            },
            created_by=ctx.user_id, idempotency_key=idempotency_key,
        )
        return self.store.create_work_order(order)

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
        snapshot = self.projects.store.get_source_set_snapshot(order.source_set_snapshot_id, order.tenant_id)
        if snapshot is None:
            raise ValueError("work order source set snapshot is missing")
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
        retrieve: Callable[[Any, int], RetrievalOutcome],
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
        schema = self._schema(order.document_schema_id, order.document_schema_version)
        snapshot = self.projects.store.get_source_set_snapshot(order.source_set_snapshot_id, order.tenant_id)
        if snapshot is None:
            raise ValueError("work order source set snapshot is missing")
        template = self._template(order.template_version_id)
        run, manifest = self.harness_runtime.create_run(order, policy, snapshot, template, schema)
        order = self._replace_order(order, status="retrieving", run_manifest_id=manifest.run_manifest_id)
        try:
            result = self.harness_runtime.execute(
                work_order=order, run=run, manifest=manifest, policy=policy, schema=schema, snapshot=snapshot,
                legacy_claims=self.store.list_legacy_template_claims(template.template_version_id),
                writer=writer, retrieve=retrieve,
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
        retrieve: Callable[[Any, int], RetrievalOutcome],
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
        schema = self._schema(order.document_schema_id, order.document_schema_version)
        snapshot = self.projects.store.get_source_set_snapshot(order.source_set_snapshot_id, order.tenant_id)
        if snapshot is None:
            raise ValueError("frozen work order source set snapshot is unavailable")
        template = self._template(order.template_version_id)
        order = self._replace_order(order, status="retrieving", run_manifest_id=manifest.run_manifest_id)
        try:
            result = self.harness_runtime.execute(
                work_order=order, run=run, manifest=manifest, policy=policy, schema=schema, snapshot=snapshot,
                legacy_claims=self.store.list_legacy_template_claims(template.template_version_id),
                writer=writer, retrieve=retrieve,
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

    def get_background_run_status(self, run_id: str) -> dict[str, str] | None:
        run = self.worker.get(run_id)
        if run is None:
            return None
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
        self.projects.access.require(ctx, self._order_raw(artifact.work_order_id).project_id, required)
        actor_role = self._actor_role(ctx, self._order_raw(artifact.work_order_id).project_id)
        report = self.store.get_validation_report(artifact.validation_report_id)
        if report is None:
            raise ValueError("artifact validation report is missing")
        snapshot = self.projects.store.get_source_set_snapshot(
            self._order_raw(artifact.work_order_id).source_set_snapshot_id, ctx.tenant_id or "default"
        )
        if snapshot is None:
            raise ValueError("artifact source set snapshot is missing")
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
        self.projects.access.require(ctx, order.project_id, "approve_artifact")
        report = self.store.get_validation_report(candidate.validation_report_id)
        snapshot = self.projects.store.get_source_set_snapshot(order.source_set_snapshot_id, ctx.tenant_id or "default")
        if report is None or snapshot is None or report.status == "failed":
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
        self.projects.access.require(ctx, order.project_id, capability)
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
        self.projects.access.require(ctx, order.project_id, capability)
        return order

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
        self.projects.access.require(ctx, order.project_id, "run_deterministic_work_order")
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
