from types import SimpleNamespace

from src.document_authoring.circuit_capabilities import enrich_circuit_capabilities
from src.document_authoring.harness.graph import _requirement_for_unit


def test_connection_field_terms_add_relationship_lookup_without_project_specific_rules():
    capabilities = enrich_circuit_capabilities(
        ["document_claim_lookup"],
        label="Pin Definition",
        description="连接器的引脚与网络对应关系",
        query_terms=["connector pinout"],
    )

    assert capabilities == ["document_claim_lookup", "relationship_lookup"]


def test_component_model_terms_add_entity_lookup_and_preserve_declared_capabilities():
    capabilities = enrich_circuit_capabilities(
        ["tabular_lookup"],
        label="板端型号",
        query_terms=["component part number"],
    )

    assert capabilities == ["tabular_lookup", "entity_lookup"]


def test_non_circuit_field_does_not_gain_a_circuit_capability():
    capabilities = enrich_circuit_capabilities(
        ["document_claim_lookup"],
        label="项目背景",
        description="系统项目概述",
        query_terms=["introduction"],
    )

    assert capabilities == ["document_claim_lookup"]


def test_pin_definition_field_requests_circuit_relationship_evidence():
    schema = SimpleNamespace(
        required_capabilities=["document_claim_lookup"],
        preferred_source_roles=[],
        label="引脚定义",
        description="接插件的管脚连接网络",
        query_terms=[],
        missing_policy="mark_tbd",
    )
    work_order = SimpleNamespace(
        work_order_id="work-1",
        input_fingerprint="input-1",
        project_id="project-1",
        baseline_id="baseline-1",
        scope_type="knowledge_base",
    )
    snapshot = SimpleNamespace(source_names=["board.edf"])

    requirement = _requirement_for_unit(
        {"unit_id": "field:pins", "kind": "field", "schema": schema},
        work_order,
        snapshot,
    )

    assert requirement.required_capabilities == ["document_claim_lookup", "relationship_lookup"]
