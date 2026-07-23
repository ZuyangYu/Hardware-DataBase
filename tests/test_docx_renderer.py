from __future__ import annotations

import hashlib
import io
import zipfile
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from src.document_authoring.models import DocxFill, DocxFillPlan, RendererPolicy, TemplateVersion
from src.document_authoring.renderers.docx import DocxRenderer
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import DocxRegionSchema
from src.document_authoring.worker import DocumentGenerationWorker
from src.document_authoring.work_order_store import DocumentAuthoringStore


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": WORD_NS}


def _docx(*, text: str = "old", include_table: bool = False, include_content_control: bool = False, extra_parts: dict[str, bytes] | None = None) -> bytes:
    body = f"<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{text}</w:t></w:r></w:p>"
    if include_table:
        body += "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>table old</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    if include_content_control:
        body += (
            '<w:sdt><w:sdtPr><w:tag w:val="summary"/><w:id w:val="42"/></w:sdtPr>'
            "<w:sdtContent><w:p><w:r><w:t>control old</w:t></w:r></w:p></w:sdtContent></w:sdt>"
        )
    parts = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}<w:sectPr/></w:body></w:document>"
        ).encode(),
        "word/_rels/document.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        "word/media/keep.bin": b"must-remain-byte-identical",
    }
    parts.update(extra_parts or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return output.getvalue()


def _region(region_id: str, locator: dict[str, object], *, role: str = "semantic_draft", write_policy: str = "validated_draft") -> DocxRegionSchema:
    return DocxRegionSchema(region_id=region_id, locator=locator, role=role, write_policy=write_policy)


def _plan(region_id: str, value: str, *, template_version_id: str = "template") -> DocxFillPlan:
    return DocxFillPlan(template_version_id=template_version_id, fills=[DocxFill(region_id=region_id, value=value, semantic_unit_id="summary")])


def _policy() -> RendererPolicy:
    return RendererPolicy(renderer_policy_id="docx-policy", allowed_changed_parts=["word/document.xml"])


def _document_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    return "".join(root.itertext())


def _part(content: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        return package.read(name)


def test_docx_renderer_changes_only_approved_paragraph_part():
    source = _docx()

    output = DocxRenderer().render(source, [_region("p-0", {"paragraph_index": 0})], _plan("p-0", "new"), _policy())

    assert _document_text(output.content) == "new"
    assert output.integrity_manifest["changed_parts"] == ["word/document.xml"]
    assert b"<w:b" in _part(output.content, "word/document.xml")
    assert _part(output.content, "word/media/keep.bin") == _part(source, "word/media/keep.bin")


def test_docx_renderer_preserves_all_non_text_document_xml_bytes():
    source = _docx()
    output = DocxRenderer().render(source, [_region("p-0", {"paragraph_index": 0})], _plan("p-0", "new"), _policy())

    assert _part(output.content, "word/document.xml") == _part(source, "word/document.xml").replace(
        b">old</w:t>", b">new</w:t>"
    )


def test_docx_renderer_rejects_human_only_region():
    with pytest.raises(PermissionError, match="machine-written"):
        DocxRenderer().render(
            _docx(), [_region("human", {"paragraph_index": 0}, write_policy="human_only")],
            _plan("human", "new"), _policy(),
        )


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ({"table_index": 0, "row_index": 0, "cell_index": 0}, "new table"),
        ({"content_control_tag": "summary"}, "new control"),
        ({"content_control_id": 42}, "new control"),
    ],
)
def test_docx_renderer_resolves_explicit_table_and_content_control_locators(locator, expected):
    source = _docx(include_table=True, include_content_control=True)
    output = DocxRenderer().render(source, [_region("target", locator)], _plan("target", expected), _policy())

    assert expected in _document_text(output.content)


def test_docx_renderer_rejects_external_relationship_without_approved_active_content_policy():
    source = _docx(extra_parts={
        "word/_rels/document.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Target="https://example.test" TargetMode="External"/></Relationships>''',
    })

    with pytest.raises(PermissionError, match="active content"):
        DocxRenderer().render(source, [_region("p-0", {"paragraph_index": 0})], _plan("p-0", "new"), _policy())


def test_docx_renderer_keeps_document_xml_byte_identical_for_empty_fill_plan():
    source = _docx()
    output = DocxRenderer().render(
        source, [_region("p-0", {"paragraph_index": 0})],
        DocxFillPlan(template_version_id="template"), _policy(),
    )

    assert _part(output.content, "word/document.xml") == _part(source, "word/document.xml")
    assert output.integrity_manifest["changed_parts"] == []


def test_docx_renderer_rejects_fill_that_overlaps_registered_human_only_content_control():
    source = _docx(include_content_control=True)
    regions = [
        _region("outer", {"paragraph_index": 0}),
        _region("human", {"content_control_tag": "summary"}, role="human_approval", write_policy="never"),
    ]
    # Put the protected control inside the approved paragraph to make the
    # overlap explicit rather than relying on document order.
    document = _part(source, "word/document.xml").replace(
        b"</w:p><w:sdt>", b"<w:sdt>", 1
    ).replace(b"</w:sdt><w:sectPr", b"</w:sdt></w:p><w:sectPr", 1)
    package = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as archive, zipfile.ZipFile(package, "w") as rewritten:
        for info in archive.infolist():
            rewritten.writestr(info, document if info.filename == "word/document.xml" else archive.read(info.filename))

    with pytest.raises(PermissionError, match="overlaps a protected"):
        DocxRenderer().render(package.getvalue(), regions, _plan("outer", "new"), _policy())


def test_docx_renderer_rejects_duplicate_zip_member_names():
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as package:
        package.writestr("word/document.xml", b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>old</w:t></w:r></w:p></w:body></w:document>')
        package.writestr("word/_rels/document.xml.rels", b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        package.writestr("word/_rels/document.xml.rels", b"different duplicate relationship bytes")

    with pytest.raises(ValueError, match="duplicate ZIP member"):
        DocxRenderer().render(source.getvalue(), [_region("p-0", {"paragraph_index": 0})], _plan("p-0", "new"), _policy())


def test_service_dispatches_docx_fill_plan_to_docx_renderer():
    policy = _policy()
    calls: list[object] = []

    class Store:
        def read_template_content(self, template_version_id):
            assert template_version_id == "docx-template"
            return b"docx bytes"

        def list_docx_regions(self, schema_id, version):
            assert (schema_id, version) == ("docx-schema", "1")
            return ["docx-region"]

    class DocxRendererRecorder:
        def render(self, content, regions, fill_plan, renderer_policy, *, security_approved):
            calls.append((content, regions, fill_plan, renderer_policy, security_approved))
            return SimpleNamespace(content=b"rendered docx", integrity_manifest={"manifest_hash": "docx-manifest"})

    service = object.__new__(DocumentGenerationService)
    service.store = Store()
    service.docx_renderer = DocxRendererRecorder()
    service.workbook_renderer = None
    service._policy = lambda template: policy
    template = TemplateVersion(
        template_version_id="docx-template", template_id="docx", format="docx",
        content_hash=hashlib.sha256(b"docx bytes").hexdigest(),
        template_schema_id="docx-schema", template_schema_version="1", renderer_policy_id="docx-policy",
    )

    content, manifest = service._render_fill_plan(template, _plan("p-0", "new", template_version_id="docx-template"))

    assert content == b"rendered docx"
    assert manifest["manifest_hash"] == "docx-manifest"
    assert calls[0][4] is True


def test_service_rejects_fill_plan_for_different_frozen_template_version():
    service = object.__new__(DocumentGenerationService)
    service.store = SimpleNamespace(read_template_content=lambda _: b"unexpected")
    service._policy = lambda template: _policy()
    template = TemplateVersion(
        template_version_id="docx-template", template_id="docx", format="docx", content_hash="a" * 64,
        template_schema_id="docx-schema", template_schema_version="1", renderer_policy_id="docx-policy",
    )

    with pytest.raises(PermissionError, match="different frozen template"):
        service._render_fill_plan(template, _plan("p-0", "new", template_version_id="other-template"))


def test_service_keeps_existing_positional_worker_constructor_slot():
    worker = DocumentGenerationWorker()

    service = DocumentGenerationService(None, None, None, worker)

    assert service.worker is worker


def test_store_scopes_docx_region_ids_by_template_schema(tmp_path):
    store = DocumentAuthoringStore(db_path=str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts"))
    first = _region("paragraph:0", {"paragraph_index": 0})
    second = _region("paragraph:0", {"paragraph_index": 1})

    store.save_docx_regions("schema-a", "1", [first])
    store.save_docx_regions("schema-b", "1", [second])

    assert store.list_docx_regions("schema-a", "1") == [first]
    assert store.list_docx_regions("schema-b", "1") == [second]
