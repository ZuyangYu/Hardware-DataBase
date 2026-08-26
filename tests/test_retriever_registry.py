from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.agents.claim_evidence import InformationRequirement
from src.agents.schemas import Evidence
from src.document_authoring.retriever_registry import (
    CrossUnitEvidenceCache,
    RetrieverRegistry,
    apply_role_boost,
    content_hash,
    dedup_by_content,
)


def _req(subject: str = "额定电压", caps=None, roles=None) -> InformationRequirement:
    return InformationRequirement(
        requirement_id="r",
        semantic_unit_id="field:f1",
        claim_type="attribute",
        subject=subject,
        required_capabilities=caps or [],
        preferred_source_roles=roles or [],
    )


def _ev(
    content: str = "content",
    score: float = 0.5,
    source_name: str = "spec.pdf",
    role: str | None = None,
    unit_id: str | None = None,
) -> Evidence:
    metadata: dict = {}
    if role is not None:
        metadata["document_role"] = role
    if unit_id is not None:
        metadata["reused_from_unit"] = unit_id
    return Evidence(
        id=f"id:{content}:{score}",
        content=content,
        source_name=source_name,
        content_kind="document_text",
        processor_kind="ragflow",
        score=score,
        metadata=metadata,
    )


# --------------------------------------------------------------------------- #
# dedup_by_content
# --------------------------------------------------------------------------- #

def test_dedup_keeps_highest_score_for_same_content():
    evs = [_ev("same", 0.3), _ev("same", 0.9), _ev("other", 0.5)]
    result = dedup_by_content(evs)
    assert len(result) == 2
    kept = next(e for e in result if e.content == "same")
    assert kept.score == 0.9


def test_content_hash_strips_whitespace():
    assert content_hash(_ev("  same  ")) == content_hash(_ev("same"))


# --------------------------------------------------------------------------- #
# apply_role_boost
# --------------------------------------------------------------------------- #

def test_role_boost_applies_to_matching_role():
    evs = [_ev("a", score=0.4, role="specification")]
    result = apply_role_boost(evs, ["specification"], factor=1.5)
    assert result[0].score == pytest.approx(0.6, abs=1e-9)
    assert result[0].metadata["preferred_source_role_match"] == "specification"


def test_role_boost_noop_without_role():
    evs = [_ev("a", score=0.4)]
    result = apply_role_boost(evs, ["specification"], factor=1.5)
    assert result[0].score == 0.4
    assert "preferred_source_role_match" not in result[0].metadata


def test_role_boost_noop_when_role_does_not_match():
    evs = [_ev("a", score=0.4, role="drawing")]
    result = apply_role_boost(evs, ["specification"], factor=1.5)
    assert result[0].score == 0.4


# --------------------------------------------------------------------------- #
# CrossUnitEvidenceCache
# --------------------------------------------------------------------------- #

def test_cross_unit_cache_offers_on_empty_with_term_overlap():
    cache = CrossUnitEvidenceCache(max_reuse_per_unit=5)
    cache.ingest([_ev("BOM 用量 row", score=0.8, source_name="bom.xlsx")], "field:a")
    offered = cache.offer(_req(subject="用量"), "用量", "field:b")
    assert len(offered) == 1
    assert offered[0].metadata.get("reused") is True
    assert offered[0].metadata.get("reused_from_unit") == "field:a"


def test_cross_unit_cache_no_offer_without_term_overlap():
    cache = CrossUnitEvidenceCache(max_reuse_per_unit=5)
    cache.ingest([_ev("completely unrelated text", score=0.8)], "field:a")
    offered = cache.offer(_req(subject="用量"), "用量", "field:b")
    assert offered == []


def test_cross_unit_cache_caps_reuse():
    cache = CrossUnitEvidenceCache(max_reuse_per_unit=2)
    cache.ingest(
        [
            _ev("用量 a", score=0.8),
            _ev("用量 b", score=0.7),
            _ev("用量 c", score=0.6),
            _ev("用量 d", score=0.5),
        ],
        "field:a",
    )
    offered = cache.offer(_req(subject="用量"), "用量", "field:b")
    assert len(offered) == 2


def test_cross_unit_cache_does_not_offer_from_same_unit():
    cache = CrossUnitEvidenceCache(max_reuse_per_unit=5)
    cache.ingest([_ev("用量 row", score=0.8)], "field:a")
    # Same unit should not get its own evidence back as "reused".
    offered = cache.offer(_req(subject="用量"), "用量", "field:a")
    assert offered == []


# --------------------------------------------------------------------------- #
# RetrieverRegistry.retrieve
# --------------------------------------------------------------------------- #

def test_default_always_invoked_for_empty_capabilities():
    default = Mock(return_value=[_ev("d", 0.5)])
    registry = RetrieverRegistry(default_retriever=default)
    result = registry.retrieve(_req(caps=[]), "query")
    default.assert_called_once()
    assert len(result) == 1


def test_specialized_dispatched_for_tabular_lookup():
    default = Mock(return_value=[_ev("text", 0.4)])
    spreadsheet = Mock(return_value=[_ev("table", 0.9, source_name="bom.xlsx")])
    registry = RetrieverRegistry(
        default_retriever=default,
        specialized={"tabular_lookup": spreadsheet},
    )
    result = registry.retrieve(_req(caps=["tabular_lookup"]), "query")
    default.assert_called_once()
    spreadsheet.assert_called_once()
    contents = {e.content for e in result}
    assert contents == {"text", "table"}


def test_revision_lookup_falls_back_to_default():
    default = Mock(return_value=[_ev("d", 0.5)])
    registry = RetrieverRegistry(default_retriever=default)  # no specialized for revision
    result = registry.retrieve(_req(caps=["revision_lookup"]), "query")
    default.assert_called_once()
    assert len(result) == 1


def test_registry_dedups_across_default_and_specialized():
    default = Mock(return_value=[_ev("dup", 0.3)])
    spreadsheet = Mock(return_value=[_ev("dup", 0.9, source_name="bom.xlsx")])
    registry = RetrieverRegistry(
        default_retriever=default,
        specialized={"tabular_lookup": spreadsheet},
    )
    result = registry.retrieve(_req(caps=["tabular_lookup"]), "query")
    assert len(result) == 1
    assert result[0].score == 0.9


def test_registry_applies_role_boost():
    default = Mock(return_value=[_ev("a", 0.4, role="specification")])
    registry = RetrieverRegistry(default_retriever=default)
    result = registry.retrieve(_req(roles=["specification"]), "query")
    assert result[0].score == pytest.approx(0.6, abs=1e-9)
    assert result[0].metadata["preferred_source_role_match"] == "specification"


def test_registry_offers_cross_unit_reuse_when_fresh_empty():
    default = Mock(return_value=[])  # fresh retrieval empty
    cache = CrossUnitEvidenceCache(max_reuse_per_unit=5)
    # Prior unit populated the cache with an overlapping-evidence entry.
    cache.ingest([_ev("用量 row", 0.8, source_name="bom.xlsx")], "field:a")
    registry = RetrieverRegistry(default_retriever=default, cross_unit_cache=cache)
    result = registry.retrieve(_req(subject="用量", caps=["tabular_lookup"]), "用量")
    assert len(result) == 1
    assert result[0].metadata.get("reused") is True
    assert result[0].metadata.get("reused_from_unit") == "field:a"


def test_registry_does_not_offer_reuse_when_fresh_has_hits():
    default = Mock(return_value=[_ev("fresh hit", 0.9)])
    cache = CrossUnitEvidenceCache(max_reuse_per_unit=5)
    cache.ingest([_ev("fresh hit", 0.8)], "field:a")  # would overlap
    registry = RetrieverRegistry(default_retriever=default, cross_unit_cache=cache)
    result = registry.retrieve(_req(subject="fresh"), "fresh hit")
    # No reused tag because fresh retrieval had hits.
    assert all(not e.metadata.get("reused") for e in result)
    assert len(result) == 1  # deduped to one
