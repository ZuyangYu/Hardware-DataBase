import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from src.agents.claim_evidence import InformationRequirement
from src.core.app_pipeline import AppPipeline
from src.document_authoring.icd_scope_decision import (
    IcdScopeDecision,
    IcdScopeException,
    IcdScopeItem,
)
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocumentWorkOrder,
    IcdScopeReview,
    KnowledgeBaseSourceSnapshot,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext


def fixture_pins():
    return [
        {"refdes": "X1900", "pin_name": str(pin), "net_name": f"NET_{pin}"}
        for pin in range(1, 20)
    ]


def fixture_fpt():
    return {f"X1900-{pin}" for pin in range(1, 19)}


def icd_template():
    return DocumentSchema(
        document_schema_id="icd", version="1", document_type="icd",
        execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id="pins", label="Connector pin definition",
            required_capabilities=["relationship_lookup"],
            retrieval_policy_id="retrieval-pins", verification_policy_id="verify-pins",
        )],
    )


def run_logged_in_kb_flow(*, edf_pins, fpt, template):
    pipeline = object.__new__(AppPipeline)
    ctx = RequestContext(
        user_id="alice", tenant_id="tenant-a", metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "read"},
    )
    order = SimpleNamespace(
        work_order_id="work-icd-1", document_schema_id=template.document_schema_id,
        document_schema_version=template.version,
    )
    review = IcdScopeReview(
        work_order_id=order.work_order_id,
        source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(
            auto_items=[IcdScopeItem(**pin) for pin in edf_pins if f"{pin['refdes']}-{pin['pin_name']}" in fpt],
            exceptions=[IcdScopeException(
                exception_id="extra-pin-exposure", kind="extra_pin_exposure", **edf_pins[-1],
                recommended_action="mark_pending", user_instruction="Confirm exposure.",
            )],
            frozen_pin_mappings=edf_pins,
        ),
    )
    service = Mock()
    service.create_knowledge_base_work_order.return_value = order
    service.resolve_source_snapshot.return_value = SimpleNamespace(
        source_set_snapshot_id="snapshot-1", source_names=["board.edf"],
    )
    service._schema.return_value = template
    service.prepare_icd_scope_review.return_value = review
    service.get_icd_scope_review.return_value = review
    service.submit_icd_scope_resolution.return_value = review.model_copy(update={
        "status": "frozen",
        "resolutions": [],
    })
    service.build_knowledge_base_retrieval_outcome.side_effect = (
        lambda _kb, _sources, evidences, **_kwargs: SimpleNamespace(evidences=evidences)
    )
    pipeline.document_generation = service
    pipeline.list_file_infos = Mock(return_value=[SimpleNamespace(name="board.edf")])
    pipeline.backend = Mock()
    pipeline.backend.retrieve.return_value = []
    pipeline.circuit_service = Mock()
    pipeline.circuit_service.list_pin_mapping_evidence.return_value = []
    pipeline.spreadsheet_service = None

    blocked = pipeline.auto_generate_knowledge_base_document(
        ctx, knowledge_base_name="hardware", template_version_id="template-1",
        document_schema_id="icd", document_schema_version="1",
    )
    assert blocked["stage"] == "scope_review_required"
    pending = pipeline.get_icd_scope_review(ctx, order.work_order_id)
    frozen = pipeline.submit_icd_scope_resolution(
        ctx, order.work_order_id,
        resolutions=[{"exception_id": "extra-pin-exposure", "action": "exclude"}],
        comment="PGND stays internal",
    )
    pipeline.circuit_service = None
    retrieve = pipeline._knowledge_base_retriever(
        ctx, "hardware", ["board.edf"], icd_scope_review=pending,
    )
    outcome = retrieve(InformationRequirement(
        requirement_id="pins", semantic_unit_id="pins", claim_type="relationship",
        subject="connector pins", required_capabilities=["relationship_lookup"],
    ), 0)
    candidate_pin_set = outcome.evidences[-1].metadata["pin_mappings"]
    return SimpleNamespace(
        auto_adopted_count=len(pending.decision.auto_items),
        exceptions=[exception.kind for exception in pending.exceptions],
        candidate_pin_set=candidate_pin_set,
        frozen_pin_set=pending.decision.frozen_pin_mappings,
        frozen=frozen,
    )


def test_logged_in_icd_flow_injects_frozen_pins_and_requires_only_pgnd_decision():
    result = run_logged_in_kb_flow(
        edf_pins=fixture_pins(), fpt=fixture_fpt(), template=icd_template(),
    )

    assert result.auto_adopted_count == 18
    assert result.exceptions == ["extra_pin_exposure"]
    assert result.candidate_pin_set == result.frozen_pin_set


def scope_decision_with_one_exception():
    return IcdScopeDecision(
        frozen_pin_mappings=[{"refdes": "J7", "pin_name": "3", "net_name": "PGND"}],
        exceptions=[IcdScopeException(
            exception_id="exception-pgnd", kind="extra_pin_exposure",
            refdes="J7", pin_name="3", net_name="PGND",
            recommended_action="mark_pending", user_instruction="Confirm exposure.",
        )],
    )


def scope_review_pipeline(tmp_path):
    """Real pipeline -> service -> store chain used by the icd-scope-resolution route.

    Route anchor: src/api/routes/document_generation.py:501-523 (icd_scope_resolution).
    """
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"), str(tmp_path / "artifacts")
    )
    store.create_knowledge_base_source_snapshot(KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot-1",
        tenant_id="tenant-1",
        knowledge_base_name="hardware",
        source_names=["board.edf"],
        created_by="alice",
    ))
    order = store.create_work_order(DocumentWorkOrder(
        work_order_id="work-icd-scope-1",
        tenant_id="tenant-1",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        resource_department_id="hw",
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
    ))
    service = DocumentGenerationService(store=store)
    ctx = RequestContext(
        user_id="alice", tenant_id="tenant-1", metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "write"},
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = service
    return pipeline, ctx, order


def prepare_pending_scope_review(pipeline, ctx, order):
    return pipeline.document_generation.prepare_icd_scope_review(
        ctx, order.work_order_id, scope_decision_with_one_exception()
    )


def test_scope_resolution_requires_an_explicit_include_or_exclude_before_queueing(tmp_path):
    pipeline, ctx, order = scope_review_pipeline(tmp_path)
    review = prepare_pending_scope_review(pipeline, ctx, order)
    job_store = Mock()
    pipeline.document_job_store = job_store

    with pytest.raises(ValueError, match="include or exclude"):
        pipeline.submit_icd_scope_resolution(
            ctx, order.work_order_id,
            resolutions=[{"exception_id": review.exceptions[0].exception_id, "action": ""}],
            comment="PGND stays internal",
        )

    job_store.create_job.assert_not_called()
    assert pipeline.get_icd_scope_review(ctx, order.work_order_id).status == "pending"


def test_frozen_scope_review_replays_recorded_resolutions_and_rejects_resubmission(tmp_path):
    pipeline, ctx, order = scope_review_pipeline(tmp_path)
    review = prepare_pending_scope_review(pipeline, ctx, order)

    frozen = pipeline.submit_icd_scope_resolution(
        ctx, order.work_order_id,
        resolutions=[{"exception_id": review.exceptions[0].exception_id, "action": "exclude"}],
        comment="PGND stays internal",
    )
    replayed = pipeline.get_icd_scope_review(ctx, order.work_order_id)

    assert frozen.status == "frozen"
    assert replayed.status == "frozen"
    assert replayed.pending_count == 0
    assert [(item.exception_id, item.action, item.actor_id) for item in replayed.resolutions] == [
        ("exception-pgnd", "exclude", "alice"),
    ]
    with pytest.raises(ValueError, match="already frozen"):
        pipeline.submit_icd_scope_resolution(
            ctx, order.work_order_id,
            resolutions=[{"exception_id": replayed.resolutions[0].exception_id, "action": "include"}],
            comment="retry",
        )


def test_scope_resolution_batch_freezes_review_and_queues_generation_for_same_work_order(tmp_path):
    pipeline, ctx, order = scope_review_pipeline(tmp_path)
    review = prepare_pending_scope_review(pipeline, ctx, order)
    job_store = Mock()
    job_store.create_job.return_value = SimpleNamespace(job_id="job-1")
    pipeline.document_job_store = job_store

    frozen = pipeline.submit_icd_scope_resolution(
        ctx, order.work_order_id,
        resolutions=[{"exception_id": review.exceptions[0].exception_id, "action": "exclude"}],
        comment="PGND stays internal",
    )
    run_id = pipeline.submit_knowledge_base_document_generation(ctx, order.work_order_id)

    assert frozen.status == "frozen"
    assert run_id == "job-1"
    job_store.create_job.assert_called_once()
    kwargs = job_store.create_job.call_args.kwargs
    assert kwargs["work_order_id"] == order.work_order_id
    assert kwargs["operation"] == "generate_work_order"
    assert kwargs["payload"] == {
        "work_order_id": order.work_order_id,
        "knowledge_base_name": "hardware",
    }


def test_continue_kb_generation_reuses_the_existing_work_order_and_snapshot():
    pipeline = object.__new__(AppPipeline)
    ctx = RequestContext(
        user_id="alice", tenant_id="tenant-a", metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "read"},
    )
    order = SimpleNamespace(
        work_order_id="work-continue-1", scope_type="knowledge_base",
        knowledge_base_name="hardware",
    )
    review = IcdScopeReview(
        work_order_id=order.work_order_id, source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(), status="frozen",
    )
    snapshot = SimpleNamespace(source_set_snapshot_id="snapshot-1", source_names=["board.edf"])
    service = Mock()
    service.store.get_work_order.return_value = order
    service.get_icd_scope_review.return_value = review
    service.resolve_source_snapshot.return_value = snapshot
    service.run_internal_harness.return_value = "candidate-1"
    pipeline.document_generation = service
    pipeline._knowledge_base_retriever = Mock(return_value="retrieve")

    result = pipeline.continue_knowledge_base_document_generation(ctx, order.work_order_id)

    assert result == "candidate-1"
    service.require_work_order_capability.assert_called_once_with(ctx, order, "view_project")
    service.get_icd_scope_review.assert_called_once_with(ctx, order.work_order_id)
    pipeline._knowledge_base_retriever.assert_called_once_with(
        ctx, "hardware", ["board.edf"],
        source_set_snapshot_id="snapshot-1", icd_scope_review=review,
    )
    service.run_internal_harness.assert_called_once_with(
        ctx, order.work_order_id, retrieve="retrieve",
    )
