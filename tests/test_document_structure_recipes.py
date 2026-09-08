from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest
from pypdf import PdfReader

from src.document_authoring.document_model import (
    DocumentModel,
    ParagraphBlock,
    SectionBlock,
    TypedTableBlock,
)
from src.document_authoring.models import TypedTableRow
from src.document_authoring.planning.models import (
    DocumentPlan,
    StructureContract,
)
from src.document_authoring.planning.recipes import (
    StructureBindingCompiler,
    build_builtin_recipe_registry,
)
from src.document_authoring.renderers.structured import (
    StructuredDocxRenderer,
    StructuredPdfRenderer,
    StructuredXlsxRenderer,
)


def _model(plan_hash: str = "sha256:plan-recipe-test") -> DocumentModel:
    return DocumentModel(
        document_id="document:recipe-test",
        plan_id="plan:recipe-test",
        plan_version=1,
        plan_hash=plan_hash,
        blocks=[
            SectionBlock(unit_id="overview", block_id="block:overview", title="Overview", content="Safe report"),
            ParagraphBlock(unit_id="summary", block_id="block:summary", content="The result is evidence-bound."),
            TypedTableBlock(
                unit_id="signals",
                block_id="block:signals",
                columns=["signal", "value"],
                expected_row_keys=["J1:1", "J1:2"],
                rows=[
                    TypedTableRow(
                        row_key="J1:1",
                        cells={"signal": "CAN_H", "value": "3.3V"},
                        evidence_ids=["ev-1"],
                        cell_evidence_ids={"signal": ["ev-1"], "value": ["ev-1"]},
                    ),
                    TypedTableRow(
                        row_key="J1:2",
                        cells={"signal": "CAN_L", "value": "1.7V"},
                        evidence_ids=["ev-2"],
                        cell_evidence_ids={"signal": ["ev-2"], "value": ["ev-2"]},
                    ),
                ],
            ),
        ],
    )


def _plan(*, fmt: str = "docx", recipe_id: str = "generic-report") -> DocumentPlan:
    return DocumentPlan(
        document_plan_id="plan:recipe-test",
        version=1,
        status="accepted",
        output_spec_id="spec:recipe-test",
        output_spec_version=1,
        output_spec_hash="sha256:spec-recipe-test",
        source_snapshot_id="snapshot:recipe-test",
        source_snapshot_hash="sha256:snapshot-recipe-test",
        domain_strategy_id="generic_report",
        domain_strategy_version="1",
        layout_adapter_id="system_recipe",
        layout_adapter_version="1",
        renderer_capability_id=fmt,
        renderer_capability_version="1",
        layout_contract=StructureContract(
            structure_profile_id=recipe_id,
            structure_profile_version="1",
            components=[],
        ),
        semantic_units=[
            {"unit_id": "overview", "kind": "section"},
            {"unit_id": "summary", "kind": "paragraph"},
            {"unit_id": "signals", "kind": "table"},
        ],
        coverage_contract={
            "requirements": [
                {"requirement_id": "overview", "unit_id": "overview", "kind": "section"},
                {"requirement_id": "summary", "unit_id": "summary", "kind": "paragraph"},
                {
                    "requirement_id": "signals",
                    "unit_id": "signals",
                    "kind": "table",
                    "required_columns": ["signal", "value"],
                    "row_keys": ["J1:1", "J1:2"],
                },
            ]
        },
        unit_tasks=[
            {"task_id": "task:overview", "unit_id": "overview", "plan_version": 1, "action_key": "action:overview"},
            {"task_id": "task:summary", "unit_id": "summary", "plan_version": 1, "action_key": "action:summary"},
            {"task_id": "task:signals", "unit_id": "signals", "plan_version": 1, "action_key": "action:signals"},
        ],
        render_spec={"format": fmt, "recipe_id": recipe_id, "recipe_version": "1"},
    )


def test_builtin_recipe_registry_requires_exact_version_and_exposes_safe_recipes():
    registry = build_builtin_recipe_registry()

    report = registry.resolve("generic-report", "1")
    table = registry.resolve("structured-table", "1")

    assert report.recipe_id == "generic-report"
    assert "docx" in report.supported_formats
    assert "pdf" in report.supported_formats
    assert table.supported_formats == ["xlsx"]
    assert registry.lookup("generic-report", "2") is None
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(report)


def test_structure_binding_rejects_unknown_component_and_unapproved_hints():
    compiler = StructureBindingCompiler(build_builtin_recipe_registry())
    plan = _plan()
    model = _model(plan.plan_hash)

    plan.layout_contract = StructureContract(
        structure_profile_id="generic-report",
        structure_profile_version="1",
        components=["component:not-registered"],
    )
    with pytest.raises(ValueError, match="component"):
        compiler.compile(plan, model)

    model.blocks[0].layout_hints = {"raw_xml": "<w:p/>"}
    with pytest.raises(ValueError, match="layout hint"):
        compiler.compile(_plan(), model)


def test_structure_renderers_emit_valid_deterministic_safe_artifacts():
    model = _model(_plan().plan_hash)
    registry = build_builtin_recipe_registry()
    binding = StructureBindingCompiler(registry).compile(_plan(), model)
    recipe = registry.resolve("generic-report", "1")

    first_docx = StructuredDocxRenderer().render(model, recipe, binding)
    second_docx = StructuredDocxRenderer().render(model, recipe, binding)
    assert first_docx.content == second_docx.content
    assert first_docx.integrity_manifest["active_content_status"] == "clean"
    with ZipFile(BytesIO(first_docx.content)) as package:
        assert "word/document.xml" in package.namelist()
        xml = package.read("word/document.xml").decode("utf-8")
        assert "Safe report" in xml
        assert "CAN_H" in xml

    pdf_plan = _plan(fmt="pdf")
    pdf_model = _model(pdf_plan.plan_hash)
    pdf_binding = StructureBindingCompiler(registry).compile(pdf_plan, pdf_model)
    first_pdf = StructuredPdfRenderer().render(pdf_model, recipe, pdf_binding)
    second_pdf = StructuredPdfRenderer().render(pdf_model, recipe, pdf_binding)
    assert first_pdf.content == second_pdf.content
    assert first_pdf.content.startswith(b"%PDF")
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(first_pdf.content)).pages)
    assert "Safe report" in pdf_text
    assert "CAN_L" in pdf_text


def test_structured_table_recipe_emits_parseable_xlsx_and_rejects_formula_text():
    registry = build_builtin_recipe_registry()
    table_plan = _plan(fmt="xlsx", recipe_id="structured-table")
    model = _model(table_plan.plan_hash)
    binding = StructureBindingCompiler(registry).compile(table_plan, model)
    recipe = registry.resolve("structured-table", "1")

    rendered = StructuredXlsxRenderer().render(model, recipe, binding)
    assert rendered.content[:2] == b"PK"
    with ZipFile(BytesIO(rendered.content)) as package:
        assert "[Content_Types].xml" in package.namelist()
        assert "xl/workbook.xml" in package.namelist()
        assert "xl/worksheets/sheet1.xml" in package.namelist()
        xml = package.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "J1:1" in xml
        assert "CAN_L" in xml

    model.blocks[2].rows[0].cells["value"] = "=HYPERLINK(\"https://evil.invalid\",\"x\")"
    with pytest.raises(ValueError, match="formula-like"):
        StructuredXlsxRenderer().render(model, recipe, binding)
