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


def test_complete_structure_groups_policy_defaults_into_one_question():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-policy-group",
        purpose="生成 ICD",
        document_type="icd",
        layout_source={
            "mode": "provided_template",
            "template_version_id": "template-1",
            "template_schema_id": "schema-1",
            "template_schema_version": "1",
        },
        outline=[{"unit_id": "pins", "kind": "table", "title": "管脚", "required": True}],
        artifact={"deliverables": [{"format": "xlsx", "role": "primary", "required": True}]},
        target_identity={"name": "EQ6 ADAS", "connector": "X1900"},
    )

    question = intake.next_question(draft)
    assert question.question_id == "recommendations"
    assert question.options == ["采用推荐方案", "逐项设置"]

    custom = intake.merge_answer(
        draft,
        expected_version=1,
        question_id="recommendations",
        answer="逐项设置",
    )
    assert intake.next_question(custom).question_id == "missing_data_policy"


def test_template_backed_icd_requires_target_identity_before_policy_defaults():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-icd-target",
        purpose="根据知识库生成新的 ICD",
        document_type="icd",
        layout_source={
            "mode": "provided_template",
            "template_version_id": "template-1",
            "template_schema_id": "schema-1",
            "template_schema_version": "1",
        },
        outline=[{"unit_id": "pins", "kind": "table", "title": "管脚", "required": True}],
        artifact={"deliverables": [{"format": "xlsx", "role": "primary", "required": True}]},
    )

    question = intake.next_question(draft)

    assert question.question_id == "target_identity"
    assert "连接器位号" in question.prompt


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
    draft["additional_requirements"] = [{
        "field": "generation_basis",
        "value": "actual_function",
        "meaning": "use_actual_function",
        "raw_text": "按实际功能生成",
    }]
    spec = intake.to_output_spec(draft)
    assert spec.output_spec_id == "spec-5"
    assert spec.layout_source.mode == "generated_structure"
    assert spec.additional_requirements[0].value == "actual_function"


@pytest.mark.parametrize(
    ("question_id", "answer", "expected"),
    [
        ("missing_data_policy", "A", "mark_tbd"),
        ("missing_data_policy", "选A", "mark_tbd"),
        ("missing_data_policy", "明确标注待补充并保留占位", "mark_tbd"),
        ("inference_policy", "B. 允许但必须标注", "allow_labeled"),
        ("approval_policy", "C", "custom-document-v1"),
        # 自然语言改写必须收敛到同一规范策略,而不是被拒绝三次。
        ("missing_data_policy", "缺数据时留空", "keep_blank"),
        ("missing_data_policy", "留空（不填写）", "keep_blank"),
        ("missing_data_policy", "使用默认值占位", "mark_tbd"),
        ("missing_data_policy", "标记为\"待确认/Unknown\"", "mark_tbd"),
        ("missing_data_policy", "leave_blank", "keep_blank"),
        ("missing_data_policy", "mark_unknown", "mark_tbd"),
        ("missing_data_policy", "abort", "block_generation"),
        ("missing_data_policy", "报错中止生成", "block_generation"),
    ],
)
def test_v2_clarification_answers_are_normalized_before_writing(
    question_id, answer, expected,
):
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(output_spec_id=f"spec-{question_id}")

    updated = intake.merge_answer(
        draft,
        expected_version=1,
        question_id=question_id,
        answer=answer,
    )

    if question_id == "approval_policy":
        assert updated["approval_policy_id"] == expected
    else:
        assert updated[question_id] == expected
    assert updated["clarification_answers"][-1]["raw_answer"] == answer
    assert updated["clarification_answers"][-1]["normalized_answer"] == expected


def test_option_text_maps_deliverable_and_layout_without_literal_letter_values():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(output_spec_id="spec-options")

    draft = intake.merge_answer(
        draft,
        expected_version=1,
        question_id="deliverables",
        answer="B. Word 文档",
    )
    assert draft["artifact"]["deliverables"][0]["format"] == "docx"

    draft = intake.merge_answer(
        draft,
        expected_version=2,
        question_id="layout_source",
        answer="B. 采用标准结构",
    )
    assert draft["layout_source"] == {
        "mode": "system_recipe",
        "recipe_id": "generic-report",
        "recipe_version": "1",
    }


def test_free_text_outline_rejects_bare_option_instead_of_creating_section_b():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(output_spec_id="spec-outline")

    with pytest.raises(ValueError, match="outline"):
        intake.merge_answer(
            draft,
            expected_version=1,
            question_id="outline",
            answer="B",
        )


def test_one_answer_merges_current_policy_and_explicit_cross_field_requirements():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(output_spec_id="spec-multi")

    updated = intake.merge_user_answer(
        draft,
        expected_version=1,
        question_id="missing_data_policy",
        answer="A，ERP 暂时不填，按实际功能生成，知识库自主检索",
    )

    assert updated["missing_data_policy"] == "mark_tbd"
    assert updated["target_identity"] == {}
    assert updated["source_scope"]["version_policy"] == "current_published"
    requirements = updated["additional_requirements"]
    assert {item["field"] for item in requirements} >= {
        "generation_basis",
        "source_scope",
    }
    erp_requirement = next(
        item for item in requirements if item["field"] == "target_identity.erp"
    )
    assert erp_requirement["value"] is None
    assert erp_requirement["meaning"] == "explicitly_unspecified"
    audit = updated["clarification_answers"][-1]
    assert audit["raw_answer"] == "A，ERP 暂时不填，按实际功能生成，知识库自主检索"
    assert audit["normalized_answer"] == "mark_tbd"


def test_additional_requirements_are_kept_when_current_question_is_not_answered():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(output_spec_id="spec-extra-only", purpose="生成文档")

    updated = intake.merge_user_answer(
        draft,
        expected_version=1,
        question_id="document_type",
        answer="物料号先不填写；知识库中内容自主规划和检索",
    )

    assert updated["document_type"] is None
    assert updated["target_identity"] == {}
    assert any(item["field"] == "target_identity.erp" for item in updated["additional_requirements"])
    assert intake.next_question(updated).question_id == "document_type"
    assert updated["version"] == 2


def test_one_answer_can_add_an_explicit_outline_while_answering_another_question():
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-outline-extra",
        purpose="生成文档",
        document_type="报告",
    )

    updated = intake.merge_user_answer(
        draft,
        expected_version=1,
        question_id="missing_data_policy",
        answer="A，文档结构包括：概述、接口明细表",
    )

    assert updated["missing_data_policy"] == "mark_tbd"
    assert [item["title"] for item in updated["outline"]] == ["概述", "接口明细表"]
    assert any(item["field"] == "outline" for item in updated["additional_requirements"])
