"""Old workers cannot materialize an R3 plan through the submission outbox."""

import sqlite3
import uuid

import pytest
from psycopg.errors import CheckViolation

import src.settings
from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from src.document_authoring.planning.submissions import DocumentPlanSubmission, DocumentPlanSubmissionStore
from src.document_authoring.work_order_store import DocumentAuthoringStore


def check_submission_review_contract(backend, monkeypatch):
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id='contract-tenant', knowledge_base_name='target', source_names=[], created_by='alice',
        source_snapshot_kind='multi_knowledge_v1', sources=[{'kb_id': 101, 'role': 'primary'}, {'kb_id': 205, 'role': 'reference'}],
    )
    backend.create_knowledge_base_source_snapshot(snapshot)
    submissions = DocumentPlanSubmissionStore(backend.db_path)
    submission = DocumentPlanSubmission(
        submission_id=f'contract-submission-{uuid.uuid4().hex}', tenant_id='contract-tenant', user_id='alice',
        task_id='task-contract', session_id='session-contract', knowledge_base_name='target',
        output_spec_id='spec-contract', output_spec_version=1, output_spec_hash='spec-hash',
        document_plan_id=f'plan-contract-{uuid.uuid4().hex}', document_plan_version=1, document_plan_hash='plan-hash',
        source_snapshot_id=snapshot.source_set_snapshot_id, source_snapshot_hash=snapshot.content_hash,
        client_request_id=f'contract-request-{uuid.uuid4().hex}',
    )
    submissions.insert_or_get(submission)
    monkeypatch.setattr(src.settings, 'MULTI_KB_HUMAN_REVIEW_ENABLED', False, raising=False)
    assert all(item.submission_id != submission.submission_id for item in submissions.list_pending())
    assert submissions.claim(submission.submission_id, 'new-worker') is None
    assert submissions.get(submission.submission_id).attempt == 0
    with submissions._connect() as conn, pytest.raises((sqlite3.IntegrityError, CheckViolation), match='unsupported_worker_contract'):
        conn.execute("UPDATE document_plan_submissions SET status='running', lease_token=lease_token+1 WHERE submission_id=?", (submission.submission_id,))
    monkeypatch.setattr(src.settings, 'MULTI_KB_HUMAN_REVIEW_ENABLED', True)
    claimed = submissions.claim(submission.submission_id, 'new-worker')
    assert claimed is not None and claimed.attempt == 1
    with submissions._connect() as conn, pytest.raises((sqlite3.IntegrityError, CheckViolation), match='unsupported_worker_contract'):
        conn.execute('UPDATE document_plan_submissions SET lease_token=lease_token+1 WHERE submission_id=?', (submission.submission_id,))


def test_sqlite_submission_contract_stops_old_workers_and_rollback_without_attempts(tmp_path, monkeypatch):
    backend = DocumentAuthoringStore(str(tmp_path / 'authoring.db'), str(tmp_path / 'artifacts'))
    check_submission_review_contract(backend, monkeypatch)
