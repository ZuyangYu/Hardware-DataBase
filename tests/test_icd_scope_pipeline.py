from types import SimpleNamespace
from unittest.mock import Mock
from io import BytesIO
import zipfile

from src.agents.state import Evidence
from src.agents.claim_evidence import InformationRequirement
from src.core.app_pipeline import AppPipeline
from src.document_authoring.models import DocumentFieldSchema, DocumentSchema
from src.document_authoring.models import IcdScopeResolution, IcdScopeReview
from src.document_authoring.icd_scope_decision import IcdScopeDecision, IcdScopeException
from src.pipelines.document_rag.schemas import RequestContext


def _relationship_schema(*, query_terms: list[str] | None = None) -> DocumentSchema:
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
                query_terms=(
                    ["connector J7 pinout"] if query_terms is None else query_terms
                ),
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


def _pin_mapping_evidence_for(refdes: str, pin_name: str, net_name: str) -> Evidence:
    evidence = _pin_mapping_evidence()
    return evidence.model_copy(update={
        "id": f"circuit:board:pin_mapping:{refdes}",
        "source_name": "board.edf",
        "locator": {"entity_id": refdes, "entity_type": "pin_mapping"},
        "metadata": {
            "source_group": "circuit_design",
            "pin_mappings": [{
                "refdes": refdes, "pin_name": pin_name, "net_name": net_name,
            }],
        },
    })


def _front_view_template_bytes(refdes: str = "X302") -> bytes:
    """A minimal uploaded ICD template with one explicit front-view slot."""
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="接口" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>板端接插件前视图管序布局和定义</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>管脚定义 Pin Definition</t></is></c></row>'
            f'<row r="3"><c r="A3" t="inlineStr"><is><t>管脚号 Pin Number</t></is></c><c r="B3" t="inlineStr"><is><t>{refdes}-20</t></is></c></row>'
            '<row r="4"><c r="A4" t="inlineStr"><is><t>板端接插件序号</t></is></c><c r="B4"><v>20</v></c></row>'
            '</sheetData></worksheet>',
        )
    return content.getvalue()


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
        template_version_id="template-a",
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
        "hardware", list(snapshot.source_names), ctx, refdes=["J7"]
    )
    service.run_internal_harness.assert_not_called()


def test_kb_icd_scope_queries_only_explicit_connector_refdes():
    pipeline, ctx, service, snapshot = _pipeline()
    service._schema.return_value = _relationship_schema(query_terms=["connector J7 pinout"])
    pipeline.circuit_service.list_pin_mapping_evidence.return_value = [
        _pin_mapping_evidence_for("J7", "1", "CAN_H"),
        _pin_mapping_evidence_for("U1", "1", "VDD"),
    ]
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
    pipeline.circuit_service.list_pin_mapping_evidence.assert_called_once_with(
        "hardware", list(snapshot.source_names), ctx, refdes=["J7"],
    )


def test_non_icd_relationship_field_does_not_enumerate_every_edf_pin():
    pipeline, ctx, service, _snapshot = _pipeline()
    service._schema.return_value = DocumentSchema(
        document_schema_id="architecture", version="1", document_type="architecture",
        execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id="net_relation", label="Network relationship",
            required_capabilities=["relationship_lookup"],
            retrieval_policy_id="retrieval-network",
            verification_policy_id="verify-network",
        )],
    )
    service.run_internal_harness.return_value = SimpleNamespace(artifact_id="candidate-1")

    result = pipeline.auto_generate_knowledge_base_document(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="architecture",
        document_schema_version="1",
    )

    assert result.artifact_id == "candidate-1"
    pipeline.circuit_service.list_pin_mapping_evidence.assert_not_called()
    service.prepare_icd_scope_review.assert_not_called()


def test_icd_scope_uses_direct_refdes_pin_from_fpt_when_template_has_only_pin_headers():
    pipeline, ctx, service, snapshot = _pipeline()
    service._schema.return_value = _relationship_schema(query_terms=[])
    pipeline.backend.retrieve.return_value = [SimpleNamespace(
        source_name="FPT.xlsx",
        content="X1900-14 UBD voltage sampling",
        metadata={"document_role": "fpt"},
    )]
    service.prepare_icd_scope_review.return_value = SimpleNamespace(
        pending_count=1,
        exceptions=[SimpleNamespace(user_instruction="Confirm X1900 exposure")],
    )

    pipeline.auto_generate_knowledge_base_document(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
    )

    pipeline.circuit_service.list_pin_mapping_evidence.assert_called_once_with(
        "hardware", list(snapshot.source_names), ctx, refdes=["X1900"],
    )


def test_icd_pin_template_without_connector_candidate_returns_one_scope_todo_not_a_full_edf_scan():
    pipeline, ctx, service, _snapshot = _pipeline()
    service._schema.return_value = _relationship_schema(query_terms=[])
    pipeline.backend.retrieve.return_value = []
    service.prepare_icd_scope_review.side_effect = lambda _ctx, _work_order_id, decision: SimpleNamespace(
        pending_count=1, exceptions=decision.exceptions,
    )

    result = pipeline.auto_generate_knowledge_base_document(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
    )

    assert result["stage"] == "scope_review_required"
    assert [issue["kind"] for issue in result["exceptions"]] == ["connector_scope_unknown"]
    assert "模板字段的检索条件" in result["exceptions"][0]["user_instruction"]
    pipeline.circuit_service.list_pin_mapping_evidence.assert_not_called()


def test_icd_connector_with_no_edf_mapping_returns_controlled_blocker():
    pipeline, ctx, service, snapshot = _pipeline()
    service._schema.return_value = _relationship_schema(query_terms=["connector J7 pinout"])
    pipeline.circuit_service.list_pin_mapping_evidence.return_value = []
    service.prepare_icd_scope_review.side_effect = lambda _ctx, _work_order_id, decision: SimpleNamespace(
        pending_count=len(decision.exceptions), exceptions=decision.exceptions,
    )

    result = pipeline.auto_generate_knowledge_base_document(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
    )

    assert result["stage"] == "scope_review_required"
    assert [issue["kind"] for issue in result["exceptions"]] == ["connector_mapping_missing"]
    assert result["exceptions"][0]["refdes"] == "J7"
    pipeline.circuit_service.list_pin_mapping_evidence.assert_called_once_with(
        "hardware", list(snapshot.source_names), ctx, refdes=["J7"],
    )
    service.run_internal_harness.assert_not_called()


def test_icd_scope_reads_connector_candidate_from_uploaded_front_view_template():
    pipeline, ctx, service, snapshot = _pipeline()
    service._schema.return_value = _relationship_schema(query_terms=[])
    service.store.read_template_content.return_value = _front_view_template_bytes("X302")
    pipeline.circuit_service.list_pin_mapping_evidence.return_value = [
        _pin_mapping_evidence_for("X302", "20", "CAN_H")
    ]
    service.prepare_icd_scope_review.side_effect = lambda _ctx, _work_order_id, decision: SimpleNamespace(
        pending_count=len(decision.exceptions), exceptions=decision.exceptions,
    )

    result = pipeline.auto_generate_knowledge_base_document(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
    )

    assert result["stage"] == "scope_review_required"
    pipeline.circuit_service.list_pin_mapping_evidence.assert_called_once_with(
        "hardware", list(snapshot.source_names), ctx, refdes=["X302"],
    )


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


def test_frozen_icd_pin_evidence_preserves_each_mapping_edf_source():
    pipeline, ctx, service, _snapshot = _pipeline()
    pipeline.backend.retrieve.return_value = []
    pipeline.circuit_service = None
    review = SimpleNamespace(decision=SimpleNamespace(frozen_pin_mappings=[
        {"refdes": "J7", "pin_name": "1", "net_name": "CAN_H", "source_name": "left.edf"},
        {"refdes": "J8", "pin_name": "1", "net_name": "CAN_L", "source_name": "right.edf"},
    ]))

    retrieve = pipeline._knowledge_base_retriever(
        ctx, "hardware", ["left.edf", "right.edf"], icd_scope_review=review,
    )
    outcome = retrieve(InformationRequirement(
        requirement_id="pins", semantic_unit_id="pins", claim_type="relationship",
        subject="connector pins", required_capabilities=["relationship_lookup"],
    ), 0)

    assert {
        (evidence.source_name, evidence.metadata["pin_mappings"][0]["source_name"])
        for evidence in outcome.evidences
    } == {("left.edf", "left.edf"), ("right.edf", "right.edf")}
