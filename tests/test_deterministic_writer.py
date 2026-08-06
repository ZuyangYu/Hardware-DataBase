from __future__ import annotations

import json

import pytest

from src.document_authoring.validator import DocumentValidator
from src.document_authoring.writers.managed import _build_user_prompt
from src.document_authoring.writers.provider import WriterRequest


def _request(evidence):
    return WriterRequest(
        work_order_id="wo-1", run_id="run-1", unit_id="field:f1",
        unit_label="额定电流", prompt_version="1", evidence=evidence,
    )


def _ev(eid: str, content: str) -> dict:
    return {"id": eid, "content": content, "source_name": "s.pdf",
            "metadata": {}, "locator": {}, "fact_type": None}


def test_single_evidence_verbatim():
    from src.document_authoring.writers.managed import _deterministic_draft

    draft = _deterministic_draft(_request([_ev("e1", "额定电流为 10A")]))

    assert draft.content == "额定电流为 10A"
    assert draft.proposed_value == "额定电流为 10A"
    assert draft.evidence_ids == ["e1"]
    assert len(draft.assertions) == 1
    assert draft.assertions[0].evidence_ids == ["e1"]
    assert draft.assertions[0].text == "额定电流为 10A"
    assert draft.validation_status == "pending"  # graph decides, not the writer


def test_scalar_writer_extracts_a_typed_value_from_an_explicit_assignment():
    from src.document_authoring.writers.managed import _deterministic_draft

    draft = _deterministic_draft(_request([_ev("e1", "额定电流为 10A")]))

    assert draft.typed_value is not None
    assert draft.typed_value.kind == "scalar"
    assert draft.typed_value.normalized_values == ["10A"]
    assert draft.typed_value.display_value == "10A"
    assert draft.typed_value.evidence_ids == ["e1"]


def test_multi_evidence_summarizes_all():
    from src.document_authoring.writers.managed import _deterministic_draft

    draft = _deterministic_draft(_request([
        _ev("e1", "额定电流为 10A"),
        _ev("e2", "电源拓扑为 buck"),
    ]))

    # Both evidence contents are present; nothing fabricated.
    assert "额定电流为 10A" in draft.content
    assert "电源拓扑为 buck" in draft.content
    assert draft.evidence_ids == ["e1", "e2"]
    # One summary assertion referencing all evidence.
    assert len(draft.assertions) == 1
    assert set(draft.assertions[0].evidence_ids) == {"e1", "e2"}
    assert draft.validation_status == "pending"


def test_multi_evidence_passes_validation_no_inner_conflict():
    from src.document_authoring.writers.managed import _deterministic_draft

    evidence = [_ev("e1", "额定电流为 10A"), _ev("e2", "电源拓扑为 buck")]
    draft = _deterministic_draft(_request(evidence))
    evidence_by_id = {item["id"]: item for item in evidence}

    validated = DocumentValidator().validate_unit_draft(draft, evidence_by_id)
    assert validated.validation_status == "supported", validated.validation_notes

    # A single summary assertion with one value must not trip cross-unit conflict.
    conflicts = DocumentValidator().validate_cross_unit_consistency([validated])
    assert conflicts == []


def test_no_evidence_raises():
    from src.document_authoring.writers.managed import _deterministic_draft

    with pytest.raises(ValueError):
        _deterministic_draft(_request([]))


def test_llm_build_user_prompt_includes_all_evidence():
    request = _request([
        _ev("e1", "额定电流为 10A"),
        _ev("e2", "电源拓扑为 buck"),
    ])

    prompt = _build_user_prompt(request, None)

    # Regression lock: the LLM prompt carries every evidence id and content,
    # not just the first chunk.
    assert "e1" in prompt and "额定电流为 10A" in prompt
    assert "e2" in prompt and "电源拓扑为 buck" in prompt


def test_llm_writer_requires_and_returns_a_typed_value():
    from src.document_authoring.writers.managed import LLMManagedWriter

    class Client:
        def chat(self, messages, **kwargs):
            return json.dumps({
                "unit_id": "field:f1",
                "run_id": "run-1",
                "generated_by": "managed_writer",
                "content": "额定电流为 10A",
                "proposed_value": "10A",
                "typed_value": {
                    "kind": "scalar",
                    "normalized_values": ["10A"],
                    "display_value": "10A",
                    "evidence_ids": ["e1"],
                },
                "assertions": [{
                    "assertion_id": "assertion-1",
                    "text": "额定电流为 10A",
                    "claim_id": "claim-f1",
                    "evidence_ids": ["e1"],
                }],
                "evidence_ids": ["e1"],
                "proposed_status": "draft",
                "validation_status": "pending",
                "validation_notes": [],
            }, ensure_ascii=False)

    draft = LLMManagedWriter(Client()).generate(_request([_ev("e1", "额定电流为 10A")]))

    assert draft.typed_value.display_value == "10A"
