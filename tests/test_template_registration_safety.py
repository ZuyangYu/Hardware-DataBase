from __future__ import annotations

import hashlib
import io
import zipfile

from src.document_authoring.models import TemplateVersion, WorkbookRegionSchema
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import workbook_value_hash
from src.document_authoring.work_order_store import DocumentAuthoringStore


def _xlsx_with_fixed_value() -> bytes:
    parts = {
        "xl/workbook.xml": b'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": b'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Fixed label</t></is></c></row></sheetData></worksheet>''',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_registration_freezes_baseline_without_authorizing_nonempty_overwrite(
    tmp_path,
):
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "authoring-files"),
    )
    service = DocumentGenerationService(store=store)
    content = _xlsx_with_fixed_value()

    service.register_template(
        TemplateVersion(
            template_version_id="legacy-template",
            template_id="legacy",
            format="xlsx",
            content_hash=hashlib.sha256(content).hexdigest(),
            template_schema_id="legacy-schema",
            template_schema_version="1",
            renderer_policy_id="policy-render",
        ),
        content,
        regions=[WorkbookRegionSchema(
            region_id="legacy-cell",
            sheet_name="Review",
            locator={"cell": "A1"},
            role="semantic_draft",
            write_policy="validated_draft",
        )],
        bindings=[],
    )

    [saved] = store.list_workbook_regions("legacy-schema", "1")
    assert saved.expected_value_hash == workbook_value_hash("Fixed label")
    assert saved.allow_nonempty_overwrite is False
