from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.app_pipeline import AppPipeline
from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from src.agents.claim_evidence import InformationRequirement
from src.pipelines.document_rag.schemas import RequestContext
from tests import test_knowledge_scope as scope_fixtures

auth_scope = scope_fixtures.auth_scope


def frozen_setup(auth_scope, monkeypatch):
    auth, _admin, actor, _owner, _ids = auth_scope
    sources = []
    for name, role, record_id in [('A', 'primary', 7), ('B', 'reference', 9)]:
        sources.append({
            'tenant_key': 'default', 'department_id': str(actor.department_id),
            'kb_id': auth.get_knowledge_base_id(name, department_id=actor.department_id),
            'kb_name_snapshot': name, 'role': role, 'local_record_id': record_id,
            'source_name': 'same.pdf', 'source_content_hash': f'hash-{name}',
            'remote_document_id': f'remote-{name}', 'dataset_id': f'dataset-{name}',
            'index_revision': 2, 'processor_kind': 'document_text',
            'source_version_id': 'v1', 'revision': 'r1', 'approval_status': 'approved',
        })
    frozen = KnowledgeBaseSourceSnapshot.create(
        tenant_id='default', knowledge_base_name='A', source_names=['same.pdf'],
        created_by=actor.username, source_snapshot_kind='multi_knowledge_v1', sources=sources,
    )
    pipeline = AppPipeline.__new__(AppPipeline)
    pipeline.document_generation = Mock()
    pipeline.document_generation.store.get_knowledge_base_source_snapshot.return_value = frozen
    pipeline.document_generation.build_knowledge_base_retrieval_outcome.return_value = SimpleNamespace(status='success_empty')
    pipeline.backend = Mock()
    pipeline.spreadsheet_service = None
    pipeline.circuit_service = None
    pipeline.list_file_infos = lambda name, _ctx: [SimpleNamespace(name='same.pdf', metadata={
        **next(source for source in sources if source['kb_name_snapshot'] == name),
        'store_id': 7 if name == 'A' else 9,
    })]
    monkeypatch.setattr('src.core.app_pipeline.AuthService', lambda: auth)
    ctx = RequestContext(
        user_id=actor.username, tenant_id='default',
        metadata={'resource_department_id': str(actor.department_id)},
        kb_permissions=auth.get_kb_permissions_for_user(actor, ['A', 'B']),
    )
    return pipeline, ctx, frozen


def test_worker_rebuilds_all_sources_and_frozen_record_contracts(auth_scope, monkeypatch):
    pipeline, ctx, frozen = frozen_setup(auth_scope, monkeypatch)
    searches = []

    def search(_self, **kwargs):
        searches.append(kwargs)
        return SimpleNamespace(evidence=[], source_outcomes=[], partial=False)

    monkeypatch.setattr('src.services.multi_kb_retrieval.MultiKnowledgeRetrievalService.search', search)
    retrieve = pipeline._knowledge_base_retriever(ctx, 'A', ['same.pdf'], source_set_snapshot_id=frozen.source_set_snapshot_id)
    requirement = InformationRequirement(requirement_id='r1', semantic_unit_id='power', claim_type='attribute', subject='voltage')
    retrieve(requirement, 0)
    retrieve(requirement, 1, relaxed=True)
    assert len(searches) == 2
    assert {source.kb_name for source in searches[0]['snapshot'].sources} == {'A', 'B'}
    contracts = searches[0]['document_contracts']
    assert {record for contract in contracts.values() for record in contract.allowed_record_ids} == {7, 9}
    assert all(contract.mode == 'frozen' for contract in contracts.values())
    assert searches[0]['budget'] is searches[1]['budget']
    pipeline.backend.retrieve.assert_not_called()


@pytest.mark.parametrize('field', ['source_version_id', 'revision', 'approval_status', 'source_content_hash', 'index_revision'])
def test_freeze_recheck_detects_source_and_approval_changes(auth_scope, monkeypatch, field):
    pipeline, ctx, frozen = frozen_setup(auth_scope, monkeypatch)
    documents = pipeline.list_file_infos('A', ctx)
    documents[0].metadata[field] = 99 if field == 'index_revision' else 'changed'
    original = pipeline.list_file_infos
    pipeline.list_file_infos = lambda name, current: documents if name == 'A' else original(name, current)
    with pytest.raises(ValueError, match='source_changed'):
        pipeline._validate_authoring_source_snapshot(ctx, SimpleNamespace(), frozen)


def test_freeze_recheck_uses_live_acl_even_with_a_stale_request_context(auth_scope, monkeypatch):
    pipeline, ctx, frozen = frozen_setup(auth_scope, monkeypatch)
    auth, _admin, _actor, owner, _ids = auth_scope
    auth.set_knowledge_base_sharing('B', owner, False)
    with pytest.raises(PermissionError):
        pipeline._validate_authoring_source_snapshot(ctx, SimpleNamespace(), frozen)


def test_multi_kb_outcome_accepts_reference_evidence_by_record_identity(auth_scope, monkeypatch):
    from src.agents.schemas import Evidence
    from src.document_authoring.service import DocumentGenerationService

    _pipeline, _ctx, frozen = frozen_setup(auth_scope, monkeypatch)
    source = frozen.sources[1]
    evidence = Evidence(id='ref-chunk', content='reference', source_name='same.pdf', content_kind='document_text', processor_kind='ragflow', metadata={
        'kb_id': source['kb_id'], 'local_record_id': 9, 'kb_name': 'B',
        'source_content_hash': source['source_content_hash'],
    })
    outcome = DocumentGenerationService.build_knowledge_base_retrieval_outcome(
        'A', ['same.pdf'], [evidence], frozen_sources=frozen.sources,
    )
    assert outcome.evidences[0].metadata['knowledge_base_name'] == 'B'
    evidence.metadata['local_record_id'] = 999
    with pytest.raises(PermissionError):
        DocumentGenerationService.build_knowledge_base_retrieval_outcome(
            'A', ['same.pdf'], [evidence], frozen_sources=frozen.sources,
        )


def test_legacy_snapshot_hash_still_loads_without_multi_source_fields():
    from src.document_authoring.models import content_hash

    current = KnowledgeBaseSourceSnapshot.create(tenant_id='default', knowledge_base_name='A', source_names=['old.pdf'], created_by='alice')
    payload = current.model_dump(mode='json', exclude={'content_hash', 'source_snapshot_kind', 'sources', 'knowledge_scope_snapshot'})
    payload['content_hash'] = content_hash(payload)
    assert KnowledgeBaseSourceSnapshot.model_validate(payload).content_hash == payload['content_hash']
