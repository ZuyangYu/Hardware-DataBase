from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from src.document_authoring.models import HarnessPolicy
from src.document_authoring.renderers.docx import DocxRenderer, DocxRenderResult
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import TemplateAnalysisSuggestion
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import DeterministicEvidenceWriter
from src.core.app_pipeline import AppPipeline
from src.pipelines.document_rag.schemas import EvidenceEnvelope
from src.projects.retrieval import ProjectEvidenceRetrievalService
from test_document_authoring_p2a import _prepare_project
from test_template_sanitizer import _active_parts, _docx_with_external_link_and_ole_object


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
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return output.getvalue()


def _with_embedded_object_part(content: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr("word/embeddings/injected.bin", b"active")
    return output.getvalue()


class _ActiveOutputDocxRenderer(DocxRenderer):
    def render(self, *args, **kwargs):
        result = super().render(*args, **kwargs)
        return DocxRenderResult(
            content=_with_embedded_object_part(result.content),
            security_report=result.security_report,
            integrity_manifest=result.integrity_manifest,
        )


class _DeterministicTemplateSuggester:
    def suggest(self, analysis):
        suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="project_summary",
            label="Project Summary",
            target_unit_ids=[analysis.units[0].unit_id],
            retrieval_terms=["project summary"],
            confidence=1.0,
        )]
        analysis.suggestions = suggestions
        return suggestions


def _pipeline_with_approved_project_source(tmp_path):
    projects, ctx, _project, baseline, processing = _prepare_project(tmp_path)
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "artifacts"))
    generation = DocumentGenerationService(
        projects,
        store,
        suggestion_provider=_DeterministicTemplateSuggester(),
    )
    generation.register_harness_policy(HarnessPolicy(
        harness_policy_id="deterministic-docx-writer",
        version="1",
        status="approved",
        writer_provider_id=DeterministicEvidenceWriter.provider_id,
    ))
    pipeline = object.__new__(AppPipeline)
    pipeline.projects = projects
    pipeline.document_generation = generation
    project_retrieval = ProjectEvidenceRetrievalService(projects)

    def retrieve(requirement, attempt):
        orders = store.list_work_orders(ctx.tenant_id, baseline.project_id)
        assert len(orders) == 1
        order = orders[0]
        snapshot = projects.store.get_source_set_snapshot(order.source_set_snapshot_id, ctx.tenant_id)
        assert snapshot is not None
        assert requirement.project_id == order.project_id == baseline.project_id
        assert attempt == 1
        assert snapshot.source_version_ids == ["version-a"]
        assert snapshot.processing_artifact_ids == [processing.artifact_id]
        assert snapshot.region_policy_versions == {"policy-a": "1"}

        def retrieve_one(version_id, artifact_ids, region_policies):
            assert version_id == snapshot.source_version_ids[0]
            assert artifact_ids == snapshot.processing_artifact_ids
            assert region_policies == snapshot.region_policy_versions
            return [EvidenceEnvelope(
                id="project-controller-evidence",
                content="The project controller is STM32H743.",
                project_id=baseline.project_id,
                source_version_id=version_id,
                processing_artifact_id=artifact_ids[0],
            )]

        outcome = project_retrieval.retrieve(
            ctx,
            requirement,
            snapshot.source_set_snapshot_id,
            retrieve_one,
        )
        assert outcome.applied_source_set_snapshot_id == order.source_set_snapshot_id
        assert outcome.applied_region_policy_versions == snapshot.region_policy_versions
        assert outcome.source_outcomes[0].source_version_id == snapshot.source_version_ids[0]
        assert outcome.source_outcomes[0].processing_artifact_id == snapshot.processing_artifact_ids[0]
        assert outcome.evidences[0].project_id == order.project_id
        return outcome

    return pipeline, ctx, baseline, retrieve


def test_uploaded_docx_is_analyzed_confirmed_written_from_project_evidence_and_downloaded(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx, filename="review.docx", content=_docx_with_text("Controller"), template_name="Review",
    )
    template = pipeline.confirm_document_template(ctx, analysis_id=analysis.analysis_id, display_name="Review")
    order = pipeline.create_document_work_order(
        ctx,
        project_id=baseline.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version="1",
        harness_policy_id="deterministic-docx-writer",
    )

    candidate = pipeline.run_internal_document_harness(ctx, order.work_order_id, retrieve=retrieve)

    assert candidate.stage == "review_candidate"
    downloaded = pipeline.download_document_artifact(ctx, candidate.artifact_id)
    assert b"STM32H743" in downloaded
    with zipfile.ZipFile(io.BytesIO(downloaded)) as document:
        assert b"STM32H743" in document.read("word/document.xml")


def test_rendered_artifact_from_sanitized_template_contains_no_active_content(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx,
        filename="review.docx",
        content=_docx_with_external_link_and_ole_object(),
        template_name="Review",
    )
    template = pipeline.confirm_document_template(
        ctx,
        analysis_id=analysis.analysis_id,
        display_name="Review",
    )
    order = pipeline.create_document_work_order(
        ctx,
        project_id=baseline.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version=template.template_schema_version,
        harness_policy_id="deterministic-docx-writer",
    )

    candidate = pipeline.run_internal_document_harness(
        ctx,
        order.work_order_id,
        retrieve=retrieve,
    )

    assert _active_parts(pipeline.download_document_artifact(ctx, candidate.artifact_id)) == []


def test_candidate_is_not_saved_when_service_reinspection_finds_active_content(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx,
        filename="review.docx",
        content=_docx_with_text("Controller"),
        template_name="Review",
    )
    template = pipeline.confirm_document_template(
        ctx,
        analysis_id=analysis.analysis_id,
        display_name="Review",
    )
    order = pipeline.create_document_work_order(
        ctx,
        project_id=baseline.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version=template.template_schema_version,
        harness_policy_id="deterministic-docx-writer",
    )
    pipeline.document_generation.docx_renderer = _ActiveOutputDocxRenderer()

    with pytest.raises(ValueError, match="generated artifact contains active content"):
        pipeline.run_internal_document_harness(ctx, order.work_order_id, retrieve=retrieve)

    assert pipeline.document_generation.store.list_artifacts(order.work_order_id) == []


def test_release_is_not_saved_when_candidate_bytes_contain_active_content(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx,
        filename="review.docx",
        content=_docx_with_text("Controller"),
        template_name="Review",
    )
    template = pipeline.confirm_document_template(
        ctx,
        analysis_id=analysis.analysis_id,
        display_name="Review",
    )
    order = pipeline.create_document_work_order(
        ctx,
        project_id=baseline.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version=template.template_schema_version,
        harness_policy_id="deterministic-docx-writer",
    )
    candidate = pipeline.run_internal_document_harness(ctx, order.work_order_id, retrieve=retrieve)
    assert candidate.storage_ref
    Path(candidate.storage_ref).write_bytes(
        _with_embedded_object_part(
            pipeline.document_generation.store.read_artifact_content(candidate.artifact_id),
        ),
    )

    with pytest.raises(ValueError, match="generated artifact contains active content"):
        pipeline.approve_document_artifact(ctx, candidate.artifact_id)

    assert [artifact.stage for artifact in pipeline.document_generation.store.list_artifacts(order.work_order_id)] == [
        "review_candidate",
    ]


def test_release_is_not_saved_when_clean_candidate_bytes_do_not_match_hash(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx,
        filename="review.docx",
        content=_docx_with_text("Controller"),
        template_name="Review",
    )
    template = pipeline.confirm_document_template(
        ctx,
        analysis_id=analysis.analysis_id,
        display_name="Review",
    )
    order = pipeline.create_document_work_order(
        ctx,
        project_id=baseline.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version=template.template_schema_version,
        harness_policy_id="deterministic-docx-writer",
    )
    candidate = pipeline.run_internal_document_harness(ctx, order.work_order_id, retrieve=retrieve)
    assert candidate.storage_ref
    Path(candidate.storage_ref).write_bytes(_docx_with_text("Clean but tampered"))

    with pytest.raises(ValueError, match="candidate content hash changed"):
        pipeline.approve_document_artifact(ctx, candidate.artifact_id)

    assert [artifact.stage for artifact in pipeline.document_generation.store.list_artifacts(order.work_order_id)] == [
        "review_candidate",
    ]


def test_docx_confirmation_rejects_a_persisted_template_change(tmp_path):
    pipeline, ctx, _baseline, _retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx, filename="review.docx", content=_docx_with_text("Controller"), template_name="Review",
    )
    template = pipeline.document_generation.store.get_template(analysis.template_version_id)
    assert template is not None and template.storage_ref
    Path(template.storage_ref).write_bytes(_docx_with_text("Changed controller"))

    with pytest.raises(ValueError, match="content hash"):
        pipeline.confirm_document_template(ctx, analysis_id=analysis.analysis_id, display_name="Review")


def test_internal_harness_rejects_docx_changed_after_hash_bound_confirmation(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx, filename="review.docx", content=_docx_with_text("Controller"), template_name="Review",
    )
    template = pipeline.confirm_document_template(ctx, analysis_id=analysis.analysis_id, display_name="Review")
    order = pipeline.create_document_work_order(
        ctx,
        project_id=baseline.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version="1",
        harness_policy_id="deterministic-docx-writer",
    )
    assert template.storage_ref
    Path(template.storage_ref).write_bytes(_docx_with_text("Changed controller"))

    with pytest.raises(ValueError, match="content hash"):
        pipeline.run_internal_document_harness(ctx, order.work_order_id, retrieve=retrieve)
