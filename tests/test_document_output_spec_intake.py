from __future__ import annotations

import pytest

from src.document_authoring.planning.intake import (
    OutputSpecIntakeService,
    OutputSpecVersionConflict,
)


def test_template_free_draft_asks_one_material_question_at_a_time():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-1",
        document_type=None,
        template_version_id=None,
    )

    question = intake.next_question(draft)
    assert question.question_id == "purpose"
    assert len(question.options) <= 3
    assert draft.get("layout_source") is None

    draft = intake.merge_answer(draft, expected_version=1, question_id="purpose", answer="生成测试报告")
    question = intake.next_question(draft)
    assert question.question_id == "document_type"


def test_template_backed_draft_uses_server_template_reference_and_accepts_free_text():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-2",
        document_type="report",
        template_version_id="template-1",
        template_schema_id="template-schema-1",
        template_schema_version="1",
    )
    assert draft["layout_source"]["mode"] == "provided_template"
    draft = intake.merge_answer(draft, expected_version=1, question_id="purpose", answer="按模块生成报告")
    assert draft["purpose"] == "按模块生成报告"


def test_recommendation_acceptance_records_ids_without_confirming_execution():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(output_spec_id="spec-3", document_type="report")
    recommendations = intake.recommendations(draft)
    assert recommendations
    accepted = intake.accept_recommendation(
        draft,
        recommendation_id=recommendations[0]["id"],
    )
    assert recommendations[0]["id"] in accepted["accepted_recommendations"]
    assert accepted["missing_data_policy"] == "mark_tbd"
    assert accepted["inference_policy"] == "forbid"
    assert accepted.get("confirmed") is not True


def test_intake_uses_optimistic_version_and_rejects_stale_answer():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(output_spec_id="spec-4", document_type="report")
    changed = intake.merge_answer(draft, expected_version=1, question_id="purpose", answer="报告")
    assert changed["version"] == 2
    with pytest.raises(OutputSpecVersionConflict):
        intake.merge_answer(changed, expected_version=1, question_id="document_type", answer="测试报告")


def test_complete_output_spec_is_emitted_only_when_required_fields_exist():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-5",
        purpose="生成报告",
        document_type="report",
        template_version_id=None,
        layout_source={
            "mode": "generated_structure",
            "constraints_profile_id": "generic-report",
            "constraints_profile_version": "1",
        },
        outline=[{"unit_id": "summary", "kind": "section", "title": "Summary", "required": True}],
        deliverables=[{"format": "markdown", "role": "primary", "required": True, "requested_by": "user"}],
        missing_data_policy="mark_tbd",
        inference_policy="forbid",
        approval_policy_id="default-document-v1",
    )
    assert intake.is_complete(draft)
    spec = intake.to_output_spec(draft)
    assert spec.output_spec_id == "spec-5"
    assert spec.layout_source.mode == "generated_structure"
