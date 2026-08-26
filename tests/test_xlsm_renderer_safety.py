from __future__ import annotations

import io
import zipfile

import pytest

from src.document_authoring.models import (
    RendererPolicy,
    WorkbookFill,
    WorkbookFillPlan,
    WorkbookRegionSchema,
)
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.template_analysis import workbook_value_hash
from src.document_authoring.validator import DocumentValidator


def _xlsx(value: str | None = "{{summary}}") -> bytes:
    cell = (
        ""
        if value is None
        else f'<c r="A1" t="inlineStr"><is><t>{value}</t></is></c>'
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
            f'<sheetData><row r="1">{cell}</row></sheetData></worksheet>'
        ).encode(),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def _region(
    *,
    expected: str | None = "{{summary}}",
    allow_nonempty_overwrite: bool = True,
) -> WorkbookRegionSchema:
    return WorkbookRegionSchema(
        region_id="region-a1",
        sheet_name="Review",
        locator={"cell": "A1"},
        role="semantic_draft",
        write_policy="validated_draft",
        value_type="text",
        expected_value_hash=workbook_value_hash(expected),
        allow_nonempty_overwrite=allow_nonempty_overwrite,
    )


def _plan(*fills: WorkbookFill) -> WorkbookFillPlan:
    return WorkbookFillPlan(
        template_version_id="template-1",
        fills=list(fills) or [
            WorkbookFill(
                region_id="region-a1",
                value="Generated summary",
                semantic_unit_id="summary",
            ),
        ],
    )


def _policy() -> RendererPolicy:
    return RendererPolicy(
        renderer_policy_id="renderer-1",
        allowed_changed_parts=["xl/worksheets/"],
    )


def test_renderer_rejects_a_changed_cell_baseline():
    with pytest.raises(PermissionError, match="baseline"):
        XlsmRenderer().render(
            _xlsx("changed after review"),
            [_region(expected="{{summary}}")],
            _plan(),
            _policy(),
            security_approved=True,
        )


def test_renderer_rejects_an_unauthorized_nonempty_overwrite():
    with pytest.raises(PermissionError, match="non-empty"):
        XlsmRenderer().render(
            _xlsx("Fixed label"),
            [_region(expected="Fixed label", allow_nonempty_overwrite=False)],
            _plan(),
            _policy(),
            security_approved=True,
        )


def test_renderer_rejects_nonempty_legacy_region_without_a_frozen_baseline():
    legacy_region = _region(expected="Fixed label")
    legacy_region.expected_value_hash = None
    legacy_region.allow_nonempty_overwrite = True

    with pytest.raises(PermissionError, match="baseline"):
        XlsmRenderer().render(
            _xlsx("Fixed label"),
            [legacy_region],
            _plan(),
            _policy(),
            security_approved=True,
        )


@pytest.mark.parametrize("reference", ["A0", "A1foo", "$A$1", "XFE1", "A1048577"])
def test_workbook_region_rejects_noncanonical_or_out_of_bounds_cell_references(
    reference: str,
):
    with pytest.raises(ValueError, match="valid Excel A1 reference"):
        WorkbookRegionSchema.model_validate({
            **_region().model_dump(),
            "locator": {"cell": reference},
        })


def test_renderer_rejects_duplicate_fill_targets():
    fill = WorkbookFill(
        region_id="region-a1",
        value="Generated summary",
        semantic_unit_id="summary",
    )

    with pytest.raises(ValueError, match="duplicate"):
        XlsmRenderer().render(
            _xlsx(),
            [_region()],
            _plan(fill, fill.model_copy()),
            _policy(),
            security_approved=True,
        )


def test_renderer_emits_exact_cell_change_manifest():
    result = XlsmRenderer().render(
        _xlsx(),
        [_region()],
        _plan(),
        _policy(),
        security_approved=True,
    )

    assert result.integrity_manifest["cell_changes"] == [{
        "sheet_name": "Review",
        "cell": "A1",
        "baseline_value_hash": workbook_value_hash("{{summary}}"),
        "generated_value_hash": workbook_value_hash("Generated summary"),
        "baseline_empty": False,
        "semantic_unit_id": "summary",
        "region_id": "region-a1",
    }]


def test_renderer_rejects_abnormal_duplicate_long_values():
    long_value = "I2C interface description " * 8
    regions = [
        WorkbookRegionSchema(
            region_id=f"region-{cell}",
            sheet_name="Review",
            locator={"cell": cell},
            role="semantic_draft",
            write_policy="validated_draft",
            expected_value_hash=workbook_value_hash(None),
        )
        for cell in ("A1", "B1")
    ]
    plan = _plan(*[
        WorkbookFill(
            region_id=region.region_id,
            value=long_value,
            semantic_unit_id=f"field-{index}",
        )
        for index, region in enumerate(regions)
    ])

    with pytest.raises(ValueError, match="duplicate long value"):
        XlsmRenderer().render(
            _xlsx(None),
            regions,
            plan,
            _policy(),
            security_approved=True,
        )


def test_validator_fails_custom_renderer_cell_policy_violations():
    report = DocumentValidator().validate(
        work_order_id="work-order-1",
        matrix_rows=[],
        integrity_manifest={
            "manifest_hash": "manifest-1",
            "policy_violations": [],
            "cell_policy_violations": ["scalar_fanout"],
        },
    )

    assert report.status == "failed"
    assert report.issues == [{
        "kind": "renderer_integrity",
        "message": "scalar_fanout",
    }]
