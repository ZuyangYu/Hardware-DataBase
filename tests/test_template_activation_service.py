from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest

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
