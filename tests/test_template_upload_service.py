from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import requests

from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocxFillPlan,
    RendererPolicy,
    TemplateUnitBinding,
    WorkbookFillPlan,
)
from src.document_authoring.renderers.docx import DocxRenderer
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_sanitizer import sanitize_template
from src.document_authoring.template_analysis import TemplateAnalysisSuggestion, TemplateAnalysisUnit
from src.document_authoring.template_progress import TemplateProgress
from src.document_authoring.template_suggester import (
    LLMTemplateSuggestionProvider,
    TemplateSuggestionTechnicalFailure,
)
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import LLMManagedWriter
from src.document_authoring.writers.provider import WriterRequest
from src.core.app_pipeline import AppPipeline
from src.pipelines.document_rag.schemas import RequestContext
from tests.test_document_authoring_p2a import _prepare_project
from tests.test_template_sanitizer import _workbook_with_vba_external_link_and_embedded_object


def _docx_with_text(text: str) -> bytes:
    parts = {
        "[Content_Types].xml": (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Override PartName="/word/document.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            b"</Types>"
        ),
        "_rels/.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rRoot" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            b'Target="word/document.xml"/>'
            b"</Relationships>"
        ),
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body></w:document>"
        ).encode(),
        "word/_rels/document.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return output.getvalue()


class _SuggestedSemanticUnit:
    def suggest(self, analysis):
        if not analysis.units:
            analysis.suggestions = []
            return []
        suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="project_summary",
            label="Project Summary",
            target_unit_ids=[analysis.units[0].unit_id],
            retrieval_terms=["project summary"],
            confidence=0.95,
        )]
        analysis.suggestions = suggestions
        return suggestions


class _EmptySuggester:
    def suggest(self, analysis):
        analysis.suggestions = []
        return []


class _RequiresHumanSuggester:
    def suggest(self, analysis):
        analysis.status = "requires_human"
        analysis.suggestions = []
        return []


class _InvalidSuggestionSuggester:
    def suggest(self, analysis):
        analysis.suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="invalid",
            label="Invalid",
            target_unit_ids=["missing-unit"],
            retrieval_terms=[],
            confidence=1.0,
        )]
        return analysis.suggestions


class _AlwaysMalformedSuggester:
    def suggest(self, analysis):
        raise TemplateSuggestionTechnicalFailure("LLM response contained invalid JSON")


class _TransportFailingSuggester:
    def suggest(self, analysis):
        raise requests.ReadTimeout("LLM response timed out")


class _NonWritableTargetClient:
    def invoke(self, _messages, **_kwargs):
        return json.dumps([{
            "semantic_unit_id": "protected",
            "label": "Protected",
            "target_unit_ids": ["protected-unit"],
            "retrieval_terms": [],
            "confidence": 0.9,
        }])


class _DuplicateTargetClient:
    def invoke(self, messages, **_kwargs):
        unit_id = json.loads(messages[1]["content"])["units"][0]["unit_id"]
        return json.dumps([
            {
                "semantic_unit_id": "low-confidence",
                "label": "Low confidence",
                "target_unit_ids": [unit_id],
                "retrieval_terms": [],
                "confidence": 0.5,
            },
            {
                "semantic_unit_id": "high-confidence",
                "label": "High confidence",
                "target_unit_ids": [unit_id],
                "retrieval_terms": [],
                "confidence": 0.9,
            },
        ])


class _NonWritableTargetSuggester:
    def __init__(self):
        self._provider = LLMTemplateSuggestionProvider(_NonWritableTargetClient())

    def suggest(self, analysis):
        analysis.units.append(TemplateAnalysisUnit(
            unit_id="protected-unit",
            locator={"paragraph_index": 1},
            label="Protected unit",
            writable=False,
        ))
        return self._provider.suggest(analysis)


@pytest.fixture
def author_ctx():
    return RequestContext(user_id="alice", tenant_id="tenant-a", roles=["user"])


@pytest.fixture
def authoring_service(tmp_path: Path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    service = DocumentGenerationService(store=store)
    service.template_suggester = _SuggestedSemanticUnit()
    return service


@pytest.fixture
def pipeline(tmp_path: Path):
    project_service, _ctx, _project, baseline, _processing = _prepare_project(tmp_path)
    store = DocumentAuthoringStore(str(tmp_path / "pipeline-authoring.db"), str(tmp_path / "pipeline-authoring-files"))
    app = object.__new__(AppPipeline)
    app.projects = project_service
    app.document_generation = DocumentGenerationService(
        project_service, store, suggestion_provider=_SuggestedSemanticUnit(),
    )
    app.approved_project_baseline = baseline
    return app


@pytest.fixture
def approved_project_baseline(pipeline):
    return pipeline.approved_project_baseline


def _replace_template_bytes(store: DocumentAuthoringStore, template_version_id: str, content: bytes) -> None:
    template = store.get_template(template_version_id)
    assert template is not None and template.storage_ref
    Path(template.storage_ref).write_bytes(content)


def test_confirmed_docx_analysis_creates_hash_bound_approved_template_and_schema(authoring_service, author_ctx):
    analysis = authoring_service.analyze_uploaded_template(
        author_ctx, filename="review.docx", content=_docx_with_text("Project Summary"), template_name="Review",
    )

    template = authoring_service.confirm_template_analysis(
        author_ctx, analysis_id=analysis.analysis_id, display_name="Review",
    )

    assert template.format == "docx"
    assert template.status == "approved"
    assert authoring_service.store.get_template_analysis(template.template_version_id).content_hash == template.content_hash
    schema = authoring_service.store.get_document_schema(template.template_schema_id, "1")
    assert schema is not None and schema.status == "approved"


def test_auto_activation_approves_template_with_valid_suggestions(authoring_service, author_ctx):
    template = authoring_service.analyze_and_activate_uploaded_template(
        author_ctx, filename="review.docx", content=_docx_with_text("Project Summary"), template_name="Review",
    )

    assert template.status == "approved"
    assert authoring_service.store.get_document_schema(template.template_schema_id, "1").execution_mode == "internal_harness"


def test_auto_activation_resolves_duplicate_llm_targets_before_creating_bindings(
    authoring_service,
    author_ctx,
):
    authoring_service.template_suggester = LLMTemplateSuggestionProvider(
        _DuplicateTargetClient(),
    )

    template = authoring_service.analyze_and_activate_uploaded_template(
        author_ctx,
        filename="review.docx",
        content=_docx_with_text("Project Summary"),
        template_name="Review",
    )

    analysis = authoring_service.store.get_template_analysis(template.template_version_id)
    bindings = authoring_service.store.list_unit_bindings(
        template.template_schema_id,
        template.template_schema_version,
    )
    assert template.status == "approved"
    assert analysis is not None
    assert [item.semantic_unit_id for item in analysis.suggestions] == ["high-confidence"]
    assert [item.semantic_unit_id for item in bindings] == ["high-confidence"]
    assert len(bindings[0].target_region_ids) == 1


def test_auto_activation_approves_template_with_empty_suggestions(authoring_service, author_ctx):
    authoring_service.template_suggester = _EmptySuggester()

    template = authoring_service.analyze_and_activate_uploaded_template(
        author_ctx, filename="review.docx", content=_docx_with_text("Project Summary"), template_name="Review",
    )

    assert template.status == "approved"
    assert authoring_service.store.get_document_schema(template.template_schema_id, "1").execution_mode == "deterministic_only"


def test_schema_harness_policy_scales_budgets_for_122_semantic_units(authoring_service):
    schema = DocumentSchema(
        document_schema_id="large-schema",
        version="1",
        document_type="Checklist",
        status="approved",
        execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id=f"field-{index}",
                label=f"Field {index}",
                retrieval_policy_id=f"retrieval-{index}",
                verification_policy_id=f"verify-{index}",
                authoring_policy="managed_writer",
            )
            for index in range(122)
        ],
    )

    policy = authoring_service._schema_harness_policy(schema)

    assert policy.max_units_per_run == 122
    assert policy.max_retrieval_attempts_per_unit == 2
    assert policy.max_retrieval_rounds == 244
    assert policy.max_steps == 734
    assert policy.status == "approved"


def test_schema_harness_policy_rejects_schema_over_automatic_generation_cap(authoring_service):
    schema = DocumentSchema(
        document_schema_id="oversized-schema",
        version="1",
        document_type="Checklist",
        status="approved",
        execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id=f"field-{index}",
                label=f"Field {index}",
                retrieval_policy_id=f"retrieval-{index}",
                verification_policy_id=f"verify-{index}",
                authoring_policy="managed_writer",
            )
            for index in range(501)
        ],
    )

    with pytest.raises(
        ValueError,
        match="schema semantic unit count exceeds automatic-generation capacity",
    ):
        authoring_service._schema_harness_policy(schema)


def test_auto_activation_reports_local_and_activation_progress(authoring_service, author_ctx):
    events: list[TemplateProgress] = []

    authoring_service.analyze_and_activate_uploaded_template(
        author_ctx,
        filename="review.docx",
        content=_docx_with_text("Project Summary"),
        template_name="Review",
        progress_callback=events.append,
    )

    assert [event.stage for event in events] == [
        "upload_started",
        "sanitization_completed",
        "structure_analysis_completed",
        "analysis_persisted",
        "activation_started",
        "activation_completed",
    ]
    assert events[2].unit_count == 1
    assert events[2].writable_unit_count == 1
    assert all(event.template_version_id for event in events[1:])


def test_auto_activation_keeps_non_ready_template_as_draft_for_audit(authoring_service, author_ctx):
    authoring_service.template_suggester = _RequiresHumanSuggester()

    with pytest.raises(ValueError, match="automatic template activation failed"):
        authoring_service.analyze_and_activate_uploaded_template(
            author_ctx, filename="review.docx", content=_docx_with_text("Project Summary"), template_name="Review",
        )

    template = authoring_service.store.list_templates()[0]
    analysis = authoring_service.store.get_template_analysis(template.template_version_id)
    assert template.status == "draft"
    assert analysis is not None and analysis.status == "requires_human"


def test_auto_activation_persists_invalid_suggestion_analysis_as_audit_draft(authoring_service, author_ctx):
    authoring_service.template_suggester = _InvalidSuggestionSuggester()

    with pytest.raises(ValueError, match="automatic template activation failed"):
        authoring_service.analyze_and_activate_uploaded_template(
            author_ctx, filename="review.docx", content=_docx_with_text("Project Summary"), template_name="Review",
        )

    template = authoring_service.store.list_templates()[0]
    analysis = authoring_service.store.get_template_analysis(template.template_version_id)
    assert template.status == "draft"
    assert analysis is not None and analysis.status == "requires_human"
    assert analysis.suggestions == []


@pytest.mark.parametrize("suggester", [_AlwaysMalformedSuggester(), _TransportFailingSuggester()])
def test_auto_upload_preserves_failed_audit_and_aborts_on_technical_suggestion_failure(
    authoring_service,
    author_ctx,
    suggester,
):
    authoring_service.template_suggester = suggester
    events: list[TemplateProgress] = []

    with pytest.raises(TemplateSuggestionTechnicalFailure, match="automatic template upload failed"):
        authoring_service.analyze_and_activate_uploaded_template(
            author_ctx,
            filename="review.docx",
            content=_docx_with_text("Project Summary"),
            template_name="Review",
            progress_callback=events.append,
        )

    template = authoring_service.store.list_templates()[0]
    analysis = authoring_service.store.get_template_analysis(template.template_version_id)
    assert template.status == "draft"
    assert analysis is not None and analysis.status == "failed"
    assert analysis.suggestions == []
    assert [event.stage for event in events][-2:] == ["analysis_failed", "activation_failed"]


def test_auto_upload_aborts_and_preserves_failed_audit_for_non_writable_llm_target(
    authoring_service,
    author_ctx,
):
    authoring_service.template_suggester = _NonWritableTargetSuggester()

    with pytest.raises(TemplateSuggestionTechnicalFailure, match="automatic template upload failed"):
        authoring_service.analyze_and_activate_uploaded_template(
            author_ctx,
            filename="review.docx",
            content=_docx_with_text("Project Summary"),
            template_name="Review",
        )

    template = authoring_service.store.list_templates()[0]
    analysis = authoring_service.store.get_template_analysis(template.template_version_id)
    assert template.status == "draft"
    assert analysis is not None and analysis.status == "failed"
    assert analysis.suggestions == []
    assert authoring_service.store.get_document_schema(template.template_schema_id, "1") is None


def test_pipeline_auto_activation_delegates_to_document_generation_service(pipeline, author_ctx):
    template = pipeline.analyze_and_activate_document_template(
        author_ctx,
        filename="review.docx",
        content=_docx_with_text("Project Summary"),
        template_name="Review",
    )

    assert template.status == "approved"


def test_confirmation_rejects_when_template_hash_changes(authoring_service, author_ctx):
    analysis = authoring_service.analyze_uploaded_template(
        author_ctx, filename="review.docx", content=_docx_with_text("A"), template_name="Review",
    )
    _replace_template_bytes(authoring_service.store, analysis.template_version_id, _docx_with_text("B"))

    with pytest.raises(ValueError, match="content hash"):
        authoring_service.confirm_template_analysis(
            author_ctx, analysis_id=analysis.analysis_id, display_name="Review",
        )


def test_confirmation_rechecks_persisted_bytes_inside_activation(author_ctx, tmp_path: Path):
    class _ActivationMutationStore(DocumentAuthoringStore):
        replacement: bytes | None = None

        def activate_template_analysis(self, **kwargs):
            if self.replacement is not None:
                template = kwargs["template"]
                assert template.storage_ref
                Path(template.storage_ref).write_bytes(self.replacement)
                self.replacement = None
            return super().activate_template_analysis(**kwargs)

    store = _ActivationMutationStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    service = DocumentGenerationService(store=store, suggestion_provider=_SuggestedSemanticUnit())
    analysis = service.analyze_uploaded_template(
        author_ctx, filename="review.docx", content=_docx_with_text("A"), template_name="Review",
    )
    store.replacement = _docx_with_text("B")

    with pytest.raises(ValueError, match="content hash"):
        service.confirm_template_analysis(
            author_ctx, analysis_id=analysis.analysis_id, display_name="Review",
        )

    template = store.get_template(analysis.template_version_id)
    assert template is not None and template.status == "draft"
    assert store.get_document_schema(template.template_schema_id, "1") is None


def test_identical_uploads_receive_distinct_confirmation_ids(authoring_service, author_ctx):
    content = _docx_with_text("Project Summary")

    first = authoring_service.analyze_uploaded_template(
        author_ctx, filename="first.docx", content=content, template_name="First",
    )
    second = authoring_service.analyze_uploaded_template(
        author_ctx, filename="second.docx", content=content, template_name="Second",
    )

    assert first.content_hash == second.content_hash
    assert first.analysis_id != second.analysis_id


def test_active_xlsm_upload_is_analyzed_as_a_safe_xlsx_template(authoring_service, author_ctx):
    analysis = authoring_service.analyze_uploaded_template(
        author_ctx,
        filename="review.xlsm",
        content=_workbook_with_vba_external_link_and_embedded_object(),
        template_name="review",
    )

    template = authoring_service.store.get_template(analysis.template_version_id)
    assert template is not None
    report = authoring_service.store.get_template_sanitization_report(template.template_version_id)

    assert analysis.status == "ready_for_confirmation"
    assert analysis.format == template.format == "xlsx"
    assert report is not None and report.source_format == "xlsm"
    content = authoring_service.store.read_template_content(template.template_version_id)
    assert authoring_service.workbook_renderer.inspect(content, template.format).active_content_status == "clean"
    policy = authoring_service.store.get_renderer_policy(template.renderer_policy_id)
    assert policy is not None
    assert policy.macro_policy == policy.external_link_policy == policy.embedded_object_policy == "strip"
    assert policy.allowlisted_template_hashes == []


def test_pipeline_sanitization_summary_exposes_only_counts_and_safe_format(pipeline, author_ctx):
    analysis = pipeline.analyze_document_template(
        author_ctx,
        filename="review.xlsm",
        content=_workbook_with_vba_external_link_and_embedded_object(),
        template_name="review",
    )

    summary = pipeline.get_document_template_sanitization_summary(author_ctx, analysis.template_version_id)

    assert summary["安全模板格式"] == "xlsx"
    assert summary["已移除宏"] == 1
    assert summary["已移除外链"] >= 1
    assert {"removed_parts", "removed_relationships", "source_storage_ref"}.isdisjoint(summary)
    assert "vbaProject.bin" not in str(summary)


@pytest.mark.parametrize(
    ("renderer", "content", "fill_plan"),
    [
        (DocxRenderer(), _docx_with_text("Safe"), DocxFillPlan(template_version_id="template-1")),
        (
            XlsmRenderer(),
            sanitize_template(_workbook_with_vba_external_link_and_embedded_object(), "xlsm").content,
            WorkbookFillPlan(template_version_id="template-1"),
        ),
    ],
    ids=["docx", "xlsx"],
)
def test_renderer_rejects_output_when_final_inspection_finds_active_content(
    monkeypatch,
    renderer,
    content,
    fill_plan,
):
    real_inspect = renderer.inspect
    inspections = 0

    def inspect_with_active_output(rendered_content, *args):
        nonlocal inspections
        inspections += 1
        report = real_inspect(rendered_content, *args)
        if inspections == 2:
            return report.model_copy(update={
                "embedded_parts": ["injected-active-part"],
                "active_content_status": "requires_approval",
            })
        return report

    monkeypatch.setattr(renderer, "inspect", inspect_with_active_output)
    policy = RendererPolicy(
        renderer_policy_id="strip-active-content",
        macro_policy="strip",
        external_link_policy="strip",
        embedded_object_policy="strip",
        allowlisted_template_hashes=[],
    )

    with pytest.raises(ValueError, match="generated artifact contains active content"):
        renderer.render(content, [], fill_plan, policy)


def _upload_and_confirm_semantic_template(pipeline, author_ctx):
    analysis = pipeline.analyze_document_template(
        author_ctx,
        filename="semantic-review.docx",
        content=_docx_with_text("Project Summary"),
        template_name="Semantic Review",
    )
    return pipeline.confirm_document_template(
        author_ctx, analysis_id=analysis.analysis_id, display_name="Semantic Review",
    )


def test_semantic_template_work_order_uses_frozen_internal_harness_policy(
    pipeline, author_ctx, approved_project_baseline,
):
    template = _upload_and_confirm_semantic_template(pipeline, author_ctx)

    order = pipeline.create_document_work_order(
        author_ctx,
        project_id=approved_project_baseline.project_id,
        baseline_id=approved_project_baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version="1",
    )

    assert order.execution_mode == "internal_harness"
    assert order.harness_policy_id == (
        f"schema-{template.template_schema_id}-1-managed-writer"
    )
    assert order.harness_policy_version == "units-1-attempts-2-rewrite"


def test_large_semantic_template_work_order_freezes_schema_sized_harness_policy(
    pipeline,
    author_ctx,
    approved_project_baseline,
):
    template = _upload_and_confirm_semantic_template(pipeline, author_ctx)
    service = pipeline.document_generation
    region = service.store.list_docx_regions(
        template.template_schema_id,
        template.template_schema_version,
    )[0]
    large_template = template.model_copy(update={
        "template_version_id": "large-semantic-template",
        "template_id": "Large Checklist",
        "template_schema_id": "large-schema",
        "template_schema_version": "1",
        "status": "draft",
        "approved_by": None,
        "approved_at": None,
    })
    service.register_template(
        large_template,
        service.store.read_template_content(template.template_version_id),
        regions=[region],
        bindings=[TemplateUnitBinding(
            binding_id="large-field-0-binding",
            template_schema_id="large-schema",
            template_schema_version="1",
            semantic_unit_type="field",
            semantic_unit_id="field-0",
            target_region_ids=[region.region_id],
        )],
    )
    service.approve_template(large_template.template_version_id, author_ctx.user_id)
    large_schema = DocumentSchema(
        document_schema_id="large-schema",
        version="1",
        document_type="Checklist",
        status="approved",
        execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id=f"field-{index}",
                label=f"Field {index}",
                retrieval_policy_id=f"retrieval-{index}",
                verification_policy_id=f"verify-{index}",
                authoring_policy="managed_writer",
            )
            for index in range(122)
        ],
    )
    service.register_document_schema(large_schema)

    order = pipeline.create_document_work_order(
        author_ctx,
        project_id=approved_project_baseline.project_id,
        baseline_id=approved_project_baseline.baseline_id,
        template_version_id=large_template.template_version_id,
        document_schema_id="large-schema",
        document_schema_version="1",
    )

    policy = pipeline.document_generation.store.get_harness_policy(
        order.harness_policy_id,
        order.harness_policy_version,
    )
    assert policy is not None
    assert order.harness_policy_version == "units-122-attempts-2-rewrite"
    assert policy.max_units_per_run == 122
    assert policy.max_retrieval_rounds == 244
    assert policy.max_steps == 734


def test_managed_llm_writer_rejects_model_supplied_validation_status():
    """LLM-supplied validation_status='supported' is rejected on every attempt.

    Persistent rejections fall back to the deterministic evidence writer, so
    the run completes with a safe evidence-copied draft instead of failing
    the whole document generation. The important guarantee is that the
    model's fabricated validation_status never reaches the caller.
    """
    call_count = 0

    class _RecordingClient:
        def invoke(self, messages, **kwargs):
            nonlocal call_count
            call_count += 1
            return json.dumps({
                "unit_id": "field:summary",
                "run_id": "run-1",
                "generated_by": "managed_writer",
                "content": "Project Summary confirms STM32H743.",
                "proposed_value": "STM32H743",
                "assertions": [{
                    "assertion_id": "assertion-1",
                    "text": "Project Summary confirms STM32H743.",
                    "claim_id": "claim-summary",
                    "evidence_ids": ["evidence-1"],
                }],
                "evidence_ids": ["evidence-1"],
                "proposed_status": "draft",
                "validation_status": "supported",
                "validation_notes": [],
            })

    request = WriterRequest(
        work_order_id="work-1", run_id="run-1", unit_id="field:summary",
        unit_label="Summary", prompt_version="1",
        evidence=[{"id": "evidence-1", "content": "Project Summary confirms STM32H743."}],
    )

    draft = LLMManagedWriter(_RecordingClient()).generate(request)

    # LLM was retried, then the deterministic fallback produced a valid draft
    # with the model-supplied validation_status stripped away.
    assert call_count == 2, "LLM writer must retry once before falling back"
    assert draft.validation_status == "pending", (
        "Deterministic fallback must never surface an LLM-supplied validation_status"
    )
    assert draft.generated_by == "managed_writer"
    assert draft.evidence_ids == ["evidence-1"]
    # The deterministic writer copies the evidence content verbatim.
    assert draft.content == "Project Summary confirms STM32H743."
