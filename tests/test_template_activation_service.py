from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest

import src.settings
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import (
    TemplateAnalysisSuggestion,
    TemplateMappingCorrection,
)
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext


def _xlsx(*values: str) -> bytes:
    cells = "".join(
        f'<c r="{chr(ord("A") + index)}1" t="inlineStr"><is><t>{value}</t></is></c>'
        for index, value in enumerate(values)
    )
    parts = {
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''',
        "_rels/.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        "xl/workbook.xml": b'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData><row r="1">{cells}</row></sheetData></worksheet>'
        ).encode(),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


class _FirstPlaceholderSuggester:
    def suggest(self, analysis):
        target = next(
            unit for unit in analysis.units
            if unit.structural_role_hint == "placeholder"
        )
        analysis.suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="project_summary",
            label="Project Summary",
            target_unit_ids=[target.unit_id],
            retrieval_terms=["project summary"],
            confidence=0.96,
        )]
        return analysis.suggestions


class _FanoutSuggester:
    def suggest(self, analysis):
        targets = [
            unit.unit_id for unit in analysis.units
            if unit.structural_role_hint == "placeholder"
        ]
        analysis.suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="project_summary",
            label="Project Summary",
            target_unit_ids=targets,
            retrieval_terms=["project summary"],
            confidence=0.99,
        )]
        return analysis.suggestions


class _AiRecommendedHeaderSuggester:
    def suggest(self, analysis):
        target = next(
            unit for unit in analysis.units
            if unit.structural_role_hint == "table_header"
        )
        analysis.suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="project_name",
            label="Project Name",
            target_unit_ids=[target.unit_id],
            retrieval_terms=["project name"],
            confidence=0.60,
            overwrite_basis="sample_value",
        )]
        return analysis.suggestions


@pytest.fixture
def ctx() -> RequestContext:
    return RequestContext(user_id="alice", tenant_id="tenant-a", roles=["user"])


@pytest.fixture
def store(tmp_path: Path) -> DocumentAuthoringStore:
    return DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "authoring-files"),
    )


def test_explicit_scalar_placeholder_automatically_activates(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FirstPlaceholderSuggester(),
    )

    template = service.analyze_and_activate_uploaded_template(
        ctx,
        filename="normal.xlsx",
        content=_xlsx("{{project_summary}}"),
        template_name="Normal",
    )

    analysis = store.get_template_analysis(template.template_version_id)
    assert template.status == "approved"
    assert analysis is not None
    assert analysis.activation_decision is not None
    assert analysis.activation_decision.status == "auto_accepted"


def test_identical_workbooks_activate_with_template_scoped_regions_and_bindings(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FirstPlaceholderSuggester(),
    )
    content = _xlsx("{{project_summary}}")

    first = service.analyze_and_activate_uploaded_template(
        ctx,
        filename="first.xlsx",
        content=content,
        template_name="First",
    )
    second = service.analyze_and_activate_uploaded_template(
        ctx,
        filename="second.xlsx",
        content=content,
        template_name="Second",
    )

    first_regions = store.list_workbook_regions(
        first.template_schema_id,
        first.template_schema_version,
    )
    second_regions = store.list_workbook_regions(
        second.template_schema_id,
        second.template_schema_version,
    )
    first_bindings = store.list_unit_bindings(
        first.template_schema_id,
        first.template_schema_version,
    )
    second_bindings = store.list_unit_bindings(
        second.template_schema_id,
        second.template_schema_version,
    )

    assert first.status == second.status == "approved"
    assert {region.region_id for region in first_regions}.isdisjoint(
        region.region_id for region in second_regions
    )
    assert {binding.binding_id for binding in first_bindings}.isdisjoint(
        binding.binding_id for binding in second_bindings
    )


def test_region_and_binding_ids_are_scoped_by_template_version(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FirstPlaceholderSuggester(),
    )
    template = service.analyze_and_activate_uploaded_template(
        ctx,
        filename="first.xlsx",
        content=_xlsx("{{project_summary}}"),
        template_name="First",
    )
    analysis = store.get_template_analysis(template.template_version_id)
    assert analysis is not None
    same_schema_other_template = template.model_copy(update={
        "template_version_id": "template-other-version",
    })

    regions, bindings = service._regions_and_bindings(template, analysis)
    other_regions, other_bindings = service._regions_and_bindings(
        same_schema_other_template,
        analysis,
    )

    assert {region.region_id for region in regions}.isdisjoint(
        region.region_id for region in other_regions
    )
    assert {binding.binding_id for binding in bindings}.isdisjoint(
        binding.binding_id for binding in other_bindings
    )


def test_scalar_fanout_stays_draft_with_actionable_review_reason(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FanoutSuggester(),
    )

    with pytest.raises(ValueError, match="automatic template activation failed"):
        service.analyze_and_activate_uploaded_template(
            ctx,
            filename="ambiguous.xlsx",
            content=_xlsx("{{summary_one}}", "{{summary_two}}"),
            template_name="Ambiguous",
        )

    template = store.list_templates()[0]
    analysis = store.get_template_analysis(template.template_version_id)
    assert template.status == "draft"
    assert analysis is not None
    assert analysis.status == "requires_human"
    assert analysis.activation_decision is not None
    assert "scalar_target_fanout" in analysis.activation_decision.reason_codes


def test_confirmation_rejects_a_ready_status_with_rejected_activation_decision(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
    monkeypatch: pytest.MonkeyPatch,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FanoutSuggester(),
    )
    analysis = service.analyze_uploaded_template(
        ctx,
        filename="ambiguous.xlsx",
        content=_xlsx("{{summary_one}}", "{{summary_two}}"),
        template_name="Ambiguous",
    )
    assert analysis.activation_decision is not None
    assert analysis.activation_decision.status == "requires_human"
    tampered = analysis.model_copy(update={"status": "ready_for_confirmation"})
    monkeypatch.setattr(store, "get_template_analysis_by_id", lambda _analysis_id: tampered)
    monkeypatch.setattr(store, "get_template_analysis", lambda _template_version_id: tampered)

    with pytest.raises(ValueError, match="activation decision rejects confirmation"):
        service.confirm_template_analysis(
            ctx,
            analysis_id=analysis.analysis_id,
            display_name="Ambiguous",
        )


def test_confirmation_rejects_a_ready_status_without_an_activation_decision(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
    monkeypatch: pytest.MonkeyPatch,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FirstPlaceholderSuggester(),
    )
    analysis = service.analyze_uploaded_template(
        ctx,
        filename="normal.xlsx",
        content=_xlsx("{{project_summary}}"),
        template_name="Normal",
    )
    tampered = analysis.model_copy(update={"activation_decision": None})
    monkeypatch.setattr(store, "get_template_analysis_by_id", lambda _analysis_id: tampered)
    monkeypatch.setattr(store, "get_template_analysis", lambda _template_version_id: tampered)

    with pytest.raises(ValueError, match="activation decision is required"):
        service.confirm_template_analysis(
            ctx,
            analysis_id=analysis.analysis_id,
            display_name="Normal",
        )


def test_ai_recommendation_mode_activates_without_manual_template_review(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS",
        True,
        raising=False,
    )
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_AiRecommendedHeaderSuggester(),
    )

    template = service.analyze_and_activate_uploaded_template(
        ctx,
        filename="ai-recommended.xlsx",
        content=_xlsx("Project", "Name", "Owner"),
        template_name="AI Recommended",
    )

    analysis = store.get_template_analysis(template.template_version_id)
    assert template.status == "approved"
    assert analysis is not None
    assert analysis.activation_decision is not None
    assert analysis.activation_decision.status == "auto_accepted"
    assert "table_header_target" in analysis.activation_decision.reason_codes


def test_human_correction_creates_hash_bound_revision_that_can_activate(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FanoutSuggester(),
    )
    analysis = service.analyze_uploaded_template(
        ctx,
        filename="ambiguous.xlsx",
        content=_xlsx("{{summary_one}}", "{{summary_two}}"),
        template_name="Ambiguous",
    )
    correction = TemplateMappingCorrection(
        analysis_id=analysis.analysis_id,
        expected_content_hash=analysis.content_hash,
        suggestions=[analysis.suggestions[0].model_copy(update={
            "target_unit_ids": [analysis.suggestions[0].target_unit_ids[0]],
        })],
        actor_id=ctx.user_id,
        comment="Use the first explicit placeholder.",
    )

    corrected = service.correct_template_analysis(ctx, correction=correction)

    assert corrected.analysis_id != analysis.analysis_id
    assert corrected.status == "ready_for_confirmation"
    assert corrected.activation_decision is not None
    assert corrected.activation_decision.status == "auto_accepted"
    assert corrected.correction_actor_id == "alice"
    assert corrected.correction_comment == "Use the first explicit placeholder."
    assert store.get_template_analysis_by_id(analysis.analysis_id) is not None
    assert store.get_template_analysis_by_id(corrected.analysis_id) is not None

    template = service.confirm_template_analysis(
        ctx,
        analysis_id=corrected.analysis_id,
        display_name="Ambiguous",
    )
    assert template.status == "approved"


def test_human_correction_rejects_a_different_template_hash(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FanoutSuggester(),
    )
    analysis = service.analyze_uploaded_template(
        ctx,
        filename="ambiguous.xlsx",
        content=_xlsx("{{summary_one}}", "{{summary_two}}"),
        template_name="Ambiguous",
    )
    correction = TemplateMappingCorrection(
        analysis_id=analysis.analysis_id,
        expected_content_hash="f" * 64,
        suggestions=analysis.suggestions,
        actor_id=ctx.user_id,
        comment="Wrong hash must fail.",
    )

    with pytest.raises(ValueError, match="content hash"):
        service.correct_template_analysis(ctx, correction=correction)


def test_template_review_rejects_a_different_api_bound_knowledge_base(
    store: DocumentAuthoringStore,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FirstPlaceholderSuggester(),
    )
    owner_ctx = RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        metadata={"document_template_kb_name": "shared", "department_id": 1},
        kb_permissions={"1:shared": "write"},
    )
    analysis = service.analyze_uploaded_template(
        owner_ctx,
        filename="normal.xlsx",
        content=_xlsx("{{project_summary}}"),
        template_name="Normal",
    )
    other_kb_ctx = RequestContext(
        user_id="bob",
        tenant_id="tenant-a",
        metadata={"document_template_kb_name": "other", "department_id": 1},
        kb_permissions={"1:other": "read"},
    )

    with pytest.raises(PermissionError, match="selected knowledge base"):
        service.get_template_analysis_for_review(
            other_kb_ctx,
            analysis_id=analysis.analysis_id,
        )


def test_store_migrates_current_analysis_into_revision_history(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FirstPlaceholderSuggester(),
    )
    analysis = service.analyze_uploaded_template(
        ctx,
        filename="normal.xlsx",
        content=_xlsx("{{project_summary}}"),
        template_name="Normal",
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "DELETE FROM template_analysis_revisions WHERE analysis_id = ?",
            (analysis.analysis_id,),
        )

    reopened = DocumentAuthoringStore(store.db_path, store.artifact_root)

    assert reopened.get_template_analysis_by_id(analysis.analysis_id) is not None


def test_store_compare_and_swap_rejects_a_second_correction_from_same_parent(
    store: DocumentAuthoringStore,
    ctx: RequestContext,
):
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_FirstPlaceholderSuggester(),
    )
    parent = service.analyze_uploaded_template(
        ctx,
        filename="normal.xlsx",
        content=_xlsx("{{project_summary}}"),
        template_name="Normal",
    )
    first = parent.model_copy(update={"analysis_id": "analysis-first"})
    second = parent.model_copy(update={"analysis_id": "analysis-second"})

    store.save_corrected_template_analysis(
        first,
        expected_parent_analysis_id=parent.analysis_id,
    )

    with pytest.raises(ValueError, match="stale"):
        store.save_corrected_template_analysis(
            second,
            expected_parent_analysis_id=parent.analysis_id,
        )
