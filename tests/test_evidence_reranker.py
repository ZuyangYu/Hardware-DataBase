from __future__ import annotations

from src.agents.claim_evidence import InformationRequirement
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


class _TextModel:
    def __init__(self, response: str | Exception = "[]"):
        self.response = response
        self.calls: list[tuple[object, dict]] = []

    def with_structured_output(self, schema):
        raise NotImplementedError("structured output is not supported")

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_rerank_reorders_by_llm_ranking():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo"), _ev("c", "charlie")]
    reranker = EvidenceReranker(model=_TextModel("[2, 0, 1]"))

    result = reranker.rerank(_req(), evidence)

    assert [e["id"] for e in result] == ["c", "a", "b"]


def test_rerank_passthrough_on_empty():
    model = _TextModel("[0]")
    reranker = EvidenceReranker(model=model)

    assert reranker.rerank(_req(), []) == []

    assert model.calls == []


def test_rerank_passthrough_on_single():
    model = _TextModel("[0]")
    reranker = EvidenceReranker(model=model)
    evidence = [_ev("a", "alpha")]

    result = reranker.rerank(_req(), evidence)

    assert result == evidence
    assert model.calls == []


def test_rerank_passthrough_on_llm_failure():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo")]
    reranker = EvidenceReranker(model=_TextModel(RuntimeError("boom")))

    result = reranker.rerank(_req(), evidence)

    assert result == evidence  # original order preserved


def test_rerank_passthrough_on_parse_failure():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo")]

    for bad in ("not json", "{}", '"a string"', "42", "[\"a\", \"b\"]"):
        reranker = EvidenceReranker(model=_TextModel(bad))
        assert reranker.rerank(_req(), list(evidence)) == evidence, f"failed for: {bad!r}"


def test_rerank_truncates_with_top_k():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo"), _ev("c", "charlie")]
    reranker = EvidenceReranker(model=_TextModel("[2, 0, 1]"))

    result = reranker.rerank(_req(), evidence, top_k=2)

    assert [e["id"] for e in result] == ["c", "a"]


def test_rerank_drops_invalid_indices_keeps_unreferenced():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo"), _ev("c", "charlie"), _ev("d", "delta")]
    # 9 is out of range -> dropped; 3 and 0 are valid and ordered first;
    # unreferenced b, c are appended in original order so nothing is lost.
    reranker = EvidenceReranker(model=_TextModel("[3, 9, 0]"))

    result = reranker.rerank(_req(), evidence)

    assert [e["id"] for e in result] == ["d", "a", "b", "c"]
    assert len(result) == len(evidence)  # never drops evidence


def test_rerank_uses_evidence_rerank_usage_stage():
    evidence = [_ev("a", "alpha"), _ev("b", "bravo")]
    model = _TextModel("[0, 1]")
    EvidenceReranker(model=model).rerank(_req(), evidence)

    assert len(model.calls) == 1
    assert model.calls[0][1] == {}
