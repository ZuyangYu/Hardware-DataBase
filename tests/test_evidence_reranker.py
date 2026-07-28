from __future__ import annotations

from unittest.mock import Mock

from src.agents.claim_evidence import InformationRequirement
from src.core.llm_client import LLMClient
from src.document_authoring.writers.evidence_reranker import EvidenceReranker


def _req(subject: str = "额定电流", predicate: str = "电源拓扑") -> InformationRequirement:
    return InformationRequirement(
        requirement_id="req-1",
        semantic_unit_id="field:f1",
        claim_type="attribute",
        subject=subject,
        predicate=predicate,
        required_capabilities=["entity_lookup"],
    )


def _ev(eid: str, content: str) -> dict:
    """Mirror the validated-evidence dict shape produced by _validated_evidence."""
    return {
        "id": eid,
        "content": content,
        "source_name": "s.pdf",
        "metadata": {},
        "locator": {},
        "fact_type": None,
    }


def _client(return_value: str = "[]", *, side_effect=None) -> Mock:
    client = Mock(spec=LLMClient)
    if side_effect is not None:
        client.chat.side_effect = side_effect
    else:
        client.chat.return_value = return_value
    return client


def test_rerank_reorders_by_llm_ranking():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo"), _ev("c", "charlie")]
    reranker = EvidenceReranker(client=_client("[2, 0, 1]"))

    result = reranker.rerank(_req(), evidence)

    assert [e["id"] for e in result] == ["c", "a", "b"]


def test_rerank_passthrough_on_empty():
    client = _client("[0]")
    reranker = EvidenceReranker(client=client)

    assert reranker.rerank(_req(), []) == []

    client.chat.assert_not_called()


def test_rerank_passthrough_on_single():
    client = _client("[0]")
    reranker = EvidenceReranker(client=client)
    evidence = [_ev("a", "alpha")]

    result = reranker.rerank(_req(), evidence)

    assert result == evidence
    client.chat.assert_not_called()


def test_rerank_passthrough_on_llm_failure():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo")]
    reranker = EvidenceReranker(client=_client(side_effect=RuntimeError("boom")))

    result = reranker.rerank(_req(), evidence)

    assert result == evidence  # original order preserved


def test_rerank_passthrough_on_parse_failure():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo")]

    for bad in ("not json", "{}", '"a string"', "42", "[\"a\", \"b\"]"):
        reranker = EvidenceReranker(client=_client(bad))
        assert reranker.rerank(_req(), list(evidence)) == evidence, f"failed for: {bad!r}"


def test_rerank_truncates_with_top_k():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo"), _ev("c", "charlie")]
    reranker = EvidenceReranker(client=_client("[2, 0, 1]"))

    result = reranker.rerank(_req(), evidence, top_k=2)

    assert [e["id"] for e in result] == ["c", "a"]


def test_rerank_drops_invalid_indices_keeps_unreferenced():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo"), _ev("c", "charlie"), _ev("d", "delta")]
    # 9 is out of range -> dropped; 3 and 0 are valid and ordered first;
    # unreferenced b, c are appended in original order so nothing is lost.
    reranker = EvidenceReranker(client=_client("[3, 9, 0]"))

    result = reranker.rerank(_req(), evidence)

    assert [e["id"] for e in result] == ["d", "a", "b", "c"]
    assert len(result) == len(evidence)  # never drops evidence


def test_rerank_uses_evidence_rerank_usage_stage():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo")]
    client = _client("[0, 1]")
    EvidenceReranker(client=client).rerank(_req(), evidence)

    assert client.chat.call_args.kwargs.get("usage_stage") == "evidence_rerank"
