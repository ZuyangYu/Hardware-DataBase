from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import TemplateAnalysisSuggestion
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import LLMManagedWriter
from src.document_authoring.writers.provider import WriterRequest
from src.core.app_pipeline import AppPipeline
from src.pipelines.document_rag.schemas import RequestContext
from test_document_authoring_p2a import _prepare_project


def _docx_with_text(text: str) -> bytes:
    parts = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
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
        suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="project_summary",
            label="Project Summary",
            target_unit_ids=[analysis.units[0].unit_id],
            retrieval_terms=["project summary"],
            confidence=0.95,
        )]
        analysis.suggestions = suggestions
        return suggestions


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
    assert order.harness_policy_id
    assert order.harness_policy_version == "1"


def test_managed_llm_writer_rejects_model_supplied_validation_status():
    class _RecordingClient:
        def chat(self, messages, **kwargs):
            assert kwargs["usage_stage"] == "document_authoring"
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

    with pytest.raises(ValueError, match="unsupported draft"):
        LLMManagedWriter(_RecordingClient()).generate(request)
