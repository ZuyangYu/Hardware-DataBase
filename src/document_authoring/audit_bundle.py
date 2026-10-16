"""A governed appendix beside immutable template bytes, with verifiable hashes."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

from pypdf import PdfReader, PdfWriter

from src.document_authoring.ooxml import sanitize_xml10_text, validate_ooxml_package
from src.result_exports.models import ResultEnvelope


def audit_json(audit: dict[str, Any]) -> bytes:
    return json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode('utf-8')


def _render_result(envelope: ResultEnvelope, output_format: str):
    # Import lazily: result_exports.renderers validates document_authoring OOXML
    # helpers, while the document-authoring package imports this module during
    # service initialization.
    from src.result_exports.renderers import render_result

    return render_result(envelope, output_format)


def _plain(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if isinstance(value, (dict, list, tuple)) else str('' if value is None else value)
    # Evidence and rationale are data, including Markdown-shaped instructions.
    # Escaping prevents references from creating links/images in the appendix.
    text = text.replace('\r', ' ').replace('\n', ' ')
    return re.sub(r'([\\`*\[\]()<>#!|])', r'\\\1', text)


def audit_markdown(audit: dict[str, Any]) -> str:
    lines = [
        '# 来源与决策审计附录',
        '',
        '本附录记录来源事实、审阅选择和批准待办；设计候选仍需正式变更批准。',
        '',
    ]
    for key in ('work_order_id', 'artifact_id', 'artifact_stage', 'artifact_content_hash',
                'source_snapshot_id', 'source_snapshot_hash', 'purpose', 'release_blocked'):
        lines.extend([f'{key}: {_plain(audit.get(key))}', ''])
    lines.extend(['## 冻结来源', ''])
    for source in audit.get('sources') or []:
        lines.extend([_plain(source), ''])
    for name in audit.get('source_names') or []:
        lines.extend([f'source_name: {_plain(name)}', ''])
    lines.extend(['## 继承内容的来源依赖', ''])
    for source in audit.get('inherited_sources') or []:
        lines.extend([_plain(source), ''])
    lines.extend(['## 原始事实与冲突处置', ''])
    for case in audit.get('conflicts') or []:
        lines.extend([f"conflict_id: {_plain(case.get('conflict_id'))}; status: {_plain(case.get('status'))}; field: {_plain(case.get('field_key'))}", ''])
        for claim in case.get('claims') or []:
            lines.extend([_plain(claim), ''])
        for decision in case.get('decisions') or []:
            lines.extend([_plain(decision), ''])
    for title, key in (('当前有效决定', 'effective_decisions'), ('待正式批准的设计建议', 'pending_approvals'),
                       ('执行审计引用', 'execution_audit_refs'), ('产物批准引用', 'approval_event_ids')):
        lines.extend([f'## {title}', '', _plain(audit.get(key) or []), ''])
    return '\n'.join(lines)


_WORD_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_SHEET_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_DOC_REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_CONTENT_TYPES_NS = 'http://schemas.openxmlformats.org/package/2006/content-types'

ET.register_namespace('w', _WORD_NS)
ET.register_namespace('r', _DOC_REL_NS)
ET.register_namespace('', _SHEET_NS)


def _normalized_audit_format(output_format: str) -> str:
    value = str(output_format or '').strip().lower().lstrip('.')
    return {
        'word': 'docx',
        'excel': 'xlsx',
        'powerpoint': 'pptx',
        'ppt': 'pptx',
        'md': 'markdown',
    }.get(value, value)


def _check_audit_content_hash(content: bytes, audit: dict[str, Any]) -> None:
    if hashlib.sha256(content).hexdigest() != audit.get('artifact_content_hash'):
        raise ValueError('artifact_content_hash_changed')


def _copy_zip_with_replacements(content: bytes, replacements: dict[str, bytes], additions: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content), 'r') as source, zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
        for name, data in (additions or {}).items():
            if name in source.namelist():
                raise ValueError(f'duplicate OOXML part: {name}')
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            target.writestr(info, data)
    return output.getvalue()


def _word_text_paragraph(text: str, *, heading: bool = False) -> ET.Element:
    paragraph = ET.Element(f'{{{_WORD_NS}}}p')
    if heading:
        properties = ET.SubElement(paragraph, f'{{{_WORD_NS}}}pPr')
        style = ET.SubElement(properties, f'{{{_WORD_NS}}}pStyle')
        style.set(f'{{{_WORD_NS}}}val', 'Heading1')
    run = ET.SubElement(paragraph, f'{{{_WORD_NS}}}r')
    node = ET.SubElement(run, f'{{{_WORD_NS}}}t')
    safe = sanitize_xml10_text(text)
    if safe[:1].isspace() or safe[-1:].isspace():
        node.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    node.text = safe
    return paragraph


def _embed_docx(content: bytes, markdown: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content), 'r') as package:
        source_xml = package.read('word/document.xml')
        namespaces = {
            prefix or '': uri
            for _event, (prefix, uri) in ET.iterparse(io.BytesIO(source_xml), events=('start-ns',))
        }
        for prefix, uri in namespaces.items():
            ET.register_namespace(prefix or '', uri)
        document = ET.fromstring(source_xml)
    body = document.find(f'{{{_WORD_NS}}}body')
    if body is None:
        raise ValueError('docx_document_body_missing')
    section = body.find(f'{{{_WORD_NS}}}sectPr')
    index = list(body).index(section) if section is not None else len(body)
    for line in markdown.splitlines():
        if not line:
            continue
        heading = line.startswith('# ')
        value = line[2:] if heading else line
        body.insert(index, _word_text_paragraph(value, heading=heading))
        index += 1
    serialized = ET.tostring(document, encoding='utf-8', xml_declaration=True)
    ignorable = document.attrib.get('{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable', '')
    missing_declarations = [
        f'xmlns:{prefix}="{namespaces[prefix]}"'
        for prefix in ignorable.split()
        if prefix in namespaces and re.search(rb'xmlns:' + prefix.encode('ascii') + rb'=', serialized) is None
    ]
    if missing_declarations:
        serialized = re.sub(
            rb'(<[^:>]+:document\b)',
            (rb'\1 ' + ' '.join(missing_declarations).encode('utf-8')),
            serialized,
            count=1,
        )
    rendered = _copy_zip_with_replacements(content, {'word/document.xml': serialized})
    validate_ooxml_package(rendered, 'docx')
    return rendered


def _embed_pdf(content: bytes, markdown: str) -> bytes:
    appendix = _render_result(ResultEnvelope(title='来源与决策审计附录', answer=markdown), 'pdf').content
    writer = PdfWriter()
    for source in (content, appendix):
        reader = PdfReader(io.BytesIO(source))
        if reader.is_encrypted:
            raise ValueError('encrypted_pdf_audit_embedding_unsupported')
        for page in reader.pages:
            writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _worksheet_audit_xml(lines: list[str]) -> bytes:
    worksheet = ET.Element(f'{{{_SHEET_NS}}}worksheet')
    ET.SubElement(worksheet, f'{{{_SHEET_NS}}}dimension', {'ref': f'A1:A{max(1, len(lines))}'})
    data = ET.SubElement(worksheet, f'{{{_SHEET_NS}}}sheetData')
    for row_number, line in enumerate(lines or [''], start=1):
        row = ET.SubElement(data, f'{{{_SHEET_NS}}}row', {'r': str(row_number)})
        cell = ET.SubElement(row, f'{{{_SHEET_NS}}}c', {'r': f'A{row_number}', 't': 'inlineStr'})
        inline = ET.SubElement(cell, f'{{{_SHEET_NS}}}is')
        text = ET.SubElement(inline, f'{{{_SHEET_NS}}}t')
        value = sanitize_xml10_text(line)[:32767]
        if value[:1].isspace() or value[-1:].isspace():
            text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        text.text = value
    return ET.tostring(worksheet, encoding='utf-8', xml_declaration=True)


def _embed_xlsx(content: bytes, markdown: str, output_format: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content), 'r') as package:
        workbook = ET.fromstring(package.read('xl/workbook.xml'))
        relationships = ET.fromstring(package.read('xl/_rels/workbook.xml.rels'))
        content_types = ET.fromstring(package.read('[Content_Types].xml'))
        existing_names = {
            sheet.attrib.get('name', '')
            for sheet in workbook.findall(f'{{{_SHEET_NS}}}sheets/{{{_SHEET_NS}}}sheet')
        }
        base_name = '来源与决策审计'
        name = base_name
        counter = 2
        while name in existing_names:
            suffix = f'-{counter}'
            name = (base_name[:31 - len(suffix)] + suffix)
            counter += 1
        sheet_numbers = [
            int(match.group(1))
            for part in package.namelist()
            for match in [re.fullmatch(r'xl/worksheets/sheet(\d+)\.xml', part)]
            if match
        ]
        sheet_number = max(sheet_numbers or [0]) + 1
        sheet_part = f'xl/worksheets/sheet{sheet_number}.xml'
        relation_numbers = [
            int(match.group(1))
            for relation in relationships
            for match in [re.fullmatch(r'rId(\d+)', relation.attrib.get('Id', ''))]
            if match
        ]
        relation_id = f'rId{max(relation_numbers or [0]) + 1}'
        sheet_ids = [
            int(sheet.attrib.get('sheetId', '0'))
            for sheet in workbook.findall(f'{{{_SHEET_NS}}}sheets/{{{_SHEET_NS}}}sheet')
            if sheet.attrib.get('sheetId', '').isdigit()
        ]
        sheet_id = str(max(sheet_ids or [0]) + 1)
        sheets = workbook.find(f'{{{_SHEET_NS}}}sheets')
        if sheets is None:
            sheets = ET.SubElement(workbook, f'{{{_SHEET_NS}}}sheets')
        ET.SubElement(sheets, f'{{{_SHEET_NS}}}sheet', {
            'name': name, 'sheetId': sheet_id, f'{{{_DOC_REL_NS}}}id': relation_id,
        })
        ET.SubElement(relationships, f'{{{_PKG_REL_NS}}}Relationship', {
            'Id': relation_id,
            'Type': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet',
            'Target': f'worksheets/sheet{sheet_number}.xml',
        })
        ET.SubElement(content_types, f'{{{_CONTENT_TYPES_NS}}}Override', {
            'PartName': f'/xl/worksheets/sheet{sheet_number}.xml',
            'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml',
        })
        replacements = {
            'xl/workbook.xml': ET.tostring(workbook, encoding='utf-8', xml_declaration=True),
            'xl/_rels/workbook.xml.rels': ET.tostring(relationships, encoding='utf-8', xml_declaration=True),
            '[Content_Types].xml': ET.tostring(content_types, encoding='utf-8', xml_declaration=True),
        }
    rendered = _copy_zip_with_replacements(content, replacements, {
        sheet_part: _worksheet_audit_xml(markdown.splitlines()),
    })
    validate_ooxml_package(rendered, output_format)
    return rendered


def _embed_pptx(content: bytes, markdown: str) -> bytes:
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except ImportError as exc:  # pragma: no cover - optional deployment dependency
        raise ValueError('pptx_audit_embedding_dependency_missing') from exc
    presentation = Presentation(io.BytesIO(content))
    layout = presentation.slide_layouts[6] if len(presentation.slide_layouts) > 6 else presentation.slide_layouts[-1]
    slide = presentation.slides.add_slide(layout)
    title = slide.shapes.add_textbox(Inches(0.6), Inches(0.3), Inches(12), Inches(0.6))
    title.text_frame.text = '来源与决策审计附录'
    title.text_frame.paragraphs[0].font.size = Pt(24)
    body = slide.shapes.add_textbox(Inches(0.6), Inches(1.0), Inches(12), Inches(6.0))
    frame = body.text_frame
    frame.clear()
    for index, line in enumerate(markdown.splitlines()):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = sanitize_xml10_text(line)
        paragraph.font.size = Pt(10)
    output = io.BytesIO()
    presentation.save(output)
    rendered = output.getvalue()
    validate_ooxml_package(rendered, 'pptx')
    return rendered


def build_embedded_audit_artifact(content: bytes, output_format: str, audit: dict[str, Any]) -> bytes:
    """Append the governed audit chapter to a copy of an Office/PDF artifact.

    The stored artifact remains immutable and keeps its original content hash;
    this function only creates a download representation after rechecking that
    hash.  Source and decision values are rendered as inert text.
    """
    normalized = _normalized_audit_format(output_format)
    if normalized not in {'docx', 'pdf', 'xlsx', 'xlsm', 'pptx', 'markdown'}:
        raise ValueError('unsupported_embedded_audit_format')
    _check_audit_content_hash(content, audit)
    markdown = audit_markdown(audit)
    if normalized == 'docx':
        return _embed_docx(content, markdown)
    if normalized == 'pdf':
        return _embed_pdf(content, markdown)
    if normalized in {'xlsx', 'xlsm'}:
        return _embed_xlsx(content, markdown, normalized)
    if normalized == 'pptx':
        return _embed_pptx(content, markdown)
    return (content.rstrip() + b'\n\n' + markdown.encode('utf-8'))


def build_audit_bundle(content: bytes, output_format: str, audit: dict[str, Any]) -> bytes:
    """Render a separate Office/PDF appendix without rewriting controlled bytes."""
    suffix = str(output_format or '').strip().lower().lstrip('.')
    if suffix not in {'xlsm', 'xlsx', 'docx', 'pdf', 'pptx', 'markdown', 'md'}:
        raise ValueError('unsupported_audit_bundle_format')
    if hashlib.sha256(content).hexdigest() != audit.get('artifact_content_hash'):
        raise ValueError('artifact_content_hash_changed')
    markdown = audit_markdown(audit)
    envelope = ResultEnvelope(title='来源与决策审计附录', answer=markdown)
    files = {
        f'document.{suffix}': content,
        'audit.json': audit_json(audit),
        'audit.md': markdown.encode('utf-8'),
        'audit.docx': _render_result(envelope, 'docx').content,
        'audit.pdf': _render_result(envelope, 'pdf').content,
    }
    manifest = {
        'schema_version': 1,
        'artifact_id': audit['artifact_id'],
        'source_snapshot_hash': audit['source_snapshot_hash'],
        'files': [{'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)} for path, data in files.items()],
    }
    files['manifest.json'] = audit_json(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        for path, data in files.items():
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            package.writestr(info, data)
    return buffer.getvalue()
