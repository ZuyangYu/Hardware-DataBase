"""A governed appendix beside immutable template bytes, with verifiable hashes."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from typing import Any

from src.result_exports.models import ResultEnvelope
from src.result_exports.renderers import render_result


def audit_json(audit: dict[str, Any]) -> bytes:
    return json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode('utf-8')


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
        'audit.docx': render_result(envelope, 'docx').content,
        'audit.pdf': render_result(envelope, 'pdf').content,
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
