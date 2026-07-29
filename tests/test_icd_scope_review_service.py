import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.document_authoring.icd_scope_decision import (
    IcdScopeDecision,
    IcdScopeException,
    build_icd_scope_decision,
)
from src.document_authoring.models import (
    DocumentArtifact,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    IcdScopeResolution,
    IcdScopeReview,
    KnowledgeBaseSourceSnapshot,
    ValidationReport,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext
from src.projects.models import (
    LogicalDocument,
    SourceAsset,
    SourceSetSnapshot,
    SourceVersion,
)
from src.projects.service import ProjectService
from src.projects.store import ProjectStore


def test_scope_resolution_rejects_actions_other_than_include_or_exclude():
    with pytest.raises(ValueError, match="include or exclude"):
        IcdScopeResolution(
            exception_id="exception-pgnd",
            action="mark_pending",
            actor_id="alice",
        )


def test_scope_resolution_api_rejects_unknown_action(scope_review_service):
    service, ctx, order = scope_review_service
    review = service.prepare_icd_scope_review(
        ctx, order.work_order_id, decision_with_one_exception()
    )

    with pytest.raises(ValueError, match="include or exclude"):
        service.submit_icd_scope_resolution(
            ctx,
            order.work_order_id,
            resolutions=[{
                "exception_id": review.exceptions[0].exception_id,
                "action": "mark_pending",
            }],
            comment="need a real choice",
        )


def _work_order() -> DocumentWorkOrder:
    return DocumentWorkOrder(
        work_order_id="work-1",
        tenant_id="tenant-1",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id="snapshot-1",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        template_schema_id="template-schema-1",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="internal_harness",
        harness_policy_id="policy-1",
        harness_policy_version="1",
        created_by="alice",
    )


@pytest.fixture
def scope_review_service(tmp_path):
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"), str(tmp_path / "artifacts")
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot-1",
        tenant_id="tenant-1",
        knowledge_base_name="hardware",
        source_names=["board.edf"],
        created_by="alice",
    )
    store.create_knowledge_base_source_snapshot(snapshot)
    order = store.create_work_order(_work_order())
    service = DocumentGenerationService(store=store)
    ctx = RequestContext(
        user_id="alice",
        tenant_id="tenant-1",
        metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "read"},
    )
    return service, ctx, order


def decision_with_one_exception() -> IcdScopeDecision:
    return IcdScopeDecision(
        frozen_pin_mappings=[{"refdes": "J7", "pin_name": "3", "net_name": "PGND"}],
        exceptions=[
            IcdScopeException(
                exception_id="exception-pgnd",
                kind="extra_pin_exposure",
                refdes="J7",
                pin_name="3",
                net_name="PGND",
                recommended_action="mark_pending",
                user_instruction="确认该脚是否需要在对外 ICD 中暴露。",
            )
        ],
    )


def test_scope_exceptions_are_resolved_in_one_batch_and_frozen(scope_review_service):
    service, ctx, order = scope_review_service

    review = service.prepare_icd_scope_review(
        ctx, order.work_order_id, decision_with_one_exception()
    )
    frozen = service.submit_icd_scope_resolution(
        ctx,
        order.work_order_id,
        resolutions=[
            {"exception_id": review.exceptions[0].exception_id, "action": "exclude"}
        ],
        comment="PGND 不进入线束 ICD",
    )

    assert frozen.status == "frozen"
    assert service.get_icd_scope_review(ctx, order.work_order_id).pending_count == 0
    with pytest.raises(ValueError, match="already frozen"):
        service.submit_icd_scope_resolution(
            ctx,
            order.work_order_id,
            resolutions=[{"exception_id": "exception-pgnd", "action": "exclude"}],
            comment="retry",
        )


def test_scope_retry_reuses_frozen_review_for_identical_evidence(scope_review_service):
    service, ctx, order = scope_review_service
    circuit_evidence = SimpleNamespace(
        source_name="board.edf",
        content="",
        locator={"entity_id": "J7"},
        metadata={
            "pin_mappings": [
                {"refdes": "J7", "pin_name": "3", "net_name": "PGND"}
            ]
        },
    )

    review = service.prepare_icd_scope_review(
        ctx,
        order.work_order_id,
        build_icd_scope_decision([circuit_evidence], []),
    )
    service.submit_icd_scope_resolution(
        ctx,
        order.work_order_id,
        resolutions=[
            {"exception_id": review.exceptions[0].exception_id, "action": "exclude"}
        ],
        comment="PGND 不进入线束 ICD",
    )

    retried = service.prepare_icd_scope_review(
        ctx,
        order.work_order_id,
        build_icd_scope_decision([circuit_evidence], []),
    )

    assert retried.status == "frozen"
    assert retried.resolutions[0].action == "exclude"


def test_scope_resolution_rejects_duplicate_exception_ids(scope_review_service):
    service, ctx, order = scope_review_service
    review = service.prepare_icd_scope_review(
        ctx, order.work_order_id, decision_with_one_exception()
    )

    with pytest.raises(ValueError, match="exactly once"):
        service.submit_icd_scope_resolution(
            ctx,
            order.work_order_id,
            resolutions=[
                {"exception_id": review.exceptions[0].exception_id, "action": "exclude"},
                {"exception_id": review.exceptions[0].exception_id, "action": "exclude"},
            ],
            comment="PGND 不进入线束 ICD",
        )


def test_scope_review_rejects_duplicate_decision_exception_ids():
    exception = decision_with_one_exception().exceptions[0]
    decision = IcdScopeDecision(exceptions=[exception, exception.model_copy()])

    with pytest.raises(ValueError, match="exception ids must be unique"):
        IcdScopeReview(
            work_order_id="work-1",
            decision=decision,
            source_snapshot_hash="snapshot-hash",
        )


def test_scope_review_rejects_source_outside_work_order_snapshot(scope_review_service):
    service, ctx, order = scope_review_service
    decision = decision_with_one_exception()
    decision.exceptions[0].source_names = ["foreign-source.pdf"]

    with pytest.raises(ValueError, match="source names.*frozen work order snapshot"):
        service.prepare_icd_scope_review(ctx, order.work_order_id, decision)


def test_scope_review_accepts_knowledge_base_source_names(scope_review_service):
    service, ctx, order = scope_review_service
    decision = decision_with_one_exception()
    decision.exceptions[0].source_names = ["board.edf"]

    review = service.prepare_icd_scope_review(ctx, order.work_order_id, decision)

    assert review.status == "pending"


def test_scope_review_accepts_project_document_titles(tmp_path):
    project_store = ProjectStore(str(tmp_path / "projects.db"))
    asset = project_store.create_source_asset(SourceAsset(
        asset_id="asset-1",
        tenant_id="tenant-1",
        original_file_name="schematic.edf",
        content_hash="asset-hash",
        content_kind="circuit_design",
        parser_kind="edf",
        processing_status="ready",
    ))
    document = project_store.create_logical_document(LogicalDocument(
        document_id="document-1",
        tenant_id="tenant-1",
        title="Main schematic",
        document_role="schematic",
        owner_department_id="hw",
    ))
    project_store.create_source_version(SourceVersion(
        version_id="version-1",
        tenant_id="tenant-1",
        document_id=document.document_id,
        asset_id=asset.asset_id,
        approval_status="released",
    ))
    service = DocumentGenerationService(project_service=ProjectService(project_store))
    snapshot = SourceSetSnapshot(
        source_set_snapshot_id="snapshot-1",
        tenant_id="tenant-1",
        work_order_id="work-1",
        project_id="project-1",
        baseline_id="baseline-1",
        baseline_content_hash="baseline-hash",
        baseline_item_ids=["baseline-item-1"],
        source_version_ids=["version-1"],
        authorization_snapshot_id="authorization-1",
    )
    decision = decision_with_one_exception()
    decision.exceptions[0].source_names = ["Main schematic"]

    service._validate_icd_scope_decision_sources(decision, snapshot)


@pytest.mark.parametrize(
    ("review_kind", "error_match"),
    [
        ("stale", "source snapshot differs"),
        ("pending", "unresolved ICD scope exceptions"),
    ],
)
def test_resume_blocks_stale_or_pending_scope_review_before_retry_queueing(
    scope_review_service,
    monkeypatch,
    review_kind,
    error_match,
):
    service, ctx, order = scope_review_service
    snapshot = service.resolve_source_snapshot(order)
    if review_kind == "stale":
        review = IcdScopeReview(
            work_order_id=order.work_order_id,
            decision=IcdScopeDecision(),
            source_snapshot_hash="stale-snapshot-hash",
            status="frozen",
        )
    else:
        review = IcdScopeReview(
            work_order_id=order.work_order_id,
            decision=decision_with_one_exception(),
            source_snapshot_hash=snapshot.content_hash,
        )
    service.register_harness_policy(HarnessPolicy(
        harness_policy_id="policy-1",
        version="1",
        status="approved",
    ))
    run = service.store.create_harness_run(HarnessRun(
        harness_run_id=f"run-{review_kind}",
        work_order_id=order.work_order_id,
        run_manifest_id="manifest-1",
        status="paused",
    ))
    queue_retry = Mock(return_value=run.model_copy(update={"status": "retrying"}))
    monkeypatch.setattr(service.store, "get_icd_scope_review", Mock(return_value=review))
    monkeypatch.setattr(service.store, "queue_harness_retry", queue_retry)

    with pytest.raises(ValueError, match=error_match):
        service.resume_internal_harness(ctx, run.harness_run_id, retrieve=Mock())

    queue_retry.assert_not_called()


def test_unresolved_scope_review_blocks_harness_execution(scope_review_service):
    service, ctx, order = scope_review_service
    service.prepare_icd_scope_review(ctx, order.work_order_id, decision_with_one_exception())

    with pytest.raises(ValueError, match="unresolved ICD scope exceptions"):
        service.run_internal_harness(ctx, order.work_order_id, retrieve=Mock())


def test_feedback_cannot_change_frozen_scope_review(scope_review_service):
    service, ctx, order = scope_review_service
    review = service.prepare_icd_scope_review(
        ctx, order.work_order_id, decision_with_one_exception()
    )
    service.submit_icd_scope_resolution(
        ctx,
        order.work_order_id,
        resolutions=[
            {"exception_id": review.exceptions[0].exception_id, "action": "exclude"}
        ],
        comment="PGND 不进入线束 ICD",
    )
    report = service.store.save_validation_report(ValidationReport(
        validation_report_id="report-1",
        work_order_id=order.work_order_id,
        status="passed",
        evidence_matrix_hash="matrix-hash",
    ))
    artifact_content = b"candidate"
    artifact = service.store.save_artifact(DocumentArtifact(
        artifact_id="candidate-1",
        tenant_id=order.tenant_id,
        work_order_id=order.work_order_id,
        run_id="run-1",
        stage="review_candidate",
        content_hash=hashlib.sha256(artifact_content).hexdigest(),
        validation_report_id=report.validation_report_id,
        integrity_manifest_id="manifest-1",
    ), artifact_content, "xlsx")

    event = service.submit_document_feedback(
        ctx, artifact.artifact_id, comment="please change PGND"
    )

    assert service.get_icd_scope_review(ctx, order.work_order_id).status == "frozen"
    assert service.store.list_human_events(artifact.artifact_id) == [event]

    denied_ctx = RequestContext(
        user_id="mallory",
        tenant_id="tenant-1",
        metadata={"department_id": "hw"},
        kb_permissions={},
    )
    with pytest.raises(PermissionError, match="knowledge base access"):
        service.submit_document_feedback(
            denied_ctx, artifact.artifact_id, comment="change PGND"
        )
