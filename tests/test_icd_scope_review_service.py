import hashlib
from unittest.mock import Mock

import pytest

from src.document_authoring.icd_scope_decision import (
    IcdScopeDecision,
    IcdScopeException,
)
from src.document_authoring.models import (
    DocumentArtifact,
    DocumentWorkOrder,
    KnowledgeBaseSourceSnapshot,
    ValidationReport,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext


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


def test_scope_review_rejects_source_outside_work_order_snapshot(scope_review_service):
    service, ctx, order = scope_review_service
    decision = decision_with_one_exception()
    decision.exceptions[0].source_names = ["foreign-source.pdf"]

    with pytest.raises(ValueError, match="source names.*frozen work order snapshot"):
        service.prepare_icd_scope_review(ctx, order.work_order_id, decision)


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
