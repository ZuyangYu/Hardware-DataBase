"""Task 6 deterministic XLSX/XLSM artifact review tests."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from src.document_authoring.models import RendererPolicy, WorkbookFill, WorkbookFillPlan, WorkbookRegionSchema
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.template_analysis import workbook_value_hash
from src.document_authoring.document_model import DocumentModel, ParagraphBlock
from src.document_authoring.planning.artifact_review import ArtifactReviewer
from src.document_authoring.planning.document_review import DocumentReviewer


def _xlsx(*, value_a1: str = "Fixed", formula: str = "SUM(1,2)", merge: str = "A3:B3", sheet: str = "Review") -> bytes:
    def cell(ref: str, value: str, *, formula_text: str | None = None) -> str:
        formula_xml = f"<f>{escape(formula_text)}</f>" if formula_text is not None else ""
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is>{formula_xml}</c>'

    rows = (
        f'<row r="1">{cell("A1", value_a1)}{cell("B1", "3", formula_text=formula)}</row>'
        f'<row r="2"><c r="A2"/></row>'
        f'<row r="3"><c r="A3"/><c r="B3"/></row>'
    )
    merge_xml = f'<mergeCells count="1"><mergeCell ref="{merge}"/></mergeCells>' if merge else ""
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
        "xl/workbook.xml": f'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>'''.encode(),
        "xl/_rels/workbook.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{rows}</sheetData>{merge_xml}</worksheet>"
        ).encode(),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def _model() -> DocumentModel:
    return DocumentModel(
        document_id="doc-1", plan_id="plan-1", plan_version=1, plan_hash="plan-hash",
        blocks=[ParagraphBlock(block_id="b-summary", unit_id="summary", content="Generated")],
    )


def _plan(**render_spec):
    return SimpleNamespace(
        document_plan_id="plan-1", version=1, plan_hash="plan-hash", status="accepted",
        render_spec={"format": "xlsx", **render_spec},
    )


def _render_result(content: bytes, *, manifest=None):
    try:
        security_report = XlsmRenderer().inspect(content, "xlsx")
    except Exception:
        security_report = SimpleNamespace(format="xlsx")
    return SimpleNamespace(
        content=content,
        security_report=security_report,
        integrity_manifest=manifest or {
            "manifest_hash": "manifest-1", "policy_violations": [],
            "cell_policy_violations": [], "cell_changes": [],
        },
    )


def test_artifact_review_checks_sheet_mapping_static_formula_merge_and_allowlist():
    template = _xlsx()
    region = WorkbookRegionSchema(
        region_id="summary-a2", sheet_name="Review", locator={"cell": "A2"},
        role="semantic_draft", write_policy="validated_draft",
        expected_value_hash=workbook_value_hash(None),
    )
    result = XlsmRenderer().render(
        template, [region], WorkbookFillPlan(
            template_version_id="template-1",
            fills=[WorkbookFill(region_id="summary-a2", value="Generated", semantic_unit_id="summary")],
        ), RendererPolicy(renderer_policy_id="policy-1", allowed_changed_parts=["xl/worksheets/"]),
        security_approved=True,
    )
    report = ArtifactReviewer().review(
        _plan(
            expected_sheets=["Review"],
            static_cells={"Review!A1": "Fixed"},
            formula_cells={"Review!B1": "SUM(1,2)"},
            merged_ranges={"Review": ["A3:B3"]},
            allowlisted_cells=["Review!A2"],
        ), _model(), result, result.content,
    )

    assert report.status == "pass"
    assert report.issues == []


def test_artifact_review_rejects_wrong_sheet_static_overwrite_and_overflow():
    content = _xlsx(value_a1="Changed")
    report = ArtifactReviewer().review(
        _plan(
            expected_sheets=["Review"],
            static_cells={"Review!A1": "Fixed"},
            allowlisted_cells=["Review!A2"],
            max_cell_lengths={"Review!A2": 5},
        ), _model(), _render_result(content, manifest={
            "manifest_hash": "manifest-1", "policy_violations": [],
            "cell_policy_violations": [],
            "cell_changes": [{"sheet_name": "Missing", "cell": "A2", "semantic_unit_id": "summary"}],
        }), content,
    )

    codes = {issue.code for issue in report.issues}
    assert report.status == "blocked"
    assert {"wrong_physical_sheet", "static_content_overwrite", "non_allowlisted_cell"} <= codes


def test_artifact_review_rejects_formula_merge_changes_and_overflow():
    content = _xlsx(formula="OTHER()", merge="A4:B4")
    report = ArtifactReviewer().review(
        _plan(
            formula_cells={"Review!B1": "SUM(1,2)"},
            merged_ranges={"Review": ["A3:B3"]},
            required_values={"Review!A2": "a value longer than five"},
            max_cell_lengths={"Review!A2": 5},
        ), _model(), _render_result(content), content,
    )

    codes = {issue.code for issue in report.issues}
    assert {"formula_changed", "merge_changed", "overflow_or_truncation"} <= codes


def test_artifact_review_blocks_malformed_ooxml_and_xlsm_active_content_policy_violation():
    malformed = ArtifactReviewer().review(
        _plan(), _model(), _render_result(b"not a zip"), b"not a zip"
    )
    assert malformed.status == "blocked"
    assert any(issue.code == "malformed_ooxml" for issue in malformed.issues)

    macro_output = io.BytesIO()
    with zipfile.ZipFile(macro_output, "w") as archive:
        with zipfile.ZipFile(io.BytesIO(_xlsx())) as source:
            for info in source.infolist():
                archive.writestr(info.filename, source.read(info.filename))
        archive.writestr("xl/vbaProject.bin", b"macro")
    macro_bytes = macro_output.getvalue()
    macro_result = _render_result(macro_bytes, manifest={
        "manifest_hash": "manifest-1", "policy_violations": [],
        "cell_policy_violations": [], "cell_changes": [],
        "before_parts": {"xl/vbaProject.bin": "before"},
        "after_parts": {"xl/vbaProject.bin": "after"},
    })
    macro_report = ArtifactReviewer().review(
        SimpleNamespace(
            document_plan_id="plan-1", version=1, plan_hash="plan-hash", status="accepted",
            render_spec={"format": "xlsm", "macro_policy": "preserve"},
        ), _model(), macro_result, macro_bytes,
    )
    assert any(issue.code == "macro_package_changed" for issue in macro_report.issues)


def test_document_reviewer_post_render_binds_model_and_artifact_hash():
    content = _xlsx()
    result = _render_result(content)
    report = DocumentReviewer().post_render(_plan(), _model(), result, content)

    assert report.stage == "post_render"
    assert report.artifact_hash
    assert report.model_hash == _model().model_hash
    assert report.render_manifest_hash == "manifest-1"


def test_workbook_renderer_manifest_exposes_before_and_after_package_parts():
    template = _xlsx()
    region = WorkbookRegionSchema(
        region_id="summary-a2", sheet_name="Review", locator={"cell": "A2"},
        role="semantic_draft", write_policy="validated_draft",
        expected_value_hash=workbook_value_hash(None),
    )
    result = XlsmRenderer().render(
        template, [region], WorkbookFillPlan(
            template_version_id="template-1",
            fills=[WorkbookFill(region_id="summary-a2", value="Generated", semantic_unit_id="summary")],
        ), RendererPolicy(renderer_policy_id="policy-1", allowed_changed_parts=["xl/worksheets/"]),
        security_approved=True,
    )

    assert result.integrity_manifest["before_parts"]
    assert result.integrity_manifest["after_parts"]


@pytest.mark.parametrize("bad_bytes", [b"", b"<worksheet/>", b"PK\\x03\\x04broken"])
def test_artifact_review_never_releases_unparseable_bytes(bad_bytes: bytes):
    report = ArtifactReviewer().review(
        _plan(), _model(), _render_result(bad_bytes), bad_bytes
    )
    assert report.status == "blocked"
