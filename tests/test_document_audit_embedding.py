from __future__ import annotations

import hashlib
import io
import zipfile
from xml.etree import ElementTree as ET

from docx import Document
from pypdf import PdfReader

from src.document_authoring.audit_bundle import build_embedded_audit_artifact
from src.result_exports.models import ResultEnvelope
from src.result_exports.renderers import render_result


def _audit(content: bytes) -> dict:
    return {
        "artifact_id": "artifact-1",
        "artifact_content_hash": hashlib.sha256(content).hexdigest(),
        "source_snapshot_hash": "snapshot-hash",
        "source_snapshot_id": "snapshot-1",
        "work_order_id": "work-1",
        "artifact_stage": "review_candidate",
        "purpose": "compose",
        "release_blocked": True,
        "sources": [{"kb_id": 101, "kb_name": "Target", "role": "primary"}],
        "conflicts": [{
            "conflict_id": "conflict-1",
            "status": "approval_pending",
            "field_key": "input_voltage",
            "claims": [{"claim_id": "claim-5v", "normalized_value": "5"}],
            "decisions": [{"status": "approval_pending", "decision": {"action": "choose_claim"}}],
        }],
        "effective_decisions": [],
        "pending_approvals": [{"conflict_id": "conflict-1"}],
        "inherited_sources": [],
        "source_names": ["requirements.docx"],
        "execution_audit_refs": ["execution-1"],
        "approval_event_ids": [],
    }


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("Approved target requirement remains 5 V.")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def test_embedded_docx_audit_is_an_appendix_inside_a_copy_of_the_document():
    original = _docx_bytes()
    embedded = build_embedded_audit_artifact(original, "docx", _audit(original))

    assert embedded != original
    document = Document(io.BytesIO(embedded))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Approved target requirement remains 5 V." in text
    assert "来源与决策审计附录" in text
    assert "approval_pending" in text
    assert "claim-5v" in text


def test_embedded_pdf_audit_is_an_extra_section_after_original_pages():
    original = render_result(ResultEnvelope(title="Original", answer="Approved target requirement remains 5 V."), "pdf").content
    embedded = build_embedded_audit_artifact(original, "pdf", _audit(original))

    assert len(PdfReader(io.BytesIO(embedded)).pages) > len(PdfReader(io.BytesIO(original)).pages)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(embedded)).pages)
    assert "Original" in text
    assert "来源与决策审计附录" in text
    assert "approval_pending" in text


def test_embedded_xlsx_audit_is_a_new_worksheet():
    original = render_result(ResultEnvelope(title="Original", answer="Approved target requirement remains 5 V."), "xlsx").content
    embedded = build_embedded_audit_artifact(original, "xlsx", _audit(original))

    with zipfile.ZipFile(io.BytesIO(embedded)) as package:
        workbook = ET.fromstring(package.read("xl/workbook.xml"))
        names = {sheet.attrib["name"] for sheet in workbook.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets/{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet")}
        assert "来源与决策审计" in names
        relationships = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
        relation_id = next(sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"] for sheet in workbook.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets/{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet") if sheet.attrib["name"] == "来源与决策审计")
        target = next(relation.attrib["Target"] for relation in relationships if relation.attrib["Id"] == relation_id)
        audit_sheet = "xl/" + target.lstrip("/")
        values = [node.text for node in ET.fromstring(package.read(audit_sheet)).iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
    assert any("approval_pending" in str(value) for value in values)
    assert any("claim-5v" in str(value) for value in values)


def test_embedded_artifact_rejects_changed_source_bytes():
    original = _docx_bytes()
    audit = _audit(original)
    with __import__("pytest").raises(ValueError, match="artifact_content_hash_changed"):
        build_embedded_audit_artifact(original + b"changed", "docx", audit)
