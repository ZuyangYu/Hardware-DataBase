from __future__ import annotations

from src.agents.claim_evidence import EvidenceCapability, plan_claims
from src.agents.graph import _claim_compatible, _claim_coverage, _source_capabilities, analyze_question
from src.agents.runner import _select_claim_context
from src.ingestion.parser_registry import DomainManifest, ParserRegistry


def _fake_parser(path: str, kb_name: str, source_group: str, progress):
    return []


def test_relationship_claim_requires_direct_relationship_capability():
    claim = plan_claims("模块 A 的输入连接到哪个网络？")[0]

    assert claim.operation == "relationship"
    assert claim.required_capabilities == ["relationship_lookup"]
    assert claim.support_mode == "direct"


def test_manifest_capability_registration_is_independent_of_file_name():
    relationship = EvidenceCapability(
        name="relationship_lookup",
        content_kinds=["custom_graph"],
        direct_fact=True,
    )
    registry = ParserRegistry()
    registry.register_manifest(
        DomainManifest(
            name="custom",
            parser_factories={"custom_group": _fake_parser},
            capabilities={"custom_group": (relationship,)},
        )
    )

    assert registry.capabilities_for("custom_group") == (relationship,)


def test_analysis_emits_claims_for_a_generic_connection_question():
    result = analyze_question({"user_query": "模块 A 的输入连接到哪个网络？", "trace": []})

    claim = result["question_analysis"]["claims"][0]
    assert claim["operation"] == "relationship"
    assert claim["required_capabilities"] == ["relationship_lookup"]


def test_legacy_circuit_record_advertises_relationship_capability():
    assert "relationship_lookup" in _source_capabilities({"processor_kind": "circuit_design"})


def test_document_text_cannot_support_exact_relationship_claim():
    claim = plan_claims("模块 A 的输入连接到哪里？")[0]

    coverage = _claim_coverage(
        [claim],
        [{"id": "doc-1", "content_kind": "document_text", "content": "模块 A 用于供电"}],
    )[0]

    assert coverage.status == "missing"
    assert coverage.missing_capabilities == ["relationship_lookup"]


def test_direct_relation_evidence_supports_relationship_claim():
    claim = plan_claims("模块 A 的输入连接到哪里？")[0]

    coverage = _claim_coverage(
        [claim],
        [
            {
                "id": "net-1",
                "content_kind": "circuit_design",
                "content": "A.VIN connects NET_IN",
                "metadata": {"fact_type": "relationship", "certainty": "direct"},
            }
        ],
    )[0]

    assert coverage.status == "supported"
    assert coverage.evidence_ids == ["net-1"]


def test_claim_context_keeps_required_evidence_when_budget_is_small():
    selected = _select_claim_context(
        {
            "claim_coverage": [
                {"claim_id": "c1", "status": "supported", "evidence_ids": ["relation-1"]},
                {"claim_id": "c2", "status": "supported", "evidence_ids": ["document-1"]},
            ],
            "merged_evidence": [
                {"id": "noise-1", "score": 0.99},
                {"id": "relation-1", "score": 0.20},
                {"id": "document-1", "score": 0.10},
                {"id": "noise-2", "score": 0.98},
            ],
        },
        limit=2,
    )

    assert {item["id"] for item in selected} == {"relation-1", "document-1"}


def test_source_selection_uses_claim_capability_not_file_name():
    relationship_claim = plan_claims("模块 A 的输入连接到哪里？")[0]

    assert _claim_compatible(
        {"processor_kind": "circuit_design", "status": "indexed", "document_name": "arbitrary.bin"},
        relationship_claim,
    )
    assert not _claim_compatible({"processor_kind": "ragflow", "document_name": "connection_manual.pdf"}, relationship_claim)
