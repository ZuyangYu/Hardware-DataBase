from __future__ import annotations

import json

import pytest

from src.document_authoring.models import ManagedDraftPayload
from src.document_authoring.writers.managed import LLMManagedWriter
from src.document_authoring.writers.provider import WriterRequest


def _request() -> WriterRequest:
    return WriterRequest(
        work_order_id="wo-1",
        run_id="run-1",
        unit_id="field:f1",
        unit_label="额定电流",
        field_value_type="number",
        prompt_version="1",
        evidence=[{
            "id": "e1",
            "content": "额定电流为 10A",
            "source_name": "spec.pdf",
            "metadata": {},
            "locator": {},
            "fact_type": None,
        }],
    )


def _payload() -> ManagedDraftPayload:
    return ManagedDraftPayload.model_validate({
        "content": "额定电流为 10A",
        "proposed_value": "10A",
        "typed_value": {
            "kind": "scalar",
            "normalized_values": ["10A"],
            "display_value": "10A",
            "evidence_ids": ["e1"],
        },
        "assertions": [{
            "assertion_id": "a1",
            "text": "额定电流为 10A",
            "claim_id": "claim-f1",
            "evidence_ids": ["e1"],
            "value": "10A",
            "consistency_key": "field:f1",
        }],
        "evidence_ids": ["e1"],
    })


class _Runnable:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.response


class _StructuredModel:
    provider = "fake"
    model_name = "fake-structured"

    def __init__(self, response=None):
        self.response = response or _payload()
        self.with_structured_output_calls = []
        self.runnable = _Runnable(self.response)

    def with_structured_output(self, schema):
        self.with_structured_output_calls.append(schema)
        return self.runnable


def test_structured_model_is_used_and_coordinator_owns_identity_and_status():
    model = _StructuredModel()

    draft = LLMManagedWriter(model=model).generate(_request())

    assert model.with_structured_output_calls == [ManagedDraftPayload]
    assert len(model.runnable.calls) == 1
    assert draft.unit_id == "field:f1"
    assert draft.run_id == "run-1"
    assert draft.generated_by == "managed_writer"
    assert draft.proposed_status == "draft"
    assert draft.validation_status == "pending"
    assert draft.metadata["writer_mode"] == "structured"
    assert draft.metadata["writer_fallback"] is False


def test_structured_payload_forbids_coordinator_fields():
    with pytest.raises(Exception):
        ManagedDraftPayload.model_validate({**_payload().model_dump(), "run_id": "other"})


def test_structured_evidence_boundary_retries_then_records_fallback():
    bad = _payload().model_copy(update={"evidence_ids": ["outside"]})
    model = _StructuredModel(bad)

    draft = LLMManagedWriter(model=model).generate(_request())

    assert len(model.runnable.calls) == 2
    assert draft.content == "额定电流为 10A"
    assert draft.metadata["writer_mode"] == "structured"
    assert draft.metadata["writer_fallback"] is True
    assert draft.metadata["fallback_reason"] == "structured_output_validation_failed"
    assert draft.validation_status == "pending"


def test_provider_without_structured_output_uses_explicit_json_compatibility():
    class JsonModel(_StructuredModel):
        def with_structured_output(self, _schema):
            raise NotImplementedError("structured output is unsupported")

        def invoke(self, _messages):
            return json.dumps({
                **_payload().model_dump(mode="json"),
                "unit_id": "attacker-unit",
                "run_id": "attacker-run",
                "validation_status": "supported",
            }, ensure_ascii=False)

    model = JsonModel()
    draft = LLMManagedWriter(model=model).generate(_request())

    assert draft.content == "额定电流为 10A"
    assert draft.unit_id == "field:f1"
    assert draft.run_id == "run-1"
    assert draft.validation_status == "pending"
    assert draft.metadata["writer_mode"] == "json_fallback"
    assert draft.metadata["writer_fallback"] is False


def test_missing_model_usage_is_unknown_and_not_zero():
    model = _StructuredModel()
    draft = LLMManagedWriter(model=model).generate(_request())

    observation = draft.metadata["llm_observations"][0]
    assert observation["usage_returned"] is False
    assert observation["prompt_tokens"] == "unknown"
    assert observation["completion_tokens"] == "unknown"
    assert observation["total_tokens"] == "unknown"
