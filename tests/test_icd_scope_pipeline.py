from types import SimpleNamespace
from unittest.mock import Mock

from src.agents.state import Evidence
from src.agents.claim_evidence import InformationRequirement
from src.core.app_pipeline import AppPipeline
from src.document_authoring.models import DocumentFieldSchema, DocumentSchema
from src.document_authoring.models import IcdScopeResolution, IcdScopeReview
from src.document_authoring.icd_scope_decision import IcdScopeDecision, IcdScopeException
from src.pipelines.document_rag.schemas import RequestContext


def _relationship_schema() -> DocumentSchema:
    return DocumentSchema(
        document_schema_id="schema-a",
        version="1",
        document_type="icd",
        execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id="pins",
                label="Connector pin definition",
                required_capabilities=["relationship_lookup"],
                retrieval_policy_id="retrieval-pins",
                verification_policy_id="verify-pins",
            )
        ],
    )


def _pin_mapping_evidence() -> Evidence:
    return Evidence(
        id="circuit:board:pin_mapping:J7",
        content="Pin mapping for J7: 1 -> CAN_H.",
        source_name="board.edf",
        content_kind="circuit_design",
        processor_kind="circuit_design",
        score=0.98,
        locator={"entity_id": "J7", "entity_type": "pin_mapping"},
        metadata={
            "source_group": "circuit_design",
            "pin_mappings": [
                {"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}
            ],
        },
    )


def _pipeline() -> tuple[AppPipeline, RequestContext, Mock, SimpleNamespace]:
    pipeline = object.__new__(AppPipeline)
    ctx = RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "read"},
    )
    order = SimpleNamespace(
        work_order_id="work-1",
        document_schema_id="schema-a",
        document_schema_version="1",
    )
    snapshot = SimpleNamespace(
        source_set_snapshot_id="snapshot-1",
        source_names=["board.edf"],
    )
    service = Mock()
    service.create_knowledge_base_work_order.return_value = order
    service.resolve_source_snapshot.return_value = snapshot
    service._schema.return_value = _relationship_schema()
    service.build_knowledge_base_retrieval_outcome.side_effect = (
        lambda _kb_name, _source_names, evidences, **_kwargs: SimpleNamespace(
            status="success_with_hits" if evidences else "success_empty",
            evidences=evidences,
        )
    )
    pipeline.document_generation = service
    pipeline.list_file_infos = Mock(return_value=[SimpleNamespace(name="board.edf")])
    pipeline.backend = Mock()
    pipeline.backend.retrieve.return_value = []
    pipeline.circuit_service = Mock()
    pipeline.circuit_service.list_pin_mapping_evidence.return_value = [_pin_mapping_evidence()]
    pipeline.spreadsheet_service = None
    return pipeline, ctx, service, snapshot


def test_kb_auto_run_returns_scope_review_before_harness_when_exception_exists():
    pipeline, ctx, service, snapshot = _pipeline()
    service.prepare_icd_scope_review.return_value = SimpleNamespace(
        pending_count=1,
        exceptions=[SimpleNamespace(user_instruction="Confirm J7 exposure")],
    )

    result = pipeline.auto_generate_knowledge_base_document(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
    )

    assert result["stage"] == "scope_review_required"
    assert result["exceptions"][0]["user_instruction"] == "Confirm J7 exposure"
    pipeline.circuit_service.list_pin_mapping_evidence.assert_called_once_with(
        "hardware", list(snapshot.source_names), ctx
    )
    service.run_internal_harness.assert_not_called()


def test_kb_relationship_retrieval_includes_the_frozen_pin_set():
    pipeline, ctx, service, _snapshot = _pipeline()
    review = SimpleNamespace(
        decision=SimpleNamespace(
            frozen_pin_mappings=[
                {"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}
            ]
        )
    )
    pipeline.backend.retrieve.return_value = []
    pipeline.circuit_service = None

    retrieve = pipeline._knowledge_base_retriever(
        ctx,
        "hardware",
        ["board.edf"],
        icd_scope_review=review,
    )
    outcome = retrieve(
        InformationRequirement(
            requirement_id="pins",
            semantic_unit_id="pins",
            claim_type="relationship",
            subject="connector pins",
            required_capabilities=["relationship_lookup"],
        ),
        0,
    )

    assert outcome.status == "success_with_hits"
    assert outcome.evidences[0].metadata["pin_mappings"] == [
        {"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}
    ]
    service.build_knowledge_base_retrieval_outcome.assert_called_once()


def test_kb_relationship_retrieval_omits_user_excluded_scope_exception_pin():
    pipeline, ctx, service, _snapshot = _pipeline()
    review = IcdScopeReview(
        work_order_id="work-1",
        source_snapshot_hash="snapshot-hash",
        status="frozen",
        decision=IcdScopeDecision(
            frozen_pin_mappings=[
                {"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"},
                {"refdes": "J7", "pin_name": "3", "net_name": "PGND"},
            ],
            exceptions=[IcdScopeException(
                exception_id="exception-pgnd",
                kind="extra_pin_exposure",
                refdes="J7",
                pin_name="3",
                net_name="PGND",
                recommended_action="mark_pending",
                user_instruction="Confirm PGND exposure",
            )],
        ),
        resolutions=[IcdScopeResolution(
            exception_id="exception-pgnd",
            action="exclude",
            actor_id="alice",
        )],
    )
    pipeline.backend.retrieve.return_value = []
    pipeline.circuit_service = None

    retrieve = pipeline._knowledge_base_retriever(
        ctx,
        "hardware",
        ["board.edf"],
        icd_scope_review=review,
    )
    outcome = retrieve(
        InformationRequirement(
            requirement_id="pins",
            semantic_unit_id="pins",
            claim_type="relationship",
            subject="connector pins",
            required_capabilities=["relationship_lookup"],
        ),
        0,
    )

    assert outcome.evidences[0].metadata["pin_mappings"] == [
        {"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}
    ]
    assert review.resolutions[0].action == "exclude"
