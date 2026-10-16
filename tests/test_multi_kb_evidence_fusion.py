"""R3 T5: full provenance, rank fusion, qualified coverage and token admission."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from src.pipelines.document_rag.schemas import Evidence
from src.services.knowledge_scope_models import KnowledgeSourceRetrievalOutcome


def evidence(kb=101, *, chunk="chunk-1", content="retrieved evidence", score=0.9, record_id=9, role="primary"):
    return Evidence(id=chunk, content=content, source_name="spec.pdf", score=score, metadata={
        "tenant_key": "default", "department_id": "3", "kb_id": kb,
        "processor_kind": "ragflow", "local_record_id": record_id,
        "source_content_hash": hashlib.sha256(b"file-bytes").hexdigest(),
        "evidence_text_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "processing_artifact_id": "", "index_revision": 0,
        "remote_document_id": "remote-1", "chunk_or_structured_locator": chunk,
        "knowledge_source": {"kb_id": kb, "kb_name": f"KB{kb}", "department_id": "3", "role": role},
    })


def outcome(kb, items, *, query="question"):
    return KnowledgeSourceRetrievalOutcome(kb, query, "document", "success_with_hits", items)


@pytest.mark.parametrize("field,value", [
    ("kb_id", 205), ("local_record_id", 10), ("source_content_hash", hashlib.sha256(b"other file").hexdigest()),
    ("evidence_text_hash", hashlib.sha256(b"changed text").hexdigest()),
    ("chunk_or_structured_locator", "chunk-2"), ("index_revision", 1), ("tenant_key", "other"),
    ("processing_artifact_id", "artifact-2"), ("department_id", "4"),
])
def test_full_identity_changes_when_any_source_or_version_component_changes(field, value):
    from src.services.evidence_fusion import evidence_identity

    fields = evidence().metadata
    assert evidence_identity(fields) != evidence_identity({**fields, field: value})


def test_raw_text_hash_preserves_whitespace_and_does_not_replace_file_hash():
    from src.services.evidence_fusion import evidence_text_hash

    assert evidence_text_hash("5 V\n") == hashlib.sha256(b"5 V\n").hexdigest()
    assert evidence_text_hash("5 V\n") != evidence_text_hash("5 V")
    assert evidence().metadata["source_content_hash"] != evidence_text_hash("retrieved evidence")


@pytest.mark.parametrize("field", ["kb_id", "source_content_hash", "local_record_id", "chunk_or_structured_locator", "evidence_text_hash"])
def test_missing_identity_is_rejected_without_guessing(field):
    from src.services.evidence_fusion import EvidenceIdentityIncomplete, evidence_identity

    fields = dict(evidence().metadata)
    fields.pop(field)
    with pytest.raises(EvidenceIdentityIncomplete, match="evidence_identity_incomplete"):
        evidence_identity(fields)


def test_rrf_merges_same_chunk_across_queries_but_preserves_same_text_in_other_kb():
    from src.services.evidence_fusion import fuse_evidence

    item = evidence()
    other = evidence(205, role="reference")
    result = fuse_evidence([outcome(101, [item], query="first"), outcome(101, [item], query="second"), outcome(205, [other])],
                           purpose="answer", max_items=8, token_budget=100, token_counter=lambda _: 10)
    assert len(result.evidence) == 2
    assert len({item.id for item in result.evidence}) == 2
    assert result.evidence[0].metadata["fusion_score"] == pytest.approx(2 / 61)
    assert result.evidence[1].metadata["fusion_score"] == pytest.approx(1 / 61)
    assert {item.metadata["kb_id"] for item in result.evidence} == {101, 205}
    assert result.coverage == {101: True, 205: True}
    assert result.complete


def test_duplicate_rows_in_same_query_do_not_inflate_rrf():
    from src.services.evidence_fusion import fuse_evidence

    item = evidence()
    result = fuse_evidence([outcome(101, [item, item])], purpose="answer", max_items=8, token_budget=100, token_counter=lambda _: 10)
    assert result.evidence[0].metadata["fusion_score"] == pytest.approx(1 / 61)


def test_four_peer_sources_keep_one_each_before_filling_remaining_budget():
    from src.services.evidence_fusion import fuse_evidence

    outcomes = [outcome(kb, [evidence(kb, chunk=f"chunk-{i}", role="peer") for i in range(8)]) for kb in [101, 205, 306, 407]]
    result = fuse_evidence(outcomes, purpose="compare", max_items=4, token_budget=40, token_counter=lambda _: 10)
    assert len(result.evidence) == 4
    assert result.coverage == {101: True, 205: True, 306: True, 407: True}
    assert result.complete
    assert result.token_count == 40


def test_token_crop_recomputes_coverage_and_preserves_candidate_identities():
    from src.services.evidence_fusion import fuse_evidence

    result = fuse_evidence([outcome(kb, [evidence(kb, role="peer")]) for kb in [101, 205, 306, 407]],
                           purpose="compare", max_items=4, token_budget=20, token_counter=lambda _: 10)
    assert len(result.evidence) == 2
    assert sum(result.coverage.values()) == 2
    assert not result.complete
    assert len(result.candidate_refs) == 4
    assert all("content" not in ref for ref in result.candidate_refs)


def test_low_quality_candidates_never_manufacture_coverage():
    from src.services.evidence_fusion import fuse_evidence

    result = fuse_evidence([outcome(101, [evidence()]), outcome(205, [evidence(205, role="reference", score=0.01)])],
                           purpose="answer", max_items=4, token_budget=100, min_score=0.5, token_counter=lambda _: 10)
    assert result.coverage == {101: True, 205: False}
    assert len(result.evidence) == 1
    assert result.discarded["quality"] == 1


def test_reference_score_does_not_displace_primary_when_only_one_item_fits():
    from src.services.evidence_fusion import fuse_evidence

    result = fuse_evidence([outcome(205, [evidence(205, score=0.99, role="reference")]), outcome(101, [evidence(score=0.6)])],
                           purpose="compose", max_items=1, token_budget=10, token_counter=lambda _: 10)
    assert result.evidence[0].metadata["kb_id"] == 101


def test_fusion_checks_raw_text_hash_and_never_merges_failed_source_evidence():
    from src.services.evidence_fusion import fuse_evidence

    stale = replace(evidence(), content="changed after hash")
    failed = outcome(205, [evidence(205, role="reference")])
    failed.status = "source_changed"
    result = fuse_evidence([outcome(101, [stale]), failed], purpose="answer", max_items=4, token_budget=100)
    assert result.evidence == []
    assert result.discarded["identity"] == 1


def test_runtime_reserves_a_slot_for_other_selected_source_and_keeps_citation_numbers():
    from src.agents.schemas import Evidence as AgentEvidence
    from src.agents.tools.runtime import ToolRuntime
    from tests.test_multi_kb_store_contract import snapshot

    scope = snapshot(1)
    rt = ToolRuntime(kb_name="KB101", ctx=None, knowledge_scope=scope, evidence_budget=4, question="B highly relevant")

    def row(kb, number):
        return AgentEvidence(id=f"{kb}:{number}", content="B highly relevant" if kb == 205 else "A text",
            source_name="spec.pdf", content_kind="document_text", processor_kind="ragflow",
            metadata={"kb_id": kb, "fusion_rank": number}, score=0.1 if kb == 205 else 0.99)

    assert rt.add_evidence([row(101, i) for i in range(1, 9)]) == [1, 2, 3, None, None, None, None, None]
    assert rt.add_evidence([row(205, 1)]) == [4]
    ids = [item.id for item in rt.evidence]
    rt.finalize_evidence()
    assert [item.id for item in rt.evidence] == ids
    assert rt.evidence_index["205:1"] == 4


def test_four_source_query_representatives_survive_repeated_generic_rrf_hits():
    """Minimized from live EPS/ADAS/control retrieval; no source passages copied."""
    from src.services.evidence_fusion import fuse_evidence

    eps60 = evidence(101, chunk="supply60", content="KL30 18-32V", score=.86, role="peer")
    generic60 = evidence(101, chunk="predriver60", content="L9908 current monitor", score=.42, role="peer")
    eps62 = evidence(205, chunk="supply62", content="battery 9-36V", score=.74, role="peer")
    generic62 = evidence(205, chunk="predriver62", content="L9908 SPI", score=.45, role="peer")
    adas = evidence(306, chunk="pmic", content="LP87702 VCC1V8 VCC1V1", score=.89, role="peer")
    control = evidence(407, chunk="control", content="KL30 20-30V (test candidate)", score=.65, role="peer")
    outcomes = [
        outcome(101, [eps60, generic60], query="KL30 operating voltage"),
        outcome(101, [replace(generic60, score=.4)], query="battery operating voltage"),
        outcome(101, [replace(generic60, score=.3)], query="LP87702 output"),
        outcome(205, [generic62], query="KL30 operating voltage"),
        outcome(205, [eps62, generic62], query="battery operating voltage"),
        outcome(205, [replace(generic62, score=.3)], query="LP87702 output"),
        outcome(306, [adas], query="LP87702 output"),
        outcome(407, [control], query="KL30 operating voltage"),
    ]
    result = fuse_evidence(outcomes, purpose="compare", max_items=4, token_budget=40, token_counter=lambda _: 10)
    assert {item.content for item in result.evidence} == {eps60.content, eps62.content, adas.content, control.content}
    assert result.complete
    assert result.token_count == 40
    assert [item.metadata["fusion_rank"] for item in result.evidence] == [1, 2, 3, 4]
    retained = {(item["kb_id"], item["query"]): item["top_candidate_retained"] for item in result.query_coverage}
    assert retained[(101, "KL30 operating voltage")] is True
    assert retained[(205, "battery operating voltage")] is True
    assert retained[(101, "LP87702 output")] is False


def test_unknown_score_scale_falls_back_to_rank_without_displacing_repeated_hit():
    from src.services.evidence_fusion import fuse_evidence

    stable = evidence(chunk="stable", content="repeated relevant result", score=.8)
    unknown = evidence(chunk="unknown", content="unknown score scale", score=1000)
    result = fuse_evidence([outcome(101, [stable], query="first"), outcome(101, [stable], query="second"),
                            outcome(101, [unknown], query="third")],
                           purpose="answer", max_items=1, token_budget=10, token_counter=lambda _: 10)
    assert result.evidence[0].content == stable.content
    assert result.source_representatives[0]["selection_method"] == "rank"


def test_query_coverage_reports_budget_crop_without_inventing_a_retained_hit():
    from src.services.evidence_fusion import fuse_evidence

    first = evidence(chunk="first", score=.9)
    second = evidence(chunk="second", score=.8)
    result = fuse_evidence([outcome(101, [first], query="first"), outcome(101, [second], query="second")],
                           purpose="answer", max_items=1, token_budget=10, token_counter=lambda _: 10)
    rows = {item["query"]: item for item in result.query_coverage}
    assert rows["first"]["covered"] is True
    assert rows["second"]["covered"] is False
    assert rows["second"]["budget_cropped"] is True
    assert result.discarded["budget"] == 1
