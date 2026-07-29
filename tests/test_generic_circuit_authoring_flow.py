from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.circuit.evidence_mapper import CircuitEvidenceMapper
from src.document_authoring.harness.graph import AuthoringGraph
from src.document_authoring.harness.policy import HarnessToolPolicy
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentFieldSchema,
    DocumentSchema,
    DocumentUnitDraft,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
)
from src.document_authoring.service import DocumentGenerationService


def _policy() -> HarnessPolicy:
    return HarnessPolicy(
        harness_policy_id="policy-1",
        version="1",
        status="approved",
        max_steps=30,
        max_retrieval_rounds=4,
        max_retrieval_attempts_per_unit=1,
        allowed_tools=[
            "retrieve_evidence",
            "draft_ready_unit",
            "validate_unit_draft",
            "detect_template_contamination",
            "validate_cross_unit",
        ],
    )


def _work_order(source_name: str) -> DocumentWorkOrder:
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


@pytest.mark.parametrize(
    ("source_name", "refdes", "pins", "expected_rows"),
    [
        (
            "alpha.edf",
            "P17",
            [{"name": "&A1", "net_name": "DATA_P"}, {"name": "&A2", "net_name": None}],
            [{"pin": "A1", "definition": "DATA_P"}, {"pin": "A2", "definition": "NC"}],
        ),
        (
            "beta.edf",
            "CN7",
            [
                {"name": "1", "net_name": "ETH_RX"},
                {"name": "2", "net_name": "PGND"},
                {"name": "3", "net_name": "ETH_TX"},
            ],
            [
                {"pin": "1", "definition": "ETH_RX"},
                {"pin": "2", "definition": "PGND"},
                {"pin": "3", "definition": "ETH_TX"},
            ],
        ),
    ],
)
def test_generic_pin_definition_flow_passes_all_circuit_pins_to_the_writer(
    source_name,
    refdes,
    pins,
    expected_rows,
):
    circuit_evidence = CircuitEvidenceMapper().build(
        kind="pin_mapping",
        row={"design_id": "design-1", "refdes": refdes, "pins": pins},
        metadata={"record_id": 1, "kb_name": "hardware"},
        source_name=source_name,
        score=0.9,
    )
    outcome = DocumentGenerationService.build_knowledge_base_retrieval_outcome(
        "hardware",
        [source_name],
        [circuit_evidence],
        requirement_id="requirement-1",
        source_set_snapshot_id="snapshot-1",
    )
    captured_requests = []

    def writer(request):
        captured_requests.append(request)
        mappings = request.evidence[0]["metadata"]["pin_mappings"]
        rows = [
            {
                "pin": item["pin_name"],
                "definition": item["net_name"] or "NC",
            }
            for item in mappings
        ]
        return DocumentUnitDraft(
            unit_id=request.unit_id,
            run_id=request.run_id,
                generated_by="managed_writer",
            proposed_value=rows,
            validation_status="supported",
        )

    validator = Mock()
    validator.validate_unit_draft.side_effect = lambda draft, _evidence: draft
    validator.detect_template_contamination.return_value = []
    validator.validate_cross_unit_consistency.return_value = []
    graph = AuthoringGraph(
        HarnessToolPolicy(_policy()),
        Mock(),
        validator=validator,
        draft_provider=writer,
    )
    schema = DocumentSchema(
        document_schema_id="schema-1",
        version="1",
        document_type="icd",
        execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id="pin_definition",
                label="Pin Definition",
                description="连接器引脚与网络连接",
                value_type="table_rows",
                value_schema={"columns": [{"column_id": "pin"}, {"column_id": "definition"}]},
                retrieval_policy_id="retrieval-1",
                verification_policy_id="verification-1",
                authoring_policy="managed_writer",
            )
        ],
    )
    work_order = _work_order(source_name)
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot-1",
        tenant_id="tenant-1",
        knowledge_base_name="hardware",
        source_names=[source_name],
        created_by="alice",
    )
    result = graph.run(
        work_order=work_order,
        harness_run=HarnessRun(
            harness_run_id="run-1",
            work_order_id=work_order.work_order_id,
            run_manifest_id="manifest-1",
            status="running",
        ),
        run_manifest=AuthoringRunManifest(
            run_manifest_id="manifest-1",
            work_order_id=work_order.work_order_id,
            harness_policy_id="policy-1",
            harness_policy_version="1",
            writer_provider_id="controlled-test-writer",
            prompt_version="1",
            source_set_snapshot_id="snapshot-1",
            input_fingerprint=work_order.input_fingerprint,
        ),
        schema=schema,
        snapshot=snapshot,
        legacy_claims=[],
        retrieve=lambda _requirement, _attempt, _query_override=None, **_kwargs: outcome,
    )

    assert result.requirements["field:pin_definition"].required_capabilities == ["relationship_lookup"]
    assert len(captured_requests) == 1
    assert captured_requests[0].evidence[0]["metadata"]["pin_mappings"]
    assert result.drafts[0].proposed_value == expected_rows
    assert result.unit_statuses["field:pin_definition"] == "ready_to_render"
