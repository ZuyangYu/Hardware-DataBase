"""Task 2: suggestion -> DocumentFieldSchema semantic contract inference."""

from __future__ import annotations

from src.document_authoring.contract_registry import (
    CAPABILITIES,
    normalized_missing_policy,
    normalized_value_type,
    supported_capabilities,
)
from src.document_authoring.template_analysis import (
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)
from src.document_authoring.service import DocumentGenerationService


def _suggestion(**overrides) -> TemplateAnalysisSuggestion:
    payload = dict(
        semantic_unit_id="field:rated-cell", label="额定电压(V)",
        target_unit_ids=["rated-cell"], retrieval_terms=["额定电压", "电压"],
        confidence=0.9,
    )
    payload.update(overrides)
    return TemplateAnalysisSuggestion.model_validate(payload)


def _units(**unit_overrides) -> list[TemplateAnalysisUnit]:
    base = dict(
        unit_id="rated-cell", locator={"cell": "B2", "sheet_name": "Review"},
        label="额定电压(V)", writable=True, value_kind="blank",
        structural_role_hint="scalar_input",
        neighborhood=[],
    )
    base.update(unit_overrides)
    return [TemplateAnalysisUnit.model_validate(base)]


def _field_for(suggestion, units=None):
    return DocumentGenerationService._field_for_suggestion(suggestion, units)


def test_rated_voltage_infers_number_and_tabular_or_claim_capability():
    field = _field_for(_suggestion(), _units())
    assert field.value_type == "number"
    assert "document_claim_lookup" in field.required_capabilities
    assert field.missing_policy == "mark_tbd"
    assert field.description


def test_repeating_table_infers_table_type_and_tabular_capability():
    suggestion = _suggestion(value_shape="repeating_table", label="管脚定义表")
    field = _field_for(suggestion, _units())
    assert field.value_type == "table"
    assert "tabular_lookup" in field.required_capabilities


def test_enum_date_version_fields_get_expected_types():
    enum_field = _field_for(
        _suggestion(label="冷却方式", retrieval_terms=["冷却方式", "方式"]),
        _units(label="冷却方式"),
    )
    assert enum_field.value_type == "enum"
    date_field = _field_for(
        _suggestion(label="发布日期", retrieval_terms=["发布日期", "日期"]),
        _units(label="发布日期"),
    )
    assert date_field.value_type == "date"
    version_field = _field_for(
        _suggestion(label="固件版本", retrieval_terms=["固件版本", "版本"]),
        _units(label="固件版本"),
    )
    assert version_field.value_type == "version"


def test_low_confidence_llm_hints_do_not_override_deterministic_result():
    llm = _suggestion(confidence=0.5, value_type="boolean", missing_policy="keep_blank")
    field = _field_for(llm, _units())
    assert field.value_type == "number"
    assert field.missing_policy == "mark_tbd"


def test_out_of_allowlist_llm_values_are_ignored():
    llm = _suggestion(confidence=0.95, value_type="quantum", missing_policy="whatever",
                      required_capabilities=["filesystem_lookup"], preferred_source_roles=["hacker"])
    field = _field_for(llm, _units())
    assert field.value_type == "number"
    assert field.missing_policy == "mark_tbd"
    assert "filesystem_lookup" not in field.required_capabilities
    assert "hacker" not in field.preferred_source_roles


def test_high_confidence_in_allowlist_llm_hints_override():
    llm = _suggestion(
        confidence=0.95, value_type="date", missing_policy="block_section",
        required_capabilities=["document_claim_lookup", "revision_lookup"],
        preferred_source_roles=["released_design"], allow_derivation=True,
        description="固件发布日期",
    )
    field = _field_for(llm, _units())
    assert field.value_type == "date"
    assert field.missing_policy == "block_section"
    assert field.preferred_source_roles == ["released_design"]
    assert field.allow_derivation is True
    assert field.description == "固件发布日期"


def test_capability_allowlist_matches_retriever_supported_set():
    assert CAPABILITIES == frozenset({
        "entity_lookup", "relationship_lookup", "tabular_lookup",
        "document_claim_lookup", "revision_lookup",
    })
    supported, unsupported = supported_capabilities(
        ["tabular_lookup", "made_up_capability", "entity_lookup", "made_up_two"]
    )
    assert supported == ["tabular_lookup", "entity_lookup"]
    assert unsupported == ["made_up_capability", "made_up_two"]


def test_graph_capabilities_report_unsupported_instead_of_silent_drop(caplog):
    from src.document_authoring.harness.graph import _capabilities

    with caplog.at_level("WARNING"):
        result = _capabilities(["tabular_lookup", "made_up_capability"])
    assert result == ["tabular_lookup"]
    assert any("made_up_capability" in record.message for record in caplog.records)


def test_registry_normalizers():
    assert normalized_value_type("Number") == "number"
    assert normalized_value_type("quantum") is None
    assert normalized_missing_policy("BLOCK_SECTION") == "block_section"
    assert normalized_missing_policy("whatever") is None


def test_legacy_suggestion_without_contract_fields_uses_safe_defaults():
    legacy = _suggestion().model_dump(mode="json")
    for key in ("value_type", "required_capabilities", "preferred_source_roles",
                "missing_policy", "allow_derivation", "description"):
        legacy.pop(key)
    suggestion = TemplateAnalysisSuggestion.model_validate(legacy)
    field = _field_for(suggestion, _units())
    assert field.value_type == "number"
    assert field.missing_policy == "mark_tbd"
    assert field.retrieval_policy_id == "retrieval-field:rated-cell"
    assert field.verification_policy_id == "verification-field:rated-cell"
    assert field.query_terms == ["额定电压", "电压"]
    assert field.authoring_policy == "managed_writer"
