"""R3 governed exports preserve templates and fail closed during rollback."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from docx import Document
from pypdf import PdfReader

import src.settings
from src.document_authoring.conflicts.detector import ConflictDetector
from src.document_authoring.conflicts.models import ConflictDecision
from src.document_authoring.conflicts.normalizer import normalize_claim
from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext


def _service(tmp_path):
    return DocumentGenerationService(store=DocumentAuthoringStore(
        str(tmp_path / 'authoring.db'), str(tmp_path / 'artifacts'),
    ))


@pytest.mark.parametrize('order', [
    SimpleNamespace(kb_scope_snapshot={'sources': [{'kb_id': 1}, {'kb_id': 2}]}),
    SimpleNamespace(source_snapshot_kind='multi_knowledge_v1'),
])
def test_disabled_review_refuses_real_multi_kb_work_while_preserving_legacy(tmp_path, monkeypatch, order):
    service = _service(tmp_path)
    monkeypatch.setattr(src.settings, 'MULTI_KB_HUMAN_REVIEW_ENABLED', False, raising=False)
    with pytest.raises(ValueError, match='multi_kb_human_review_disabled'):
        service.require_human_review_available(order)
    service.require_human_review_available(SimpleNamespace(kb_scope_snapshot={}))
    service.require_human_review_available(SimpleNamespace(kb_scope_snapshot={'purpose': 'compose'}))
    monkeypatch.setattr(src.settings, 'MULTI_KB_HUMAN_REVIEW_ENABLED', True)
    service.require_human_review_available(order)


def _bundle_fixture(tmp_path, *, action='choose_design_candidate', source_approval='draft'):
    service = _service(tmp_path)
    document = Document()
    document.add_paragraph('Approved target requirement remains 5 V.')
    buffer = io.BytesIO()
    document.save(buffer)
    original = buffer.getvalue()
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id='default', knowledge_base_name='Target', source_names=['requirements.docx'], created_by='alice',
        source_snapshot_kind='multi_knowledge_v1',
        sources=[
            {'kb_id': 101, 'kb_name': 'Target', 'role': 'primary', 'records': [{'file_id': 'a', 'content_hash': 'a' * 64}]},
            {'kb_id': 205, 'kb_name': 'Reference', 'role': 'reference', 'records': [{'file_id': 'b', 'content_hash': 'b' * 64}]},
        ],
    )
    claims = [normalize_claim(
        claim_id=claim_id, field_key='input_voltage', subject='power module', predicate='requires',
        value=value, unit='V', source_kb_id=kb_id, source_role=role,
        approval_status=source_approval,
        evidence_id=evidence_id, source_content_hash=source_hash, locator={'page': page},
    ) for claim_id, value, kb_id, role, evidence_id, source_hash, page in (
        ('target-5v', '5', 101, 'primary', 'ev-target', 'a' * 64, 3),
        ('ref-12v', '12', 205, 'reference', 'ev-reference', 'b' * 64, 7),
    )]
    case = ConflictDetector().detect(claims)[0].model_copy(update={'snapshot_hash': snapshot.content_hash})
    service.conflict_store.save_case(case, tenant_id='default', work_order_id='work-1')
    service.conflict_store.submit_decision(
        case.conflict_id, tenant_id='default', actor_id='alice', expected_fingerprint=case.fingerprint,
        expected_snapshot_hash=case.snapshot_hash, expected_evidence_set_hash=case.evidence_set_hash,
        decision=ConflictDecision(conflict_id=case.conflict_id, action=action,
                                  selected_claim_id='ref-12v', requires_human=True, blocking=True,
                                  rationale='Propose 12 V; target requirement still needs change approval.'),
        idempotency_key='design-12v',
    )
    artifact = SimpleNamespace(artifact_id='artifact-1', work_order_id='work-1',
                               content_hash=hashlib.sha256(original).hexdigest(), stage='review_candidate',
                               output_format='docx', approval_event_ids=[])
    order = SimpleNamespace(work_order_id='work-1', tenant_id='default', target_format='docx',
                            kb_scope_snapshot={'purpose': 'compose', 'sources': snapshot.sources})
    service._artifact_for_context = lambda *_args: artifact
    service._order = lambda *_args: order
    service._order_raw = lambda *_args: order
    service.require_work_order_capability = lambda *_args: None
    service.resolve_source_snapshot = lambda _order: snapshot
    service.store.read_artifact_content = lambda _artifact_id: original
    return service, RequestContext(user_id='alice', tenant_id='default'), original, case


def test_controlled_bundle_binds_unmodified_office_bytes_frozen_sources_and_pending_design(tmp_path):
    service, ctx, original, case = _bundle_fixture(tmp_path)
    bundle = service.download_document_artifact_bundle(ctx, 'artifact-1')
    with zipfile.ZipFile(io.BytesIO(bundle)) as package:
        assert set(package.namelist()) == {'document.docx', 'audit.json', 'audit.md', 'audit.docx', 'audit.pdf', 'manifest.json'}
        assert package.read('document.docx') == original
        audit = json.loads(package.read('audit.json'))
        assert audit['artifact_content_hash'] == hashlib.sha256(original).hexdigest()
        assert audit['release_blocked'] is True
        assert audit['pending_approvals'][0]['conflict_id'] == case.conflict_id
        assert audit['effective_decisions'] == []
        assert audit['sources'][0]['role'] == 'primary'
        assert audit['sources'][1]['role'] == 'reference'
        assert audit['conflicts'][0]['claims'][0]['normalized_value'] == '5'
        assert audit['conflicts'][0]['decisions'][0]['status'] == 'approval_pending'
        manifest = json.loads(package.read('manifest.json'))
        for entry in manifest['files']:
            assert entry['sha256'] == hashlib.sha256(package.read(entry['path'])).hexdigest()
        office_text = '\n'.join(p.text for p in Document(io.BytesIO(package.read('audit.docx'))).paragraphs)
        pdf_text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(package.read('audit.pdf'))).pages)
        for text in (office_text, pdf_text, package.read('audit.md').decode()):
            assert 'approval_pending' in text
            assert 'target-5v' in text and 'ref-12v' in text
            assert 'primary' in text and 'reference' in text
            assert 'ev-target' in text and 'ev-reference' in text


def test_embedded_audit_document_keeps_original_artifact_immutable(tmp_path):
    service, ctx, original, case = _bundle_fixture(tmp_path)
    embedded = service.download_document_artifact_with_audit(ctx, 'artifact-1')

    assert service.store.read_artifact_content('artifact-1') == original
    text = '\n'.join(paragraph.text for paragraph in Document(io.BytesIO(embedded)).paragraphs)
    assert 'Approved target requirement remains 5 V.' in text
    assert '来源与决策审计附录' in text
    assert case.conflict_id in text


def test_bundle_rechecks_permissions_after_render_and_rejects_changed_bytes(tmp_path):
    service, ctx, _original, _case = _bundle_fixture(tmp_path)
    checks = []

    def revoke_after_initial_read(*_args):
        checks.append(True)
        if len(checks) > 2:
            raise PermissionError('source_unavailable')

    service.source_snapshot_validator = revoke_after_initial_read
    with pytest.raises(PermissionError, match='source_unavailable'):
        service.download_document_artifact_bundle(ctx, 'artifact-1')
    service.source_snapshot_validator = None
    service._artifact_for_context = lambda *_args: SimpleNamespace(
        artifact_id='artifact-1', work_order_id='work-1', content_hash='0' * 64,
        stage='review_candidate', output_format='docx', approval_event_ids=[],
    )
    with pytest.raises(ValueError, match='artifact_content_hash_changed'):
        service.download_document_artifact_bundle(ctx, 'artifact-1')


def test_rollback_blocks_formal_publication_even_without_detected_cases(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(src.settings, 'MULTI_KB_HUMAN_REVIEW_ENABLED', False, raising=False)
    order = SimpleNamespace(work_order_id='work-1', tenant_id='default',
                            kb_scope_snapshot={'sources': [{'kb_id': 1}, {'kb_id': 2}]})
    service._artifact_for_context = lambda *_args: SimpleNamespace(stage='review_candidate', work_order_id='work-1')
    service._order = lambda *_args: order
    with pytest.raises(ValueError, match='multi_kb_human_review_disabled'):
        service.approve_document_artifact(RequestContext(user_id='alice'), 'artifact-1')


def test_reviewed_draft_choice_does_not_satisfy_critical_specification_approval(tmp_path, monkeypatch):
    service, ctx, _original, case = _bundle_fixture(tmp_path, action='choose_claim')
    monkeypatch.setattr(src.settings, 'MULTI_KB_HUMAN_REVIEW_ENABLED', True, raising=False)
    monkeypatch.setattr(src.settings, 'MULTI_KB_REVIEW_REQUIRE_APPROVAL_FOR_CRITICAL', True, raising=False)
    assert service.conflict_store.get_case(case.conflict_id).status == 'resolved'
    audit = service.document_artifact_audit(ctx, 'artifact-1')
    assert audit['release_blocked'] is True
    assert audit['pending_approvals'][0]['approval_reason'] == 'critical_specification_approval_required'
    with pytest.raises(ValueError, match='critical_specification_approval_required'):
        service.approve_document_artifact(ctx, 'artifact-1')


def test_reviewing_an_approved_reference_still_requires_target_specification_change_approval(tmp_path, monkeypatch):
    service, ctx, _original, _case = _bundle_fixture(tmp_path, action='accept_authoritative_source', source_approval='approved')
    monkeypatch.setattr(src.settings, 'MULTI_KB_REVIEW_REQUIRE_APPROVAL_FOR_CRITICAL', True, raising=False)
    audit = service.document_artifact_audit(ctx, 'artifact-1')
    assert audit['release_blocked'] is True
    assert audit['pending_approvals'][0]['approval_reason'] == 'critical_specification_approval_required'


@pytest.mark.parametrize('operation', [
    'document_artifact_audit',
    'download_document_artifact',
    'download_document_artifact_with_audit',
    'preview_document_artifact',
])
def test_derived_artifact_reads_recheck_inherited_source_permissions(tmp_path, operation):
    service, ctx, _original, _case = _bundle_fixture(tmp_path)
    artifact = service._artifact_for_context(ctx, 'artifact-1')
    artifact.parent_artifact_id = 'parent-artifact'
    parent = SimpleNamespace(artifact_id='parent-artifact', work_order_id='parent-order',
                             parent_artifact_id=None, stage='approved_release')
    service.store.get_artifact = lambda artifact_id: parent if artifact_id == 'parent-artifact' else artifact
    direct_order = service._order_raw('work-1')
    service._order_raw = lambda order_id: SimpleNamespace(work_order_id=order_id) if order_id == 'parent-order' else direct_order
    service._order = lambda _ctx, order_id, _capability: service._order_raw(order_id)

    def validate(_ctx, order, _snapshot):
        if order.work_order_id == 'parent-order':
            raise PermissionError('inherited_source_revoked')

    service.source_snapshot_validator = validate
    with pytest.raises(PermissionError, match='inherited_source_revoked'):
        getattr(service, operation)(ctx, 'artifact-1')
