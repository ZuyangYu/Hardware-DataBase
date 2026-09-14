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


def test_llm_transport_or_empty_response_falls_back_to_evidence_copy():
    from src.document_authoring.writers.managed import LLMManagedWriter

    class EmptyResponseClient:
        calls = 0

        def invoke(self, _messages, **kwargs):
            self.calls += 1
            raise RuntimeError("Chat API returned an empty response")

    client = EmptyResponseClient()
    draft = LLMManagedWriter(client).generate(_request([_ev("e1", "额定电流为 10A")]))

    assert client.calls == 2
    assert draft.content == "额定电流为 10A"
    assert draft.validation_status == "pending"


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


def test_llm_writer_prompt_includes_field_focus_terms():
    request = _request([_ev("e1", "X1902-3 connects to PGND")]).model_copy(
        update={"retrieval_query_terms": ["X1902-3", "PGND"]}
    )

    prompt = _build_user_prompt(request, None)

    assert "X1902-3" in prompt
    assert "PGND" in prompt


def test_llm_writer_requires_and_returns_a_typed_value():
    from src.document_authoring.writers.managed import LLMManagedWriter

    class Client:
        def invoke(self, messages, **kwargs):
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


def test_llm_managed_writer_uses_connector_manual_for_function_fields_without_model_call():
    from src.document_authoring.writers.managed import LLMManagedWriter

    class Client:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("connector function should be deterministic")

    request = _request([{
        "id": "manual-x1900-1",
        "content": "X1900 pin 1: CAN high differential bus signal.",
        "source_name": "CONN-1900 datasheet.pdf",
        "metadata": {"source_role": "datasheet"},
        "locator": {},
        "fact_type": None,
    }]).model_copy(update={
        "unit_label": "功能描述 Function",
        "retrieval_query_terms": ["X1900-1", "CAN0H"],
    })

    draft = LLMManagedWriter(Client()).generate(request)

    assert "CAN high differential bus signal" in draft.content
    assert "X1900-1" in draft.content


def test_connector_function_writer_uses_net_rule_when_manual_has_no_pin_description():
    from src.document_authoring.writers.managed import LLMManagedWriter

    class Client:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("connector function should be deterministic")

    request = _request([_ev("circuit-x1903-1", "X1903-1 is connected to MIPI0_DATA0_P")]).model_copy(update={
        "unit_label": "功能描述 Function",
        "retrieval_query_terms": ["X1903-1"],
    })

    draft = LLMManagedWriter(Client()).generate(request)

    assert "MIPI 摄像头高速差分数据信号（规则推断）" in draft.content


def test_connector_function_resolver_skips_typed_table_requests():
    """A typed table unit must be written as rows, never as one pin scalar."""
    from src.document_authoring.writers.managed import _connector_function_draft

    request = _request([{
        "id": "manual-x1900-1",
        "content": "X1900 pin 1: CAN high differential bus signal.",
        "source_name": "CONN-1900 datasheet.pdf",
        "metadata": {"source_role": "datasheet"},
        "locator": {},
        "fact_type": None,
    }]).model_copy(update={
        "unit_label": "功能描述 Function",
        "retrieval_query_terms": ["X1900-1", "CAN0H"],
        "field_value_type": "table",
        "table_mode": "typed_rows",
        "expected_columns": ["管脚号 Pin Number", "功能描述 Function"],
    })

    assert _connector_function_draft(request) is None


def test_writer_system_prompt_matches_the_structured_output_schema():
    """The prompt must not ask for coordinator-owned keys the schema forbids."""
    from src.document_authoring.models import ManagedDraftPayload
    from src.document_authoring.writers.managed import _WRITER_SYSTEM_PROMPT

    for field in ManagedDraftPayload.model_fields:
        assert field in _WRITER_SYSTEM_PROMPT
    assert "exactly these five" in _WRITER_SYSTEM_PROMPT
    # The coordinator-owned keys must not be requested as model-authored values.
    assert 'must be exactly "managed_writer"' not in _WRITER_SYSTEM_PROMPT
    assert 'use "draft"' not in _WRITER_SYSTEM_PROMPT
    assert 'MUST be exactly "pending"' not in _WRITER_SYSTEM_PROMPT
    assert "MUST be an empty list []" not in _WRITER_SYSTEM_PROMPT


def _pin_table_request():
    columns = {
        "A": "管脚号 Pin Number",
        "B": "管脚定义 Pin Definition",
        "C": "功能描述 Function",
        "D": "备注 Notice",
        "E": "总成ERP\n600600653",
    }
    evidence = [{
        "id": "frozen-icd-pin-set:825504380_ADAS_SCH_TCN2.EDF:X1900:1|X1900:2",
        "content": "Frozen ICD pin mappings: X1900-1 -> CAN0-H; X1900-2 -> GND.",
        "source_name": "825504380_ADAS_SCH_TCN2.EDF",
        "metadata": {"pin_mappings": [
            {"refdes": "X1900", "pin_name": "1", "net_name": "CAN0-H"},
            {"refdes": "X1900", "pin_name": "2", "net_name": "GND"},
        ]},
        "locator": {},
        "fact_type": "connector_pin_mapping",
    }]
    return WriterRequest(
        work_order_id="wo-1", run_id="run-1", unit_id="field:table:Sheet1:15",
        unit_label="管脚表", field_value_type="table", table_mode="typed_rows",
        table_columns=columns, expected_columns=list(columns), prompt_version="1",
        evidence=evidence,
    )


def test_frozen_pin_table_assembles_typed_rows_without_llm():
    from src.document_authoring.writers.managed import LLMManagedWriter

    class Client:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("table must be deterministic")

    request = _pin_table_request()
    draft = LLMManagedWriter(Client()).generate(request)

    assert draft.typed_value is not None
    assert draft.typed_value.kind == "table"
    assert [row.row_key for row in draft.typed_value.rows] == ["X1900-1", "X1900-2"]
    first = draft.typed_value.rows[0]
    assert first.cells["A"] == "X1900-1"
    assert first.cells["B"] == "CAN0-H"
    assert first.cells["E"] == "TBD"
    assert draft.metadata.get("writer_mode") == "deterministic_pin_table"


def test_frozen_pin_table_passes_typed_validation_with_tbd_cells():
    from src.document_authoring.writers.managed import _deterministic_draft

    request = _pin_table_request()
    draft = _deterministic_draft(request)
    evidence = {item["id"]: item for item in request.evidence}
    validated = DocumentValidator().validate_typed_field_draft(
        DocumentValidator().validate_unit_draft(draft, evidence),
        evidence,
        expected_value_type=request.field_value_type,
        expected_row_keys=request.expected_row_keys,
        required_columns=request.expected_columns,
        row_order=request.row_order,
        duplicate_policy=request.duplicate_policy,
    )
    assert validated.validation_status == "supported", validated.validation_notes


def test_explicit_tbd_cell_is_not_required_to_be_anchored():
    from src.document_authoring.writers.managed import _deterministic_draft

    request = _request([_ev("e1", "filler text")]).model_copy(update={
        "field_value_type": "table",
        "table_mode": "typed_rows",
        "expected_columns": ["A"],
        "table_columns": {"A": "管脚号 Pin Number"},
    })
    request = request.model_copy(update={"evidence": [{
        "id": "e1", "content": "filler text", "source_name": "s.pdf",
        "metadata": {"pin_mappings": [
            {"refdes": "X1", "pin_name": "1", "net_name": "NC"},
        ]},
        "locator": {}, "fact_type": None,
    }]})
    draft = _deterministic_draft(request)
    evidence = {item["id"]: item for item in request.evidence}
    validated = DocumentValidator().validate_typed_field_draft(
        DocumentValidator().validate_unit_draft(draft, evidence),
        evidence,
        expected_value_type="table",
        required_columns=["A"],
    )
    assert validated.validation_status == "supported", validated.validation_notes


def test_frozen_pin_table_uses_second_pass_function_evidence():
    from src.document_authoring.writers.managed import _deterministic_draft

    request = _pin_table_request()
    hit = {
        "id": "hsi-x1900-function",
        "content": "X1900-1 唤醒控制输入；X1900-2 车身CAN0高，诊断CAN。",
        "source_name": "HSI.docx",
        "metadata": {"pin_function_hits": {
            "X1900-1": "唤醒控制输入",
            "X1900-2": "车身CAN0高",
        }},
        "locator": {},
        "fact_type": None,
    }
    request = request.model_copy(update={"evidence": [*request.evidence, hit]})
    draft = _deterministic_draft(request)
    rows = {row.row_key: row for row in draft.typed_value.rows}

    assert rows["X1900-2"].cells["C"] == "车身CAN0高"
    assert rows["X1900-2"].cell_evidence_ids["C"] == ["hsi-x1900-function"]
    assert "hsi-x1900-function" in rows["X1900-2"].evidence_ids
    assert rows["X1900-1"].cells["C"] == "唤醒控制输入"

    evidence = {item["id"]: item for item in request.evidence}
    validated = DocumentValidator().validate_typed_field_draft(
        DocumentValidator().validate_unit_draft(draft, evidence),
        evidence,
        expected_value_type="table",
        required_columns=request.expected_columns,
    )
    assert validated.validation_status == "supported", validated.validation_notes
