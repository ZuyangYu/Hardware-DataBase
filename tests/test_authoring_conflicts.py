"""R3 T9.5-A/B: normalized claims and deterministic conflict policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.document_authoring.conflicts.detector import ConflictDetector
from src.document_authoring.conflicts.extractor import GroundedClaimExtractor
from src.document_authoring.conflicts.normalizer import normalize_claim
from src.document_authoring.conflicts.policy import ConflictPolicy
from src.document_authoring.conflicts.store import ConflictDecisionStore
from src.document_authoring.work_order_store import DocumentAuthoringStore


def _claim(
    claim_id: str,
    value: str,
    *,
    unit: str,
    role: str,
    authoritative: bool | None = None,
    approval_status: str = "",
    scope_key: str = "target-A",
):
    return normalize_claim(
        claim_id=claim_id,
        field_key="input_voltage",
        subject="power module",
        predicate="requires",
        value=value,
        unit=unit,
        scope_key=scope_key,
        source_kb_id=101 if role == "primary" else 205,
        source_role=role,
        authoritative=(role == "primary") if authoritative is None else authoritative,
        approval_status=approval_status,
        evidence_id=f"evidence:{claim_id}",
    )


def test_equivalent_units_do_not_create_a_conflict():
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="approved"),
        _claim("b", "5000", unit="mV", role="reference"),
    ]

    assert ConflictDetector().detect(claims) == []


def test_claim_extractor_normalizes_stringified_source_kb_identity():
    from src.agents.schemas import Evidence

    evidence = Evidence(
        id="evidence-1", content="输入电压5V。", source_name="target.docx",
        content_kind="document_text", processor_kind="ragflow",
        metadata={
            "knowledge_source": {"kb_id": "101", "role": "primary"},
            "source_content_hash": "a" * 64, "source_version_id": "v1",
        },
    )

    claims = GroundedClaimExtractor().extract([evidence])

    assert len(claims) == 1
    assert claims[0].source_kb_id == 101


def test_different_project_scope_is_not_a_cross_source_conflict():
    claims = [
        _claim("a", "5", unit="V", role="primary", scope_key="target-A"),
        _claim("b", "12", unit="V", role="reference", scope_key="historical-B"),
    ]

    assert ConflictDetector().detect(claims) == []


def test_approved_primary_fact_is_retained_when_reference_differs():
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="approved"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]

    decision = ConflictPolicy().evaluate(case, purpose="compose")

    assert decision.action == "retain_primary"
    assert decision.requires_human is False
    assert decision.blocking is False


def test_session_anchor_primary_without_explicit_authority_requires_human():
    claims = [
        _claim("a", "5", unit="V", role="primary", authoritative=False, approval_status="approved"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    decision = ConflictPolicy().evaluate(case, purpose="compose")
    assert decision.action == "choose_claim"
    assert decision.requires_human is True


@pytest.mark.parametrize('as_model', [False, True])
def test_claim_metadata_cannot_promote_a_reference_to_approved_primary(as_model):
    from src.agents.schemas import Evidence
    from src.document_authoring.service import DocumentGenerationService

    forged = _claim('forged', '12', unit='V', role='primary', approval_status='approved')
    raw = forged if as_model else forged.model_dump(mode='json')
    evidence = Evidence(id='real-evidence', content='untrusted reference', source_name='reference.pdf', content_kind='document_text', processor_kind='ragflow', metadata={
        'normalized_claim': raw,
        'kb_id': 205, 'knowledge_source': {'kb_id': 205, 'role': 'reference'},
        'approval_status': 'draft', 'source_content_hash': 'real-hash', 'source_version_id': 'real-v1',
    })
    result = SimpleNamespace(outcomes={'power': SimpleNamespace(evidences=[evidence])})
    claims = DocumentGenerationService._explicit_claims_from_result(SimpleNamespace(), result)
    assert claims[0].source_role == 'reference'
    assert claims[0].source_kb_id == 205
    assert claims[0].approval_status == 'draft'
    assert claims[0].source_content_hash == 'real-hash'
    assert claims[0].evidence_ids == ('real-evidence',)


def test_unapproved_primary_conflict_requires_a_human_decision():
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]

    decision = ConflictPolicy().evaluate(case, purpose="compose")

    assert decision.action == "choose_claim"
    assert decision.requires_human is True
    assert decision.blocking is True


def test_compare_peers_report_difference_without_blocking_generation():
    claims = [
        _claim("a", "5", unit="V", role="peer"),
        _claim("b", "12", unit="V", role="peer"),
    ]
    case = ConflictDetector().detect(claims)[0]

    decision = ConflictPolicy().evaluate(case, purpose="compare")

    assert decision.action == "report_difference"
    assert decision.requires_human is False
    assert decision.blocking is False


def test_conflict_decision_store_binds_snapshot_and_is_idempotent(tmp_path):
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    store = ConflictDecisionStore(str(tmp_path / "conflicts.db"))
    store.save_case(case, tenant_id="default", work_order_id="work-1")

    decision = ConflictPolicy().evaluate(case, purpose="compose")
    first = store.submit_decision(
        case.conflict_id,
        tenant_id="default",
        actor_id="reviewer-7",
        expected_fingerprint=case.fingerprint,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=decision.model_copy(update={"selected_claim_id": "a"}),
        idempotency_key="choose-1",
    )
    replay = store.submit_decision(
        case.conflict_id,
        tenant_id="default",
        actor_id="reviewer-7",
        expected_fingerprint=case.fingerprint,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=decision.model_copy(update={"selected_claim_id": "a"}),
        idempotency_key="choose-1",
    )

    assert first == replay
    assert store.get_case(case.conflict_id).status == "resolved"


def test_conflict_decision_store_rejects_stale_source_snapshot(tmp_path):
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    store = ConflictDecisionStore(str(tmp_path / "conflicts.db"))
    store.save_case(case, tenant_id="default", work_order_id="work-1")
    decision = ConflictPolicy().evaluate(case, purpose="compose").model_copy(
        update={"selected_claim_id": "a"}
    )

    import pytest

    with pytest.raises(ValueError, match="source_snapshot_changed"):
        store.submit_decision(
            case.conflict_id,
            tenant_id="default",
            actor_id="reviewer-7",
            expected_fingerprint=case.fingerprint,
            expected_snapshot_hash="changed",
            expected_evidence_set_hash=case.evidence_set_hash,
            decision=decision,
            idempotency_key="choose-2",
        )


def test_design_candidate_does_not_satisfy_formal_release_approval(tmp_path):
    from src.document_authoring.conflicts.models import ConflictDecision

    case = ConflictDetector().detect([
        _claim('a', '5', unit='V', role='primary'),
        _claim('b', '12', unit='V', role='reference'),
    ])[0]
    store = ConflictDecisionStore(str(tmp_path / 'conflicts.db'))
    store.save_case(case, tenant_id='default', work_order_id='work-1')
    result = store.submit_decision(
        case.conflict_id, tenant_id='default', actor_id='alice',
        expected_fingerprint=case.fingerprint, expected_version=1,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=ConflictDecision(
            conflict_id=case.conflict_id, action='choose_design_candidate',
            selected_claim_id='b', requires_human=True, blocking=True,
        ), idempotency_key='candidate-1',
    )
    assert result['status'] == 'approval_pending'
    assert store.list_cases(tenant_id='default', work_order_id='work-1')[0].status == 'approval_pending'
    assert store.get_case(case.conflict_id).claims[0].display_value == case.claims[0].display_value


def test_idempotent_decision_replay_still_checks_tenant(tmp_path):
    case = ConflictDetector().detect([
        _claim('a', '5', unit='V', role='primary'),
        _claim('b', '12', unit='V', role='reference'),
    ])[0]
    store = ConflictDecisionStore(str(tmp_path / 'conflicts.db'))
    store.save_case(case, tenant_id='default', work_order_id='work-1')
    body = dict(
        conflict_id=case.conflict_id, tenant_id='default', actor_id='alice',
        expected_fingerprint=case.fingerprint, expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=ConflictPolicy().evaluate(case, purpose='compose').model_copy(update={'selected_claim_id': 'a'}),
        idempotency_key='same-key',
    )
    store.submit_decision(**body)
    with pytest.raises(PermissionError, match='tenant_mismatch'):
        store.submit_decision(**{**body, 'tenant_id': 'other-tenant'})


def test_decision_requires_write_access_to_the_actual_target_not_a_query_kb(tmp_path):
    from src.document_authoring.service import DocumentGenerationService
    from src.pipelines.document_rag.schemas import RequestContext

    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / 'authoring.db'), str(tmp_path / 'artifacts')))
    case = ConflictDetector().detect([
        _claim('a', '5', unit='V', role='primary'), _claim('b', '12', unit='V', role='reference'),
    ])[0]
    service.conflict_store.save_case(case, tenant_id='default', work_order_id='work-1')
    service._order = lambda *_args: SimpleNamespace(work_order_id='work-1', tenant_id='default', scope_type='knowledge_base', knowledge_base_name='target')
    service.resolve_source_snapshot = lambda _order: SimpleNamespace(content_hash=case.snapshot_hash)
    ctx = RequestContext(user_id='alice', tenant_id='default', metadata={'resource_department_id': '7'}, kb_permissions={'7:target': 'read', '7:other': 'write'})
    with pytest.raises(PermissionError, match='write permission'):
        service.submit_conflict_decision(
            ctx, case.conflict_id, expected_fingerprint=case.fingerprint,
            expected_snapshot_hash=case.snapshot_hash, expected_evidence_set_hash=case.evidence_set_hash,
            decision=ConflictPolicy().evaluate(case, purpose='compose').model_copy(update={'selected_claim_id': 'a'}),
            idempotency_key='wrong-target-write',
        )
    assert service.conflict_store.list_decisions(case.conflict_id) == []


def test_document_generation_service_exposes_conflict_store_on_authoring_db(tmp_path):
    from src.document_authoring.service import DocumentGenerationService

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)

    assert isinstance(service.conflict_store, ConflictDecisionStore)


def test_artifact_audit_exports_frozen_sources_decisions_and_release_blockers(tmp_path):
    from src.document_authoring.service import DocumentGenerationService
    from src.document_authoring.models import KnowledgeBaseSourceSnapshot
    from src.pipelines.document_rag.schemas import RequestContext

    store = DocumentAuthoringStore(str(tmp_path / 'authoring.db'), str(tmp_path / 'artifacts'))
    service = DocumentGenerationService(store=store)
    snapshot = KnowledgeBaseSourceSnapshot.create(tenant_id='default', knowledge_base_name='A', source_names=['spec.pdf'], created_by='alice')
    case = ConflictDetector().detect([
        _claim('a', '5', unit='V', role='primary'), _claim('b', '12', unit='V', role='reference'),
    ])[0].model_copy(update={'snapshot_hash': snapshot.content_hash})
    service.conflict_store.save_case(case, tenant_id='default', work_order_id='work-1')
    order = SimpleNamespace(work_order_id='work-1', tenant_id='default', status='waiting_human_approval', kb_scope_snapshot={'purpose': 'compose'})
    service._order = lambda *_args: order
    service._artifact_for_context = lambda *_args: SimpleNamespace(artifact_id='artifact-1', work_order_id='work-1', content_hash='artifact-hash', stage='review_candidate')
    service.resolve_source_snapshot = lambda _order: snapshot
    checks = []
    service.source_snapshot_validator = lambda *_args: checks.append(True)
    ctx = RequestContext(user_id='alice', tenant_id='default')
    result = service.document_artifact_audit(ctx, 'artifact-1')
    assert result['release_blocked'] is True
    assert result['source_snapshot_hash'] == snapshot.content_hash
    assert result['artifact_content_hash'] == 'artifact-hash'
    assert result['conflicts'][0]['conflict_id'] == case.conflict_id
    assert checks == [True]
    service.conflict_store.submit_decision(
        case.conflict_id, tenant_id='default', actor_id='alice',
        expected_fingerprint=case.fingerprint, expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=ConflictPolicy().evaluate(case, purpose='compose').model_copy(update={'selected_claim_id': 'a'}),
        idempotency_key='export-1',
    )
    result = service.document_artifact_audit(ctx, 'artifact-1')
    assert result['release_blocked'] is False
    assert result['conflicts'][0]['decisions'][0]['decision']['selected_claim_id'] == 'a'

    def revoked(*_args):
        raise PermissionError('source_unavailable')

    service.source_snapshot_validator = revoked
    with pytest.raises(PermissionError):
        service.document_artifact_audit(ctx, 'artifact-1')


def test_unresolved_conflict_blocks_automatic_publication_even_with_a_passed_report(tmp_path, monkeypatch):
    from src.document_authoring.service import DocumentGenerationService
    from src.document_authoring.models import DocumentArtifact
    import hashlib
    import src.settings

    service = DocumentGenerationService(store=DocumentAuthoringStore(str(tmp_path / 'authoring.db'), str(tmp_path / 'artifacts')))
    case = ConflictDetector().detect([
        _claim('a', '5', unit='V', role='primary'), _claim('b', '12', unit='V', role='reference'),
    ])[0]
    service.conflict_store.save_case(case, tenant_id='default', work_order_id='work-1')
    monkeypatch.setattr(src.settings, 'DOCUMENT_AUTO_PUBLISH_VERIFIED', True)
    candidate = DocumentArtifact(artifact_id='candidate-1', work_order_id='work-1', run_id='run-1', stage='review_candidate',
                                 content_hash=hashlib.sha256(b'candidate').hexdigest(), validation_report_id='validation', integrity_manifest_id='manifest')
    service.resolve_source_snapshot = lambda _order: SimpleNamespace(content_hash='snapshot')
    service._save_artifact_for_task = lambda _order, artifact, *_args: artifact
    service._replace_order = lambda *_args, **_kwargs: None
    result = service._auto_publish_verified_candidate(
        SimpleNamespace(work_order_id='work-1', tenant_id='default', target_format='xlsx'), candidate,
        SimpleNamespace(status='passed', issues=[], content_hash='report-hash'), b'candidate',
        unit_statuses={}, evidence_matrix_id='matrix', validation_report_id='validation',
    )
    assert result is candidate


def test_document_generation_service_lists_conflicts_by_work_order(tmp_path):
    from src.document_authoring.service import DocumentGenerationService
    from src.pipelines.document_rag.schemas import RequestContext

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    service.conflict_store.save_case(case, tenant_id="default", work_order_id="work-1")
    service._order = lambda *_args, **_kwargs: SimpleNamespace(
        work_order_id="work-1", tenant_id="default"
    )
    service.resolve_source_snapshot = lambda _order: SimpleNamespace(content_hash="snapshot-hash")

    result = service.list_conflict_cases(
        RequestContext(user_id="alice", tenant_id="default"),
        "work-1",
    )

    assert result[0]["conflict_id"] == case.conflict_id
    assert result[0]["status"] == "open"


def test_conflict_store_deduplicates_same_work_order_fingerprint(tmp_path):
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    first = ConflictDetector().detect(claims)[0]
    second = first.model_copy(update={"conflict_id": "conflict-retry"})
    store = ConflictDecisionStore(str(tmp_path / "conflicts.db"))

    saved = store.save_case(first, tenant_id="default", work_order_id="work-1")
    replay = store.save_case(second, tenant_id="default", work_order_id="work-1")

    assert saved.conflict_id == replay.conflict_id == first.conflict_id
    assert len(store.list_cases(tenant_id="default", work_order_id="work-1")) == 1


def test_conflict_store_rejects_different_payload_for_same_idempotency_key(tmp_path):
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    store = ConflictDecisionStore(str(tmp_path / "conflicts.db"))
    store.save_case(case, tenant_id="default", work_order_id="work-1")
    decision = ConflictPolicy().evaluate(case, purpose="compose").model_copy(
        update={"selected_claim_id": "a"}
    )
    store.submit_decision(
        case.conflict_id,
        tenant_id="default",
        actor_id="reviewer-7",
        expected_fingerprint=case.fingerprint,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=decision,
        idempotency_key="choose-1",
    )

    with pytest.raises(ValueError, match="idempotency_conflict"):
        store.submit_decision(
            case.conflict_id,
            tenant_id="default",
            actor_id="reviewer-8",
            expected_fingerprint=case.fingerprint,
            expected_snapshot_hash=case.snapshot_hash,
            expected_evidence_set_hash=case.evidence_set_hash,
            decision=decision.model_copy(update={"rationale": "different"}),
            idempotency_key="choose-1",
        )


def test_conflict_store_can_share_authoring_backend_connection(tmp_path):
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    conflict_store = ConflictDecisionStore(backend_store=store)
    assert conflict_store.db_path == store.db_path
    with store._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authoring_conflict_cases'"
        ).fetchone() is not None


def test_existing_conflict_tables_upgrade_before_new_indexes_are_created(tmp_path):
    import sqlite3

    path = str(tmp_path / 'legacy-authoring.db')
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE authoring_conflict_cases (
                conflict_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, work_order_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE authoring_conflict_decisions (
                decision_id TEXT PRIMARY KEY, conflict_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(conflict_id, idempotency_key)
            );
        ''')
        original = ConflictDetector().detect([
            _claim('a', '5', unit='V', role='primary'), _claim('b', '12', unit='V', role='reference'),
        ])[0]
        for suffix in ('a', 'b'):
            case = original.model_copy(update={'conflict_id': f'legacy-{suffix}', 'snapshot_hash': f'snapshot-{suffix}'})
            conn.execute('INSERT INTO authoring_conflict_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (
                case.conflict_id, 'default', 'work-legacy', case.fingerprint, case.status,
                case.model_dump_json(), '2026-10-15', '2026-10-15',
            ))
    for _ in range(2):
        backend = DocumentAuthoringStore(path, str(tmp_path / 'artifacts'))
        conflicts = ConflictDecisionStore(backend_store=backend)
        with conflicts._connect() as conn:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(authoring_conflict_cases)')}
        assert {'source_snapshot_hash', 'evidence_set_hash', 'row_version'} <= columns
        assert len(conflicts.list_cases(tenant_id='default', work_order_id='work-legacy')) == 2


def test_service_persists_explicit_evidence_claim_conflicts_and_blocks_unit(tmp_path):
    from src.agents.claim_evidence import RetrievalOutcome
    from src.agents.schemas import Evidence
    from src.document_authoring.harness.graph import HarnessExecutionResult
    from src.document_authoring.service import DocumentGenerationService

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)
    order = SimpleNamespace(
        work_order_id="work-claims",
        tenant_id="default",
        kb_scope_snapshot={"purpose": "compose"},
    )
    snapshot = SimpleNamespace(content_hash="snapshot-hash")
    result = HarnessExecutionResult(
        outcomes={
            "field:input_voltage": RetrievalOutcome(
                requirement_id="req-voltage",
                status="success_with_hits",
                query_fingerprint="q",
                applied_source_set_snapshot_id="snapshot-id",
                evidences=[
                    Evidence(
                        id="e-a",
                        content="input 5V",
                        source_name="a.pdf",
                        content_kind="document",
                        processor_kind="ragflow",
                        metadata={
                            "normalized_claim": {
                                "claim_id": "claim-a",
                                "field_key": "input_voltage",
                                "subject": "power module",
                                "predicate": "requires",
                                "value": "5V",
                                "source_kb_id": 101,
                                "source_role": "primary",
                                "approval_status": "draft",
                                "source_content_hash": "a" * 64,
                            }
                        },
                    ),
                    Evidence(
                        id="e-b",
                        content="input 12V",
                        source_name="b.pdf",
                        content_kind="document",
                        processor_kind="ragflow",
                        metadata={
                            "normalized_claim": {
                                "claim_id": "claim-b",
                                "field_key": "input_voltage",
                                "subject": "power module",
                                "predicate": "requires",
                                "value": "12V",
                                "source_kb_id": 205,
                                "source_role": "reference",
                                "source_content_hash": "b" * 64,
                            }
                        },
                    ),
                ],
            )
        },
        unit_statuses={"field:input_voltage": "ready_to_render"},
    )

    cases = service._detect_and_persist_conflicts(order, snapshot, result)

    assert len(cases) == 1
    assert cases[0].status == "open"
    assert result.unit_statuses["field:input_voltage"] == "conflicting"
    assert result.issues[0]["code"] == "authoring_conflict_requires_human"


def test_multi_source_work_order_requires_read_access_to_each_source(tmp_path):
    from src.document_authoring.service import DocumentGenerationService
    from src.pipelines.document_rag.schemas import RequestContext

    service = DocumentGenerationService(
        store=DocumentAuthoringStore(
            str(tmp_path / "authoring.db"),
            str(tmp_path / "artifacts"),
        )
    )
    ctx = RequestContext(
        user_id="alice",
        tenant_id="default",
        metadata={"resource_department_id": "dept-a"},
        kb_permissions={"dept-a:primary": "read"},
    )
    order = SimpleNamespace(
        scope_type="knowledge_base",
        knowledge_base_name="primary",
        resource_department_id="dept-a",
        kb_scope_snapshot={
            "sources": [
                {"kb_name": "primary", "department_id": "dept-a"},
                {"kb_name": "reference", "department_id": "dept-a"},
            ]
        },
    )

    with pytest.raises(PermissionError, match="knowledge base access"):
        service.require_work_order_capability(ctx, order, "run_deterministic_work_order")


def test_source_snapshot_validator_is_called_at_execution_boundary(tmp_path):
    from src.document_authoring.service import DocumentGenerationService
    from src.pipelines.document_rag.schemas import RequestContext

    service = DocumentGenerationService(
        store=DocumentAuthoringStore(
            str(tmp_path / "authoring.db"),
            str(tmp_path / "artifacts"),
        )
    )
    calls = []

    def validate(ctx, order, snapshot):
        calls.append((ctx.user_id, order.work_order_id, snapshot.content_hash))
        raise ValueError("source_changed")

    service.source_snapshot_validator = validate
    with pytest.raises(ValueError, match="source_changed"):
        service.validate_source_snapshot(
            RequestContext(user_id="alice"),
            SimpleNamespace(work_order_id="work-1"),
            SimpleNamespace(content_hash="hash-1"),
        )
    assert calls == [("alice", "work-1", "hash-1")]


def test_conflict_list_and_detail_recheck_frozen_sources(tmp_path):
    from src.document_authoring.service import DocumentGenerationService
    from src.pipelines.document_rag.schemas import RequestContext

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)
    claims = [
        _claim("a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("b", "12", unit="V", role="reference"),
    ]
    case = ConflictDetector().detect(claims)[0]
    service.conflict_store.save_case(case, tenant_id="default", work_order_id="work-1")
    order = SimpleNamespace(work_order_id="work-1", tenant_id="default")
    snapshot = SimpleNamespace(content_hash="snapshot-hash")
    service._order = lambda *_args, **_kwargs: order
    service.resolve_source_snapshot = lambda _order: snapshot
    calls = []
    service.source_snapshot_validator = lambda ctx, current_order, current_snapshot: calls.append(
        (ctx.user_id, current_order.work_order_id, current_snapshot.content_hash)
    )
    ctx = RequestContext(user_id="alice", tenant_id="default")

    service.list_conflict_cases(ctx, "work-1")
    service.get_conflict_case(ctx, case.conflict_id)

    assert calls == [
        ("alice", "work-1", "snapshot-hash"),
        ("alice", "work-1", "snapshot-hash"),
    ]


def test_conflict_detection_maps_normalized_claim_objects_to_affected_units(tmp_path):
    from src.agents.claim_evidence import RetrievalOutcome
    from src.agents.schemas import Evidence
    from src.document_authoring.harness.graph import HarnessExecutionResult
    from src.document_authoring.service import DocumentGenerationService

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)
    order = SimpleNamespace(
        work_order_id="work-objects",
        tenant_id="default",
        kb_scope_snapshot={"purpose": "compose"},
    )
    snapshot = SimpleNamespace(content_hash="snapshot-hash")
    claims = (
        _claim("claim-a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("claim-b", "12", unit="V", role="reference"),
    )
    result = HarnessExecutionResult(
        outcomes={
            "field:input_voltage": RetrievalOutcome(
                requirement_id="req-voltage",
                status="success_with_hits",
                query_fingerprint="q",
                applied_source_set_snapshot_id="snapshot-id",
                evidences=[
                    Evidence(
                        id="e-a",
                        content="input 5V",
                        source_name="a.pdf",
                        content_kind="document",
                        processor_kind="ragflow",
                        metadata={"normalized_claim": claims[0]},
                    ),
                    Evidence(
                        id="e-b",
                        content="input 12V",
                        source_name="b.pdf",
                        content_kind="document",
                        processor_kind="ragflow",
                        metadata={"normalized_claim": claims[1]},
                    ),
                ],
            )
        },
        unit_statuses={"field:input_voltage": "ready_to_render"},
    )

    cases = service._detect_and_persist_conflicts(order, snapshot, result)

    assert cases[0].affected_unit_ids == ("field:input_voltage",)
    assert result.unit_statuses["field:input_voltage"] == "conflicting"


def test_conflict_finalization_can_move_completed_run_to_waiting_human(tmp_path):
    from src.document_authoring.models import DocumentWorkOrder, HarnessRun
    from src.document_authoring.service import DocumentGenerationService

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)
    store.create_work_order(
        DocumentWorkOrder(
            work_order_id="work-objects",
            project_id="project-a",
            baseline_id="baseline-a",
            baseline_content_hash="baseline-hash",
            source_set_snapshot_id="snapshot-id",
            template_version_id="template-a",
            document_schema_id="schema-a",
            document_schema_version="1",
            template_schema_id="template-schema-a",
            template_schema_version="1",
            retrieval_policy_version="1",
            renderer_policy_version="1",
            target_format="xlsx",
            execution_mode="deterministic_only",
            created_by="alice",
        )
    )
    store.create_harness_run(
        HarnessRun(
            harness_run_id="harness-completed",
            work_order_id="work-objects",
            run_manifest_id="manifest-objects",
            status="completed",
        )
    )

    service._mark_harness_run_waiting_for_human("harness-completed")

    assert store.get_harness_run("harness-completed").status == "waiting_human"


def test_conflict_decision_is_written_to_authoring_execution_audit(tmp_path):
    from src.document_authoring.models import DocumentWorkOrder, HarnessRun
    from src.document_authoring.service import DocumentGenerationService
    from src.pipelines.document_rag.schemas import RequestContext

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)
    store.create_work_order(
        DocumentWorkOrder(
            work_order_id="work-audit",
            project_id="project-a",
            baseline_id="baseline-a",
            baseline_content_hash="baseline-hash",
            source_set_snapshot_id="snapshot-id",
            template_version_id="template-a",
            document_schema_id="schema-a",
            document_schema_version="1",
            template_schema_id="template-schema-a",
            template_schema_version="1",
            retrieval_policy_version="1",
            renderer_policy_version="1",
            target_format="xlsx",
            execution_mode="deterministic_only",
            created_by="alice",
        )
    )
    store.create_harness_run(
        HarnessRun(
            harness_run_id="harness-audit",
            work_order_id="work-audit",
            run_manifest_id="manifest-audit",
            status="waiting_human",
        )
    )
    case = ConflictDetector().detect([
        _claim("claim-a", "5", unit="V", role="primary", approval_status="draft"),
        _claim("claim-b", "12", unit="V", role="reference"),
    ])[0].model_copy(update={"snapshot_hash": "snapshot-hash"})
    service.conflict_store.save_case(case, tenant_id="default", work_order_id="work-audit")
    service._order = lambda *_args, **_kwargs: SimpleNamespace(
        work_order_id="work-audit", tenant_id="default"
    )
    service.resolve_source_snapshot = lambda _order: SimpleNamespace(content_hash="snapshot-hash")
    service.source_snapshot_validator = lambda *_args: None
    decision = ConflictPolicy().evaluate(case, purpose="compose").model_copy(
        update={"selected_claim_id": "claim-a"}
    )

    service.submit_conflict_decision(
        RequestContext(user_id="alice", tenant_id="default"),
        case.conflict_id,
        expected_fingerprint=case.fingerprint,
        expected_conflict_version=1,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        decision=decision,
        idempotency_key="audit-decision-1",
    )

    events = store.list_execution_events("harness-audit")
    assert len(events) == 1
    assert events[0].event_type == "conflict_decision"
    assert events[0].sanitized_payload["conflict_id"] == case.conflict_id


def test_waiting_human_harness_run_can_resume_without_consuming_retry_budget(tmp_path):
    from src.document_authoring.models import DocumentWorkOrder, HarnessRun

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    store.create_work_order(
        DocumentWorkOrder(
            work_order_id="work-claims",
            project_id="project-a",
            baseline_id="baseline-a",
            baseline_content_hash="baseline-hash",
            source_set_snapshot_id="snapshot-id",
            template_version_id="template-a",
            document_schema_id="schema-a",
            document_schema_version="1",
            template_schema_id="template-schema-a",
            template_schema_version="1",
            retrieval_policy_version="1",
            renderer_policy_version="1",
            target_format="xlsx",
            execution_mode="deterministic_only",
            created_by="alice",
        )
    )
    run = store.create_harness_run(
        HarnessRun(
            harness_run_id="harness-conflict",
            work_order_id="work-claims",
            run_manifest_id="manifest-conflict",
            status="waiting_human",
            retry_count=3,
            max_retries=3,
        )
    )

    resumed = store.queue_harness_retry(run.harness_run_id, max_retries=3)

    assert resumed.status == "retrying"
    assert resumed.retry_count == 3


def test_multi_kb_snapshot_recheck_rejects_changed_record():
    from src.core.app_pipeline import AppPipeline
    from src.pipelines.document_rag.schemas import RequestContext

    pipeline = AppPipeline.__new__(AppPipeline)
    pipeline._resolve_frozen_authoring_scope = lambda *_args: None
    pipeline.list_file_infos = lambda _kb, _ctx: [
        SimpleNamespace(
            name="spec.pdf",
            metadata={
                "store_id": 7,
                "dataset_id": "dataset-a",
                "ragflow_document_id": "remote-a",
                "content_hash": "changed-hash",
                "index_revision": 2,
                "processor_kind": "ragflow",
            },
        )
    ]
    ctx = RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        metadata={"resource_department_id": "dept-a"},
        kb_permissions={"dept-a:KB-A": "read"},
    )
    snapshot = SimpleNamespace(
        source_snapshot_kind="multi_knowledge_v1",
        knowledge_base_name="KB-A",
        sources=[{
            "tenant_key": "tenant-a",
            "department_id": "dept-a",
            "kb_id": 101,
            "kb_name_snapshot": "KB-A",
            "role": "primary",
            "local_record_id": 7,
            "source_name": "spec.pdf",
            "source_content_hash": "original-hash",
            "processor_kind": "ragflow",
            "dataset_id": "dataset-a",
            "remote_document_id": "remote-a",
            "index_revision": 2,
        }],
    )

    with pytest.raises(ValueError, match="source_changed"):
        pipeline._validate_authoring_source_snapshot(ctx, SimpleNamespace(), snapshot)
